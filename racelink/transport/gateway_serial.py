"""USB serial transport for the RaceLink gateway."""

from __future__ import annotations

import logging
import threading
import time
import struct

import serial
import serial.tools.list_ports

from .framing import mac_last3_from_hex, u16le
from .gateway_events import EV_ERROR, EV_RX_WINDOW_CLOSED, EV_RX_WINDOW_OPEN, EV_TX_DONE, LP
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

# TX barrier: ``_send_m2n`` waits for the gateway's previous ``EV_TX_DONE``
# before writing the next frame. Without this, the scene runner's offset+
# control fan-out floods USB faster than the gateway can drain its radio
# slot, causing observed packet loss / interleaving. Bounded with a
# timeout so a missing ``EV_TX_DONE`` (gateway reset, USB hiccup) cannot
# deadlock the transport — we log a warning and proceed.
#
# Timeout sizes to the *previous* packet's body length (that's what's
# currently being transmitted while we wait): a fixed floor covers small
# packets, plus a per-byte component for OPC_STREAM-sized payloads which
# can be ~9 B per chunk × up to 16 chunks. A 22 B max OPC_CONTROL body
# at SF8/250 kHz/CR4:5 has ~80 ms airtime; SF12 worst case is ~600 ms.
# With ``per_byte = 0.012 s`` and ``floor = 0.4 s`` we cover SF8-SF10
# easily and SF12 degrades to a warning + proceed.
TX_BARRIER_FLOOR_S = 0.4
TX_BARRIER_BASE_S = 0.15
TX_BARRIER_PER_BYTE_S = 0.012


def _tx_barrier_timeout_for(body_len: int) -> float:
    """Compute the wait timeout in seconds for the previous packet's TX.

    ``body_len`` is the *previous* send's body byte count. Returned value
    is at least ``TX_BARRIER_FLOOR_S`` and scales linearly above that.
    """
    return max(
        TX_BARRIER_FLOOR_S,
        TX_BARRIER_BASE_S + TX_BARRIER_PER_BYTE_S * max(0, int(body_len)),
    )


# Back-compat alias kept for any caller (or test) that pinned the old
# constant. New code should use ``_tx_barrier_timeout_for(...)``.
TX_BARRIER_TIMEOUT_S = TX_BARRIER_FLOOR_S


class GatewaySerialTransport:
    """
    Thin USB transport mirroring racelink_proto on Host<->Device.

    Framing v1.1: [0x00][LEN][TYPE][DATA...]
      Host->Device DATA: [recv3(3)][Body]
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
        self.ser.timeout = 0.2
        self._stop = False
        self._rx_thread = None
        self._q = []
        self._qmax = 1000
        self._listeners = []
        self._tx_listeners = []
        self._rx_window_state = 0
        # ----- TX flow-control + serialization (A1 + A2) -----------------
        # ``_tx_lock`` serializes concurrent ``_send_m2n`` callers so the
        # USB byte stream can never interleave frames from web threads,
        # the scene runner, the auto-restore worker, and discovery timers.
        # Without it two writes can race past the previous (Event-based)
        # barrier and intermix on the wire.
        #
        # ``_tx_done_cv`` shares the same lock as a Condition so the RX
        # reader thread can briefly acquire it to flip the predicate
        # ``_tx_in_flight`` when ``EV_TX_DONE`` / ``EV_ERROR`` arrives.
        # Using ``wait_for(predicate)`` instead of the old
        # ``Event.wait`` + ``Event.clear`` pair eliminates the lost-wakeup
        # window where a stale ``set()`` from the RX thread could be
        # consumed before the next caller's ``wait`` started.
        #
        # Invariant: ``_tx_in_flight`` is True exactly between the moment
        # ``_send_m2n`` writes the frame and the moment the gateway
        # signals completion (or an EV_ERROR reports a failed TX). Both
        # transitions happen under ``_tx_lock``.
        self._tx_lock = threading.Lock()
        self._tx_done_cv = threading.Condition(self._tx_lock)
        self._tx_in_flight = False
        self._last_tx_body_len = 0
        # Set to True by ``discover_and_open`` when at least one candidate
        # port was skipped because another process held its exclusive lock.
        # Controllers use this to pick PORT_BUSY over NOT_FOUND when
        # ``discover_and_open`` returns False.
        self.last_discovery_had_busy_port = False

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

    def start(self):
        if not self.ser.is_open:
            self.open()
        self._stop = False
        import threading

        self._rx_thread = threading.Thread(target=self._reader, daemon=True)
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
                # swallow-ok: best-effort fallback; caller proceeds with safe default
                pass

    def _handle_disconnect(self, msg: str) -> None:
        logger.warning(msg)
        self._emit({"type": EV_ERROR, "data": msg})
        self._stop = True
        try:
            self.ser.close()
        except Exception:
            # swallow-ok: best-effort fallback; caller proceeds with safe default
            pass

    def _send_m2n(self, type_full: int, recv3: bytes, body: bytes = b""):
        if len(recv3) != 3:
            raise ValueError("recv3 must be 3 bytes")
        payload = bytes([type_full]) + recv3 + (body or b"")
        frame = bytes([0x00, len(payload)]) + payload

        # Two responsibilities serialized by ``_tx_lock``:
        #   1. TX serialization (A1): only one caller writes to the USB
        #      device at a time. Without this two threads can race past
        #      the barrier and intermix bytes mid-frame on the wire.
        #   2. TX barrier (A2): wait until the gateway has signalled
        #      ``EV_TX_DONE`` for the previous frame. The Condition's
        #      predicate is ``not self._tx_in_flight``, which the RX
        #      reader flips under the same lock — no lost-wakeup window.
        #
        # On timeout we proceed anyway: same best-effort semantics as the
        # original Event-based barrier (raising would surprise callers
        # that treat ``_send_m2n`` as fire-and-forget). The predicate is
        # left True in that case; the next caller will wait the full
        # timeout again, which is the desired conservative recovery.
        timeout_s = _tx_barrier_timeout_for(self._last_tx_body_len)
        prev_body_len = self._last_tx_body_len  # for the warning log only

        # ``tx_event`` is filled inside the lock if the write succeeds
        # and emitted *outside* the lock so a slow TX listener cannot
        # block other senders behind us. Same for ``disconnect_msg`` —
        # ``_handle_disconnect`` fires EV_ERROR through the listener fan-
        # out, which historically triggered cascading work; running it
        # outside the critical section keeps lock-hold time bounded.
        tx_event: dict | None = None
        disconnect_msg: str | None = None

        with self._tx_lock:
            if self._tx_in_flight:
                ok = self._tx_done_cv.wait_for(
                    lambda: not self._tx_in_flight,
                    timeout=timeout_s,
                )
                if not ok:
                    logger.warning(
                        "TX barrier timeout (%.0f ms, prev body=%d B) — "
                        "proceeding without prior EV_TX_DONE. Possible "
                        "gateway stall or lost event.",
                        timeout_s * 1000,
                        prev_body_len,
                    )
            self._tx_in_flight = True
            self._last_tx_body_len = len(body or b"")
            try:
                self.ser.write(frame)
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
            except serial.SerialException as e:
                # No EV_TX_DONE will arrive for a frame that never made
                # it onto the wire. Release the barrier under the lock so
                # any waiter that joined after us proceeds immediately
                # rather than waiting the full timeout.
                self._tx_in_flight = False
                self._tx_done_cv.notify_all()
                disconnect_msg = f"USB TX failed: {e}"

        # Listener fan-out runs outside the lock. ``_emit_tx`` invokes
        # caller-supplied callbacks that we do not control; keeping them
        # off the TX critical path means a slow listener can never block
        # subsequent sends. Ordering is preserved: the frame is on the
        # wire before any listener observes the TX_M2N event because the
        # write happened under the lock above.
        if tx_event is not None:
            self._emit_tx(tx_event)

        if disconnect_msg is not None:
            self._handle_disconnect(disconnect_msg)
            return False
        return True

    def send_get_devices(self, recv3=b"\xFF\xFF\xFF", group_id=0, flags=0):
        body = build_get_devices_body(group_id=group_id, flags=flags)
        self._send_m2n(LP.make_type(LP.DIR_M2N, LP.OPC_DEVICES), recv3, body)

    def send_set_group(self, recv3: bytes, group_id: int):
        body = build_set_group_body(group_id)
        self._send_m2n(LP.make_type(LP.DIR_M2N, LP.OPC_SET_GROUP), recv3, body)

    def send_preset(self, recv3: bytes, group_id: int, flags: int, preset_id: int, brightness: int):
        """Send an OPC_PRESET packet (4 B fixed, pre-rename: OPC_CONTROL)."""
        body = build_preset_body(group_id=group_id, flags=flags, preset_id=preset_id, brightness=brightness)
        self._send_m2n(LP.make_type(LP.DIR_M2N, LP.OPC_PRESET), recv3, body)

    def send_control(self, recv3: bytes, group_id: int, flags: int, **params):
        """Send an OPC_CONTROL packet (variable-length body, 3..21 B).

        Forwards all kwargs to ``build_control_body``; see that builder for
        the accepted field names (brightness, mode, speed, intensity, custom1,
        custom2, custom3, check1/2/3, palette, color1/2/3). Body length varies
        with which fields are provided and is framed via the generic
        ``_send_m2n`` path, which already handles variable-length bodies.
        (Pre-rename: ``send_control_adv`` / OPC_CONTROL_ADV.)
        """
        body = build_control_body(group_id=group_id, flags=flags, **params)
        self._send_m2n(LP.make_type(LP.DIR_M2N, LP.OPC_CONTROL), recv3, body)

    def send_config(self, recv3: bytes = b"\xFF\xFF\xFF", option: int = 0, data0: int = 0, data1: int = 0, data2: int = 0, data3: int = 0):
        body = build_config_body(option=option, data0=data0, data1=data1, data2=data2, data3=data3)
        self._send_m2n(LP.make_type(LP.DIR_M2N, LP.OPC_CONFIG), recv3, body)

    def send_sync(self, recv3: bytes = b"\xFF\xFF\xFF", ts24: int = 0, brightness: int = 0):
        body = build_sync_body(ts24=ts24, brightness=brightness)
        self._send_m2n(LP.make_type(LP.DIR_M2N, LP.OPC_SYNC), recv3, body)

    def send_offset(self, recv3: bytes = b"\xFF\xFF\xFF", group_id: int = 0,
                    mode="none", **mode_params):
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
        self._send_m2n(LP.make_type(LP.DIR_M2N, LP.OPC_OFFSET), recv3, body)

    def send_stream(self, recv3: bytes, payload: bytes):
        """Send one logical stream payload to the gateway.

        The gateway is responsible for splitting the payload into radio-sized
        packets, adding per-packet stream control bytes, and padding as needed.
        """
        self._send_m2n(LP.make_type(LP.DIR_M2N, LP.OPC_STREAM), recv3, bytes(payload or b""))

    def send_wled_preset(self, recv3: bytes, group_id: int, state: int, preset_id: int, brightness: int):
        """Legacy transport helper: send a WLED preset with a simple on/off state flag.

        ``preset_id`` (pre-rename: ``effect``) is the WLED preset number written
        into ``P_Preset.presetId``. The old name was misleading: OPC_PRESET
        carries a preset id, not a WLED effect-mode index — those live on
        OPC_CONTROL.
        """
        flags = 0x01 if int(state) else 0x00
        self.send_preset(recv3, group_id, flags, int(preset_id), int(brightness))

    def send_get_status(self, recv3=b"\xFF\xFF\xFF", group_id=0, flags=0):
        body = struct.pack("<BB", group_id & 0xFF, flags & 0xFF)
        self._send_m2n(LP.make_type(LP.DIR_M2N, LP.OPC_STATUS), recv3, body)

    def _reader(self):
        in_frame = False
        need = 0
        buf = bytearray()
        while not self._stop:
            try:
                b = self.ser.read(1)
            except serial.SerialException as e:
                self._handle_disconnect(f"USB serial disconnected: {e}")
                break
            if not b:
                continue
            x = b[0]
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
                self._handle_frame(buf[0], memoryview(buf)[1:].tobytes())

    def _emit(self, ev: dict):
        if len(self._q) < self._qmax:
            self._q.append(ev)

        for cb in list(self._listeners):
            try:
                cb(ev)
            except Exception:
                # swallow-ok: best-effort fallback; caller proceeds with safe default
                pass

        if self.on_event and self.on_event not in self._listeners:
            try:
                self.on_event(ev)
            except Exception:
                # swallow-ok: best-effort fallback; caller proceeds with safe default
                pass

    def _update_rx_window_state(self, event_type: int) -> int:
        """Track RX window state from OPEN/CLOSED events idempotently.

        The gateway firmware emits ``EV_RX_WINDOW_OPEN`` both when entering a
        Timed RX (window_ms>0) and when entering Continuous RX (window_ms=0).
        ``EV_RX_WINDOW_CLOSED`` is only sent when a Timed window ends; the
        Continuous -> TX transition is silent by design (Continuous is the
        resting state). That means two OPEN events can legitimately arrive
        back-to-back across a TX cycle. Treat the events as state signals
        (OPEN -> 1, CLOSED -> 0) rather than counter deltas so we do not emit
        spurious error-level log records that RotorHazard surfaces as UI
        alerts.
        """
        if event_type == EV_RX_WINDOW_OPEN:
            self._rx_window_state = 1
        elif event_type == EV_RX_WINDOW_CLOSED:
            self._rx_window_state = 0
        return self._rx_window_state

    @property
    def rx_window_state(self) -> int:
        return int(self._rx_window_state)

    def _handle_frame(self, type_byte: int, data: bytes):
        now = time.time()

        if type_byte in (EV_ERROR, EV_RX_WINDOW_OPEN, EV_RX_WINDOW_CLOSED, EV_TX_DONE):
            if type_byte == EV_RX_WINDOW_OPEN and len(data) >= 2:
                rx_state = self._update_rx_window_state(type_byte)
                ev = {"type": type_byte, "window_ms": u16le(data[:2]), "ts": now, "rx_windows": rx_state}
            elif type_byte == EV_RX_WINDOW_CLOSED and len(data) >= 2:
                rx_state = self._update_rx_window_state(type_byte)
                ev = {"type": type_byte, "rx_count_delta": u16le(data[:2]), "ts": now, "rx_windows": rx_state}
            elif type_byte == EV_TX_DONE and len(data) >= 1:
                ev = {"type": type_byte, "last_len": data[0], "ts": now, "rx_windows": self.rx_window_state}
            else:
                ev = {"type": type_byte, "data": data, "ts": now, "rx_windows": self.rx_window_state}
            # Release the TX barrier on either success or error so a queued
            # ``_send_m2n`` can proceed. EV_RX_WINDOW_* events are unrelated
            # to TX completion and intentionally do NOT release the barrier.
            #
            # The Condition's lock is the same ``_tx_lock`` that serializes
            # writes; acquiring it here briefly is safe because the TX path
            # always releases it before its blocking ``ser.write`` returns
            # (we only hold the lock across the OS-level write call, which
            # is fast on a USB CDC link).
            if type_byte == EV_TX_DONE or type_byte == EV_ERROR:
                with self._tx_lock:
                    self._tx_in_flight = False
                    self._tx_done_cv.notify_all()
            self._emit(ev)
            return

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
            rx_windows=self.rx_window_state,
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
