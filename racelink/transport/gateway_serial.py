"""USB serial transport for the RaceLink gateway."""

from __future__ import annotations

import logging
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
    build_set_group_body,
    build_sync_body,
)

logger = logging.getLogger("racelink_transport")


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
            pass
        return False

    def discover_and_open(self) -> bool:
        if self.port:
            try:
                self.ser.port = self.port
                try:
                    self.ser.exclusive = True  # type: ignore[attr-defined]
                except Exception:
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
                else:
                    logger.debug("Open/identify failed on %s: %s", p.device, msg)
                try:
                    if getattr(self.ser, "is_open", False):
                        self.ser.close()
                except Exception:
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
            pass

    def add_listener(self, cb):
        if cb and cb not in self._listeners:
            self._listeners.append(cb)

    def remove_listener(self, cb):
        try:
            if cb in self._listeners:
                self._listeners.remove(cb)
        except Exception:
            pass

    def add_tx_listener(self, cb):
        if cb and cb not in self._tx_listeners:
            self._tx_listeners.append(cb)

    def remove_tx_listener(self, cb):
        try:
            if cb in self._tx_listeners:
                self._tx_listeners.remove(cb)
        except Exception:
            pass

    def _emit_tx(self, ev: dict):
        for cb in list(self._tx_listeners):
            try:
                cb(ev)
            except Exception:
                pass

    def _handle_disconnect(self, msg: str) -> None:
        logger.warning(msg)
        self._emit({"type": EV_ERROR, "data": msg})
        self._stop = True
        try:
            self.ser.close()
        except Exception:
            pass

    def _send_m2n(self, type_full: int, recv3: bytes, body: bytes = b""):
        if len(recv3) != 3:
            raise ValueError("recv3 must be 3 bytes")
        payload = bytes([type_full]) + recv3 + (body or b"")
        frame = bytes([0x00, len(payload)]) + payload
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
            self._emit_tx(
                {
                    "type": "TX_M2N",
                    "type_full": type_full,
                    "dir": type_full & 0x80,
                    "opc": type_full & 0x7F,
                    "recv3": recv3,
                    "body_len": len(body or b""),
                }
            )
        except serial.SerialException as e:
            self._handle_disconnect(f"USB TX failed: {e}")
            return False
        return True

    def send_get_devices(self, recv3=b"\xFF\xFF\xFF", group_id=0, flags=0):
        body = build_get_devices_body(group_id=group_id, flags=flags)
        self._send_m2n(LP.make_type(LP.DIR_M2N, LP.OPC_DEVICES), recv3, body)

    def send_set_group(self, recv3: bytes, group_id: int):
        body = build_set_group_body(group_id)
        self._send_m2n(LP.make_type(LP.DIR_M2N, LP.OPC_SET_GROUP), recv3, body)

    def send_control(self, recv3: bytes, group_id: int, flags: int, preset_id: int, brightness: int):
        body = build_control_body(group_id=group_id, flags=flags, preset_id=preset_id, brightness=brightness)
        self._send_m2n(LP.make_type(LP.DIR_M2N, LP.OPC_CONTROL), recv3, body)

    def send_config(self, recv3: bytes = b"\xFF\xFF\xFF", option: int = 0, data0: int = 0, data1: int = 0, data2: int = 0, data3: int = 0):
        body = build_config_body(option=option, data0=data0, data1=data1, data2=data2, data3=data3)
        self._send_m2n(LP.make_type(LP.DIR_M2N, LP.OPC_CONFIG), recv3, body)

    def send_sync(self, recv3: bytes = b"\xFF\xFF\xFF", ts24: int = 0, brightness: int = 0):
        body = build_sync_body(ts24=ts24, brightness=brightness)
        self._send_m2n(LP.make_type(LP.DIR_M2N, LP.OPC_SYNC), recv3, body)

    def send_stream(self, recv3: bytes, payload: bytes):
        """Send one logical stream payload to the gateway.

        The gateway is responsible for splitting the payload into radio-sized
        packets, adding per-packet stream control bytes, and padding as needed.
        """
        self._send_m2n(LP.make_type(LP.DIR_M2N, LP.OPC_STREAM), recv3, bytes(payload or b""))

    def send_wled_control(self, recv3: bytes, group_id: int, state: int, effect: int, brightness: int):
        flags = 0x01 if int(state) else 0x00
        self.send_control(recv3, group_id, flags, int(effect), int(brightness))

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
                pass

        if self.on_event and self.on_event not in self._listeners:
            try:
                self.on_event(ev)
            except Exception:
                pass

    def _update_rx_window_state(self, event_type: int) -> int:
        delta = 1 if event_type == EV_RX_WINDOW_OPEN else -1
        new_state = int(self._rx_window_state) + delta
        if new_state not in (0, 1):
            logger.error(
                "RX window state invalid after %s: %s -> %s",
                "OPEN" if event_type == EV_RX_WINDOW_OPEN else "CLOSED",
                self._rx_window_state,
                new_state,
            )
            new_state = 1 if event_type == EV_RX_WINDOW_OPEN else 0
        self._rx_window_state = new_state
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
