"""USB serial transport for the RaceLink gateway."""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
import struct

import serial
import serial.tools.list_ports

from .framing import mac_last3_from_hex, u16le
from .gateway_events import (
    EV_ERROR,
    EV_STATE_CHANGED,
    EV_STATE_REPORT,
    EV_TX_DONE,
    EV_TX_REJECTED,
    GATEWAY_STATE_NAME,
    GATEWAY_STATE_RX_WINDOW,
    GATEWAY_STATE_UNKNOWN,
    GW_CMD_STATE_REQUEST,
    LP,
    TX_REJECT_REASON_NAME,
    TX_REJECT_UNKNOWN,
)
from ..protocol.codec import parse_reply_event
from ..protocol.packets import (
    build_config_body,
    build_control_body,
    build_get_devices_body,
    build_offset_body,
    build_preset_body,
    build_set_group_body,
    build_sync_body,
)

logger = logging.getLogger("racelink_transport")

# Synchronous _send_m2n outcome wait. Each host write blocks here for the
# gateway's matching outcome event (EV_TX_DONE -> SUCCESS, EV_TX_REJECTED ->
# REJECTED(reason)). The deadlock guard is a single 2-second ceiling — well
# above the LBT-induced worst case of ~550 ms at SF7 and the SF12 worst case
# of ~1100 ms — so a TIMEOUT outcome means a genuine gateway / USB stall, not
# normal LBT variance. (Pre-Batch-B this was a body-length-scaled barrier;
# the v4 redesign collapsed it because the gateway now NACKs on rejection.)
SEND_OUTCOME_TIMEOUT_S = 2.0


@dataclasses.dataclass(frozen=True)
class SendOutcome:
    """Outcome of a synchronous ``_send_m2n`` call.

    ``code`` is one of ``"SUCCESS" | "REJECTED" | "TIMEOUT" | "USB_ERROR"``.
    ``reason`` is populated for ``REJECTED`` only and carries the gateway's
    ``TX_REJECT_*`` byte; ``reason_name`` is the matching short label for
    log lines and operator-facing UI.

    ``SendOutcome`` is truthy iff the send succeeded — fire-and-forget
    callers can keep doing ``if transport.send_preset(...): ...`` while
    discriminating callers can branch on ``outcome.code`` /
    ``outcome.reason``.
    """

    ok: bool
    code: str  # 'SUCCESS' | 'REJECTED' | 'TIMEOUT' | 'USB_ERROR'
    reason: int = 0
    detail: str = ""

    def __bool__(self) -> bool:  # pragma: no cover - trivial
        return self.ok

    @property
    def reason_name(self) -> str:
        if self.code != "REJECTED":
            return ""
        return TX_REJECT_REASON_NAME.get(int(self.reason), "unknown")

    @classmethod
    def success(cls) -> "SendOutcome":
        return cls(True, "SUCCESS")

    @classmethod
    def rejected(cls, reason: int) -> "SendOutcome":
        return cls(False, "REJECTED", int(reason) & 0xFF)

    @classmethod
    def timeout(cls, detail: str = "") -> "SendOutcome":
        return cls(False, "TIMEOUT", detail=detail)

    @classmethod
    def usb_error(cls, detail: str = "") -> "SendOutcome":
        return cls(False, "USB_ERROR", detail=detail)


class GatewaySerialTransport:
    """
    Thin USB transport mirroring racelink_proto on Host<->Device.

    Framing v1.1: [0x00][LEN][TYPE][DATA...]
      Host->Device DATA: [recv3(3)][Body]                (wire opcodes)
                         [] (1-byte payload [CMD])         (USB-only commands)
      Device->Host DATA: [Header7(7)][Body][RSSI(LE16)][SNR(i8)] for transport replies,
                         or payload per EV_* for USB events.
    """

    def __init__(self, port: str = None, baud: int = 921600, on_event=None):
        self.port = port
        self.baud = baud
        self.ident_mac = None
        self.on_event = on_event
        self.ser = serial.Serial()
        self.ser.baudrate = baud
        # A9: 50 ms balances clean shutdown (RX loop checks ``_stop``
        # only between reads) with low CPU. The previous 200 ms made
        # ``close()`` wait up to that long for the reader to notice
        # the stop flag. 50 ms is well under typical inter-frame
        # latency on the USB CDC link.
        self.ser.timeout = 0.05
        self._stop = False
        self._rx_thread = None
        self._q = []
        self._qmax = 1000
        self._listeners = []
        self._tx_listeners = []
        # ----- Synchronous TX outcome (Batch B, 2026-04-28) -----------------
        # ``_tx_lock`` serializes concurrent ``_send_m2n`` callers so the USB
        # byte stream can never interleave frames AND so only one outcome can
        # ever be in flight at a time (the gateway's ``txPending`` semantic).
        # Without the lock two threads could write back-to-back, both register
        # outcome slots, and the gateway's first NACK / TX_DONE would land in
        # the wrong slot.
        #
        # The lock is paired with a Condition variable: the sender writes
        # the frame under the lock, then ``cv.wait_for(...)`` releases the
        # lock for the duration of the outcome wait so the RX reader can
        # briefly acquire it to populate the slot. Without the Condition
        # the sender's hold on the lock would deadlock against the RX
        # reader's slot-write.
        #
        # ``_pending_send_outcome`` is the slot itself: a single-element
        # list holding ``SendOutcome | None``. ``None`` means no send is
        # in flight; the RX reader drops orphan outcome events that don't
        # match a pending wait (e.g. EV_TX_DONE from gateway-internal
        # auto-sync TXs).
        self._tx_lock = threading.Lock()
        self._tx_outcome_cv = threading.Condition(self._tx_lock)
        self._pending_send_outcome: list | None = None
        # ----- Gateway state mirror (Batch B) ------------------------------
        # 1:1 mirror of the gateway's last-reported state. Updated only by
        # EV_STATE_CHANGED / EV_STATE_REPORT events; nothing else writes
        # here. Initial value UNKNOWN until the first STATE_REPORT (which
        # the host triggers via a STATE_REQUEST shortly after USB connect).
        self._gateway_state_byte = GATEWAY_STATE_UNKNOWN
        self._gateway_state_metadata_ms = 0
        # Set to True by ``discover_and_open`` when at least one candidate
        # port was skipped because another process held its exclusive lock.
        # Controllers use this to pick PORT_BUSY over NOT_FOUND when
        # ``discover_and_open`` returns False.
        self.last_discovery_had_busy_port = False

    # ---- gateway state accessors ------------------------------------------

    @property
    def gateway_state_byte(self) -> int:
        """Last-reported gateway state byte (or UNKNOWN sentinel before first event)."""
        return int(self._gateway_state_byte)

    @property
    def gateway_state_name(self) -> str:
        """Short label for the current gateway state (e.g. ``IDLE``, ``RX_WINDOW``)."""
        return GATEWAY_STATE_NAME.get(int(self._gateway_state_byte), "UNKNOWN")

    @property
    def gateway_state_metadata_ms(self) -> int:
        """RX_WINDOW min_ms metadata, or 0 for states without metadata."""
        return int(self._gateway_state_metadata_ms)

    def gateway_state_snapshot(self) -> dict:
        return {
            "state_byte": self.gateway_state_byte,
            "state": self.gateway_state_name,
            "state_metadata_ms": self.gateway_state_metadata_ms,
        }

    # ---- port discovery / open / close ------------------------------------

    def _apply_low_latency(self) -> None:
        """Drop the USB-serial bridge's latency_timer to ~1 ms (Linux only).

        Most USB-serial bridge chips (CP210x, FTDI ftdi_sio, CH340) default to
        a 16 ms latency_timer that buffers small RX bursts before flushing
        to the host. For the RaceLink interactive request/response traffic
        (small frames, low duty cycle) this dominates per-packet overhead:
        empirical measurement showed ~25 ms wall-clock per packet with the
        default timer, dropping to <10 ms with the low-latency mode set.

        ``pyserial.Serial.set_low_latency_mode(True)`` writes ASYNC_LOW_LATENCY
        via TIOCSSERIAL on Linux, which the kernel USB-serial drivers honour by
        cutting their bulk-IN poll interval to 1 ms. On Windows / macOS the
        method raises NotImplementedError or is a no-op; we silently swallow
        either case so the host still works on those platforms (just at
        higher per-packet overhead).
        """
        setter = getattr(self.ser, "set_low_latency_mode", None)
        if not callable(setter):
            # pyserial < 3.4 — nothing we can do from code.
            return
        # Use ser.port (the actually-opened device path) rather than self.port —
        # during discover_and_open we set ser.port + open() per candidate
        # before self.port is committed, so logging self.port would show
        # "None" until the identify reply matches.
        port_label = getattr(self.ser, "port", None) or self.port or "?"
        try:
            setter(True)
            logger.debug("USB low-latency mode enabled on %s", port_label)
        except (NotImplementedError, OSError, Exception) as e:
            # Windows / macOS / non-USB-serial port → method either raises
            # NotImplementedError or fails silently with OSError. Log at
            # debug so a Linux operator can see why the cap might still be
            # 16 ms (e.g. running as a non-root user on a kernel that
            # blocks TIOCSSERIAL writes).
            logger.debug(
                "USB low-latency mode unavailable on %s (%s: %s)",
                port_label, type(e).__name__, e,
            )

    @staticmethod
    def _is_usb_port(portinfo):
        try:
            dev = (getattr(portinfo, "device", "") or "").lower()
            if dev.startswith("/dev/ttyusb") or dev.startswith("/dev/ttyacm"):
                return True
            if getattr(portinfo, "vid", None) is not None or getattr(portinfo, "pid", None) is not None:
                return True
            desc = (getattr(portinfo, "description", "") or "").upper()
            if "USB" in desc:
                return True
        except Exception:
            # swallow-ok: best-effort fallback; caller proceeds with safe default
            pass
        return False

    def discover_and_open(self) -> bool:
        self.last_discovery_had_busy_port = False
        if self.port:
            try:
                self.ser.port = self.port
                try:
                    self.ser.exclusive = True  # type: ignore[attr-defined]
                except Exception:
                    # swallow-ok: best-effort fallback; caller proceeds with safe default
                    pass
                self.ser.open()
                self._apply_low_latency()
                return True
            except serial.SerialException as e:
                msg = str(e)
                if "Could not exclusively lock port" in msg or "Resource temporarily unavailable" in msg:
                    pretty = (
                        f"Port busy: {self.port} is in use by another process (exclusive lock failed). "
                        "Close the other program (e.g. esptool, screen/minicom) and retry."
                    )
                    logger.warning(pretty)
                    raise serial.SerialException(pretty) from e
                raise

        payload = struct.pack(">BBBB", 0x00, 0x01, 1, 0xFF)
        ident = b"RaceLink_Gateway_v4"

        for p in serial.tools.list_ports.comports():
            if not self._is_usb_port(p):
                continue

            try:
                self.ser.port = p.device
                try:
                    self.ser.exclusive = True  # type: ignore[attr-defined]
                except Exception:
                    # swallow-ok: best-effort fallback; caller proceeds with safe default
                    pass
                self.ser.open()
                self._apply_low_latency()
                time.sleep(0.5)
                self.ser.reset_input_buffer()
                self.ser.write(payload)
                resp = self.ser.read(len(ident) + 17)

                if not resp:
                    logger.debug("Port %s: no identify response", p.device)
                    self.ser.close()
                    continue

                if resp.startswith(ident):
                    self.port = p.device
                    mac_ascii = ""
                    try:
                        mac_ascii = resp[len(ident):].decode("ascii", errors="ignore").strip().strip("\x00")
                    except Exception:
                        # swallow-ok: best-effort fallback; caller proceeds with safe default
                        mac_ascii = ""

                    self.ident_mac = mac_ascii if mac_ascii else None

                    if self.ident_mac:
                        logger.info("Identify matched on %s (MAC: %s)", p.device, self.ident_mac)
                    else:
                        logger.info("Identify matched on %s (%r)", p.device, resp[: len(ident) + 6])

                    return True

                logger.debug("Port %s: unexpected identify reply: %r", p.device, resp[:32])
                self.ser.close()

            except serial.SerialException as e:
                msg = str(e)
                if "Could not exclusively lock port" in msg or "Resource temporarily unavailable" in msg:
                    logger.debug("Skip busy port %s (exclusive lock failed)", p.device)
                    self.last_discovery_had_busy_port = True
                else:
                    logger.debug("Open/identify failed on %s: %s", p.device, msg)
                try:
                    if getattr(self.ser, "is_open", False):
                        self.ser.close()
                except Exception:
                    # swallow-ok: best-effort fallback; caller proceeds with safe default
                    pass

        return False

    def open(self):
        if not self.port:
            raise RuntimeError("GatewaySerialTransport: no port set")
        self.ser.port = self.port
        if not self.ser.is_open:
            self.ser.open()
            self._apply_low_latency()

    def start(self):
        if not self.ser.is_open:
            self.open()
        self._stop = False
        import threading

        # A8: include the port in the thread name so multi-host
        # deployments / dev setups can distinguish RX threads in
        # threading.enumerate() output.
        port_label = (self.port or "noport").rsplit("/", 1)[-1]
        self._rx_thread = threading.Thread(
            target=self._reader,
            daemon=True,
            name=f"rl-serial-rx-{port_label}",
        )
        self._rx_thread.start()

    def close(self):
        self._stop = True
        if self._rx_thread:
            self._rx_thread.join(timeout=1.0)
        try:
            self.ser.close()
        except Exception:
            # A failed close leaves the exclusive lock on the OS FD -- the next
            # RH process will see it as ``PORT_BUSY`` until the kernel
            # eventually releases it. Log loud so it shows up in the hardware
            # log; do not re-raise (callers treat close as best-effort).
            logger.warning(
                "RaceLink transport close failed on %s", self.port, exc_info=True,
            )

    # ---- listeners --------------------------------------------------------

    def add_listener(self, cb):
        if cb and cb not in self._listeners:
            self._listeners.append(cb)

    def remove_listener(self, cb):
        try:
            if cb in self._listeners:
                self._listeners.remove(cb)
        except Exception:
            # swallow-ok: best-effort fallback; caller proceeds with safe default
            pass

    def add_tx_listener(self, cb):
        if cb and cb not in self._tx_listeners:
            self._tx_listeners.append(cb)

    def remove_tx_listener(self, cb):
        try:
            if cb in self._tx_listeners:
                self._tx_listeners.remove(cb)
        except Exception:
            # swallow-ok: best-effort fallback; caller proceeds with safe default
            pass

    def _emit_tx(self, ev: dict):
        for cb in list(self._tx_listeners):
            try:
                cb(ev)
            except Exception:
                # swallow-ok: a misbehaving TX listener must not crash
                # the transport. Debug-log with exc_info so a recurring
                # listener bug can be diagnosed without polluting the
                # warning log on every TX (this fires at packet rate).
                logger.debug(
                    "TX listener raised: cb=%r ev_type=%r",
                    cb, ev.get("type"), exc_info=True,
                )

    def _handle_disconnect(self, msg: str) -> None:
        logger.warning(msg)
        self._emit({"type": EV_ERROR, "data": msg})
        self._stop = True
        try:
            self.ser.close()
        except Exception:
            # swallow-ok: disconnect already happened; close failure
            # leaks the OS file descriptor for at most a short window.
            # Debug-log with traceback so a stuck-FD pattern is
            # diagnosable without spamming the warning log on every
            # transient disconnect.
            logger.debug(
                "ser.close failed during disconnect", exc_info=True,
            )

    # ---- send path --------------------------------------------------------

    def _send_m2n(
        self,
        type_full: int,
        recv3: bytes,
        body: bytes = b"",
        *,
        timeout_s: float | None = None,
    ) -> SendOutcome:
        """Synchronous send: writes the frame and blocks for the gateway's outcome.

        Returns a :class:`SendOutcome` whose ``code`` is ``SUCCESS`` (paired
        with EV_TX_DONE), ``REJECTED`` (paired with EV_TX_REJECTED, carrying
        the reason byte), ``TIMEOUT`` (no outcome arrived within
        ``SEND_OUTCOME_TIMEOUT_S`` — likely a gateway stall), or
        ``USB_ERROR`` (the OS write itself failed).

        Pipelining is intentionally not supported: ``_tx_lock`` enforces a
        single in-flight send. For N back-to-back sends the wall-clock cost
        is ``N × per-send RTT`` (~300-500 ms at SF7, more at higher SFs).
        Scenes typically have 1-10 actions; the trade buys a clean outcome
        contract without operator-noticeable latency.
        """
        if len(recv3) != 3:
            raise ValueError("recv3 must be 3 bytes")

        payload = bytes([type_full]) + recv3 + (body or b"")
        frame = bytes([0x00, len(payload)]) + payload
        wait_timeout = float(timeout_s if timeout_s is not None else SEND_OUTCOME_TIMEOUT_S)

        outcome_slot: list = [None]
        tx_event: dict | None = None
        disconnect_msg: str | None = None

        with self._tx_outcome_cv:
            # Register the outcome slot before writing so the RX reader
            # (which acquires _tx_lock briefly when an outcome event lands)
            # never sees a write-but-no-slot window.
            self._pending_send_outcome = outcome_slot
            try:
                self.ser.write(frame)
                # Force the OS USB-serial buffer onto the wire immediately —
                # without this small frames may sit in the pyserial / kernel
                # buffer waiting to be coalesced with a follow-up write.
                # Cheap (sub-ms) on a CDC link; meaningful when paired with
                # set_low_latency_mode on the gateway → host direction.
                try:
                    self.ser.flush()
                except Exception:
                    # swallow-ok: best-effort flush; the write itself
                    # succeeded so the frame will go out at whichever
                    # timer fires next.
                    pass
            except serial.SerialException as e:
                # No outcome will ever land for a frame that didn't make
                # it onto the USB wire. Mark the outcome USB_ERROR so any
                # downstream consumer (scene runner stop_on_error, etc.)
                # sees a clean failure.
                outcome_slot[0] = SendOutcome.usb_error(detail=str(e))
                self._pending_send_outcome = None
                disconnect_msg = f"USB TX failed: {e}"
            else:
                logger.debug(
                    "TX M2N type=0x%02X dir=%s opc=0x%02X recv3=%s len=%d body=%s",
                    type_full,
                    "M2N" if (type_full & 0x80) == LP.DIR_M2N else "N2M",
                    type_full & 0x7F,
                    recv3.hex().upper(),
                    len(body),
                    (body or b"").hex().upper(),
                )
                tx_event = {
                    "type": "TX_M2N",
                    "type_full": type_full,
                    "dir": type_full & 0x80,
                    "opc": type_full & 0x7F,
                    "recv3": recv3,
                    "body_len": len(body or b""),
                }

            # Block on the Condition so the RX reader can briefly acquire
            # _tx_lock to populate the slot. Condition.wait_for releases
            # the lock for the duration of the wait; without that the
            # sender's hold would deadlock against _fulfill_pending_outcome.
            # The deadlock guard caps the wait — a truly stalled gateway
            # returns TIMEOUT and the next send proceeds (host's recovery
            # path can call gateway_service.query_state() to verify life).
            if disconnect_msg is None:
                got = self._tx_outcome_cv.wait_for(
                    lambda: outcome_slot[0] is not None,
                    timeout=wait_timeout,
                )
                if not got:
                    outcome_slot[0] = SendOutcome.timeout(
                        detail=f"no EV_TX_DONE/EV_TX_REJECTED in {wait_timeout:.2f}s",
                    )
                    logger.warning(
                        "TX outcome timeout (%.0f ms, type=0x%02X opc=0x%02X) — "
                        "no EV_TX_DONE / EV_TX_REJECTED arrived. Likely gateway "
                        "or USB stall; consider a STATE_REQUEST to verify.",
                        wait_timeout * 1000,
                        type_full,
                        type_full & 0x7F,
                    )
                # Whether we got an outcome or hit the timeout, the slot is
                # now consumed. Clear it so the next caller (or a stray
                # late-arriving event) doesn't accidentally fill our slot.
                self._pending_send_outcome = None

        # Fan listener events out outside the critical section. A slow
        # listener can never block a subsequent _send_m2n behind us.
        if tx_event is not None:
            self._emit_tx(tx_event)

        # NOTE: do NOT use ``outcome_slot[0] or SendOutcome.timeout()`` — every
        # SendOutcome with ``ok=False`` (REJECTED, TIMEOUT, USB_ERROR) is
        # falsy, so the ``or`` would clobber a real REJECTED into a TIMEOUT.
        if disconnect_msg is not None:
            self._handle_disconnect(disconnect_msg)
            outcome = outcome_slot[0] if outcome_slot[0] is not None else SendOutcome.usb_error(detail=disconnect_msg)
        else:
            outcome = outcome_slot[0] if outcome_slot[0] is not None else SendOutcome.timeout()

        # Surface every outcome through the TX listener fan-out so the SSE
        # bridge / scene runner / diagnostics can observe REJECTED reasons
        # and TIMEOUT incidents without intercepting the call return value.
        self._emit_tx({
            "type": "TX_OUTCOME",
            "type_full": type_full,
            "dir": type_full & 0x80,
            "opc": type_full & 0x7F,
            "recv3": recv3,
            "outcome": outcome.code,
            "reason": outcome.reason,
            "reason_name": outcome.reason_name,
            "detail": outcome.detail,
        })
        return outcome

    # ---- high-level send wrappers ----------------------------------------

    def send_get_devices(self, recv3=b"\xFF\xFF\xFF", group_id=0, flags=0) -> SendOutcome:
        body = build_get_devices_body(group_id=group_id, flags=flags)
        return self._send_m2n(LP.make_type(LP.DIR_M2N, LP.OPC_DEVICES), recv3, body)

    def send_set_group(self, recv3: bytes, group_id: int) -> SendOutcome:
        body = build_set_group_body(group_id)
        return self._send_m2n(LP.make_type(LP.DIR_M2N, LP.OPC_SET_GROUP), recv3, body)

    def send_preset(self, recv3: bytes, group_id: int, flags: int, preset_id: int, brightness: int) -> SendOutcome:
        """Send an OPC_PRESET packet (4 B fixed, pre-rename: OPC_CONTROL)."""
        body = build_preset_body(group_id=group_id, flags=flags, preset_id=preset_id, brightness=brightness)
        return self._send_m2n(LP.make_type(LP.DIR_M2N, LP.OPC_PRESET), recv3, body)

    def send_control(self, recv3: bytes, group_id: int, flags: int, **params) -> SendOutcome:
        """Send an OPC_CONTROL packet (variable-length body, 3..21 B).

        Forwards all kwargs to ``build_control_body``; see that builder for
        the accepted field names (brightness, mode, speed, intensity, custom1,
        custom2, custom3, check1/2/3, palette, color1/2/3). Body length varies
        with which fields are provided and is framed via the generic
        ``_send_m2n`` path, which already handles variable-length bodies.
        (Pre-rename: ``send_control_adv`` / OPC_CONTROL_ADV.)
        """
        body = build_control_body(group_id=group_id, flags=flags, **params)
        return self._send_m2n(LP.make_type(LP.DIR_M2N, LP.OPC_CONTROL), recv3, body)

    def send_config(self, recv3: bytes = b"\xFF\xFF\xFF", option: int = 0, data0: int = 0, data1: int = 0, data2: int = 0, data3: int = 0) -> SendOutcome:
        body = build_config_body(option=option, data0=data0, data1=data1, data2=data2, data3=data3)
        return self._send_m2n(LP.make_type(LP.DIR_M2N, LP.OPC_CONFIG), recv3, body)

    def send_sync(self, recv3: bytes = b"\xFF\xFF\xFF", ts24: int = 0, brightness: int = 0,
                  flags: int = 0) -> SendOutcome:
        body = build_sync_body(ts24=ts24, brightness=brightness, flags=flags)
        return self._send_m2n(LP.make_type(LP.DIR_M2N, LP.OPC_SYNC), recv3, body)

    def send_offset(self, recv3: bytes = b"\xFF\xFF\xFF", group_id: int = 0,
                    mode="none", **mode_params) -> SendOutcome:
        """Send an OPC_OFFSET packet (variable-length, 2..7 B body).

        Sets the receiver's ``pending_change`` to the supplied mode + params;
        the pending change materialises on the next accepted
        OPC_CONTROL/OPC_PRESET (or on the OPC_SYNC that fires a queued
        arm-on-sync effect). See the comment block on ``OPC_OFFSET`` in
        ``racelink_proto.h`` for the full state machine.

        ``mode_params`` per mode (see ``packets.build_offset_body`` for the
        full contract):

            "none":     (no params)
            "explicit": offset_ms
            "linear":   base_ms, step_ms
            "vshape":   base_ms, step_ms, center
            "modulo":   base_ms, step_ms, cycle
        """
        body = build_offset_body(group_id=group_id, mode=mode, **mode_params)
        return self._send_m2n(LP.make_type(LP.DIR_M2N, LP.OPC_OFFSET), recv3, body)

    def send_stream(self, recv3: bytes, payload: bytes) -> SendOutcome:
        """Send one logical stream payload to the gateway.

        The gateway is responsible for splitting the payload into radio-sized
        packets, adding per-packet stream control bytes, and padding as needed.
        """
        return self._send_m2n(LP.make_type(LP.DIR_M2N, LP.OPC_STREAM), recv3, bytes(payload or b""))

    def send_wled_preset(self, recv3: bytes, group_id: int, state: int, preset_id: int, brightness: int) -> SendOutcome:
        """Legacy transport helper: send a WLED preset with a simple on/off state flag.

        ``preset_id`` (pre-rename: ``effect``) is the WLED preset number written
        into ``P_Preset.presetId``. The old name was misleading: OPC_PRESET
        carries a preset id, not a WLED effect-mode index — those live on
        OPC_CONTROL.
        """
        flags = 0x01 if int(state) else 0x00
        return self.send_preset(recv3, group_id, flags, int(preset_id), int(brightness))

    def send_get_status(self, recv3=b"\xFF\xFF\xFF", group_id=0, flags=0) -> SendOutcome:
        body = struct.pack("<BB", group_id & 0xFF, flags & 0xFF)
        return self._send_m2n(LP.make_type(LP.DIR_M2N, LP.OPC_STATUS), recv3, body)

    # ---- state-request command (USB-only, no LoRa wire) ------------------

    def send_state_request(self) -> bool:
        """Write a 1-byte GW_CMD_STATE_REQUEST frame to the gateway.

        Replies arrive asynchronously as ``EV_STATE_REPORT``; the host
        normally calls :meth:`gateway_service.query_state` which composes
        this with a wait-for-reply round-trip. Returns ``False`` if the
        USB write itself failed (the transport is then disconnecting).
        """
        # Frame: [0x00][LEN=1][CMD=0x7F]. No body, no recv3 — this command
        # is dispatched by the gateway's USB handler ahead of the wire-
        # protocol DIR_M2N branch.
        frame = bytes([0x00, 0x01, GW_CMD_STATE_REQUEST])
        with self._tx_lock:
            try:
                self.ser.write(frame)
                # Force the OS USB-serial buffer onto the wire — see _send_m2n
                # for the rationale (buffer-coalescing reduction).
                try:
                    self.ser.flush()
                except Exception:
                    # swallow-ok: best-effort flush; write succeeded.
                    pass
            except serial.SerialException as e:
                self._handle_disconnect(f"USB STATE_REQUEST write failed: {e}")
                return False
        logger.debug("TX GW_CMD_STATE_REQUEST (1 byte)")
        return True

    # ---- RX reader thread + frame dispatch -------------------------------

    def _reader(self):
        in_frame = False
        need = 0
        buf = bytearray()
        while not self._stop:
            try:
                # Chunked read: pull every byte that's already buffered in
                # the OS USB-serial queue in a single syscall, falling back
                # to a 1-byte blocking read (subject to ser.timeout) when
                # the queue is empty. Pre-fix the loop did read(1) per
                # byte, so a 4-byte frame header cost ~4 syscalls (each
                # waiting on the bridge's latency_timer to flush). With
                # set_low_latency_mode() shrinking that timer to ~1 ms,
                # a complete frame now usually arrives in one in_waiting
                # batch — we read it as one chunk and feed every byte
                # through the same state machine.
                n_avail = getattr(self.ser, "in_waiting", 0)
                chunk = self.ser.read(n_avail if n_avail > 0 else 1)
            except serial.SerialException as e:
                self._handle_disconnect(f"USB serial disconnected: {e}")
                break
            except Exception:
                # swallow-ok: in_waiting may raise on some platforms after
                # a USB disconnect that hasn't yet propagated to a
                # SerialException; fall back to the safe 1-byte path so
                # the loop still terminates on the next read failure.
                try:
                    chunk = self.ser.read(1)
                except serial.SerialException as e:
                    self._handle_disconnect(f"USB serial disconnected: {e}")
                    break
            if not chunk:
                continue
            for x in chunk:
                if not in_frame:
                    if x == 0x00:
                        in_frame = True
                        need = 0
                        buf.clear()
                    continue
                if need == 0:
                    need = x
                    continue
                buf.append(x)
                if len(buf) == need:
                    in_frame = False
                    # Defence in depth (Batch B follow-up): a single bad frame
                    # — corrupt body, codec bug, listener exception — must NOT
                    # kill the reader thread. If it did, every subsequent
                    # _send_m2n would block until the 2 s deadlock guard fires
                    # and then time out forever (no outcome events arrive
                    # without a live reader). Log loud so the bug stays
                    # diagnosable, then keep looping.
                    try:
                        self._handle_frame(buf[0], memoryview(buf)[1:].tobytes())
                    except Exception:
                        logger.exception(
                            "RX reader: _handle_frame raised on type=0x%02X len=%d; dropping frame and continuing",
                            buf[0], len(buf) - 1,
                        )

    def _emit(self, ev: dict):
        if len(self._q) < self._qmax:
            self._q.append(ev)

        for cb in list(self._listeners):
            try:
                cb(ev)
            except Exception:
                # swallow-ok: a misbehaving RX listener must not crash
                # the reader thread. Debug-log so a recurring listener
                # bug is diagnosable; warning would spam at packet rate.
                logger.debug(
                    "RX listener raised: cb=%r ev_type=%r",
                    cb, ev.get("type"), exc_info=True,
                )

        if self.on_event and self.on_event not in self._listeners:
            try:
                self.on_event(ev)
            except Exception:
                # swallow-ok: same rationale as the listener loop above.
                logger.debug(
                    "on_event handler raised: ev_type=%r",
                    ev.get("type"), exc_info=True,
                )

    def _fulfill_pending_outcome(self, outcome: SendOutcome) -> None:
        """Hand ``outcome`` to whoever is currently inside :meth:`_send_m2n`.

        The RX reader briefly acquires the Condition's underlying lock so
        the slot read / write is atomic against ``_send_m2n``'s register-
        then-wait. If no send is in flight (``_pending_send_outcome is
        None``) the outcome is dropped silently — this is the "orphan
        TX_DONE from gateway-internal auto-sync" path.
        """
        with self._tx_outcome_cv:
            slot = self._pending_send_outcome
            if slot is None:
                return
            if slot[0] is None:  # don't overwrite a USB_ERROR already set
                slot[0] = outcome
            self._tx_outcome_cv.notify_all()

    def _update_gateway_state(self, state_byte: int, metadata_ms: int) -> None:
        """Mirror the gateway's reported state. Single source of pill truth.

        Callers (the EV_STATE_CHANGED / EV_STATE_REPORT branches) are the
        *only* writers of ``_gateway_state_byte`` per Batch B's "no derived
        state machine" rule.
        """
        self._gateway_state_byte = int(state_byte) & 0xFF
        self._gateway_state_metadata_ms = int(metadata_ms) & 0xFFFF

    @staticmethod
    def _parse_state_event_body(data: bytes) -> tuple[int, int]:
        """Return ``(state_byte, metadata_ms)``. Defensive on short bodies.

        EV_STATE_CHANGED and EV_STATE_REPORT share the body shape:
            byte 0   : state byte (one of GW_STATE_*).
            bytes 1-2: little-endian uint16 metadata (only meaningful for
                       RX_WINDOW; other states leave it 0 or omit it).
        """
        if not data:
            return GATEWAY_STATE_UNKNOWN, 0
        state_byte = data[0]
        metadata_ms = 0
        if len(data) >= 3:
            metadata_ms = u16le(data[1:3])
        elif len(data) == 2:
            metadata_ms = data[1]
        return state_byte, metadata_ms

    def _handle_frame(self, type_byte: int, data: bytes):
        now = time.time()

        # Outcome events — release any pending _send_m2n waiter.
        if type_byte == EV_TX_DONE:
            self._fulfill_pending_outcome(SendOutcome.success())
            ev = {
                "type": EV_TX_DONE,
                "last_len": data[0] if data else 0,
                "ts": now,
            }
            self._emit(ev)
            return

        if type_byte == EV_TX_REJECTED:
            # Body: [type_full, reason_byte]. Tolerate short bodies — older
            # firmware that NACKs without a body still produces a usable
            # REJECTED outcome (reason=UNKNOWN).
            rej_type_full = data[0] if len(data) >= 1 else 0
            reason = data[1] if len(data) >= 2 else TX_REJECT_UNKNOWN
            self._fulfill_pending_outcome(SendOutcome.rejected(reason))
            reason_name = TX_REJECT_REASON_NAME.get(int(reason), "unknown")
            logger.warning(
                "EV_TX_REJECTED type=0x%02X opc=0x%02X reason=0x%02X (%s)",
                rej_type_full, rej_type_full & 0x7F, reason, reason_name,
            )
            ev = {
                "type": EV_TX_REJECTED,
                "type_full": int(rej_type_full),
                "opc": int(rej_type_full) & 0x7F,
                "reason": int(reason),
                "reason_name": reason_name,
                "ts": now,
            }
            self._emit(ev)
            return

        if type_byte == EV_STATE_CHANGED or type_byte == EV_STATE_REPORT:
            state_byte, metadata_ms = self._parse_state_event_body(data)
            self._update_gateway_state(state_byte, metadata_ms)
            ev = {
                "type": type_byte,
                "state_byte": int(state_byte),
                "state": GATEWAY_STATE_NAME.get(int(state_byte), "UNKNOWN"),
                "state_metadata_ms": int(metadata_ms),
                "ts": now,
            }
            self._emit(ev)
            return

        if type_byte == EV_ERROR:
            # Surface USB_ERROR on any pending send — the gateway may still
            # be alive but something downstream broke the contract. The
            # disconnect path uses _handle_disconnect → ``_emit({type:
            # EV_ERROR, data: msg})`` which arrives via this branch on the
            # local self-emit path.
            self._fulfill_pending_outcome(
                SendOutcome.usb_error(detail=(data.decode("utf-8", errors="replace") if data else "")),
            )
            ev = {"type": type_byte, "data": data, "ts": now}
            self._emit(ev)
            return

        # Wire packet (N2M reply forwarded by the gateway).
        if (type_byte & 0x80) != LP.DIR_N2M:
            return

        if len(data) < 10:
            return

        rssi_raw = u16le(data[-3:-1])
        rssi = rssi_raw - 0x10000 if (rssi_raw & 0x8000) else rssi_raw
        snr = struct.unpack("<b", data[-1:])[0]
        ev = parse_reply_event(
            type_byte,
            data,
            timestamp=now,
            host_rssi=rssi,
            host_snr=snr,
        )
        self._emit(ev)

    def drain_events(self, timeout_s: float = 0.0):
        t0 = time.time()
        out = []
        while True:
            if self._q:
                out.append(self._q.pop(0))
            else:
                if time.time() - t0 >= timeout_s:
                    break
                time.sleep(0.01)
        return out
