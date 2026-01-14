
# gc_transport.py -- USB transport for LoRaProto host<->communicator (v1.1 framing)
# Keeps Identify "GateCommunicator_v4" compatible for port discovery.

import time
import struct
import logging
logger = logging.getLogger('gc_transport')
import serial
import serial.tools.list_ports

try:
    # Optional: pull constants directly from auto-generated header mirror if present
    import lora_proto_auto as LPA
    _HAVE_AUTO = True
except Exception:
    _HAVE_AUTO = False

class LP:
    # Defaults (optionally overridden from lora_proto_auto if available)
    DIR_M2N = 0x00
    DIR_N2M = 0x80

    # Opcodes (7-bit, shared)
    OPC_DEVICES   = 0x01
    OPC_SET_GROUP = 0x02
    OPC_STATUS    = 0x03
    OPC_CONTROL   = 0x04
    OPC_CONFIG    = 0x05
    OPC_SYNC      = 0x06
    OPC_STREAM    = 0x07
    OPC_ACK       = 0x7E

    @staticmethod
    def make_type(direction:int, opcode7:int) -> int:
        return (direction | (opcode7 & 0x7F))

# If header mirror exists, override
if _HAVE_AUTO:
    try:
        LP.DIR_M2N = getattr(LPA, "DIR_M2N", LP.DIR_M2N)
        LP.DIR_N2M = getattr(LPA, "DIR_N2M", LP.DIR_N2M)
        LP.OPC_DEVICES   = getattr(LPA, "OPC_DEVICES", LP.OPC_DEVICES)
        LP.OPC_SET_GROUP = getattr(LPA, "OPC_SET_GROUP", LP.OPC_SET_GROUP)
        LP.OPC_STATUS    = getattr(LPA, "OPC_STATUS", LP.OPC_STATUS)
        # renamed opcode in proto v1.2: OPC_CONTROL (old: OPC_WLED_CONTROL)
        LP.OPC_CONTROL   = getattr(LPA, "OPC_CONTROL", getattr(LPA, "OPC_WLED_CONTROL", LP.OPC_CONTROL))
        LP.OPC_CONFIG    = getattr(LPA, "OPC_CONFIG", LP.OPC_CONFIG)
        LP.OPC_SYNC      = getattr(LPA, "OPC_SYNC", LP.OPC_SYNC)
        LP.OPC_STREAM    = getattr(LPA, "OPC_STREAM", LP.OPC_STREAM)
        LP.OPC_ACK       = getattr(LPA, "OPC_ACK", LP.OPC_ACK)

        make_type = getattr(LPA, "make_type", LP.make_type)
        def _make_type(direction, opcode): return make_type(direction, opcode)
        LP.make_type = staticmethod(_make_type)
    except Exception:
        pass

# USB-only event types from device (not LoRa types)
EV_ERROR            = 0xF0
EV_RX_WINDOW_OPEN   = 0xF1
EV_RX_WINDOW_CLOSED = 0xF2
EV_TX_DONE          = 0xF3

def _u16le(b2:bytes) -> int:
    return b2[0] | (b2[1] << 8)

def _mac_last3_from_hex(mac12:str) -> bytes:
    mac = (mac12 or "").strip().replace(":","").upper()
    if len(mac) < 6: mac = "FFFFFF"
    return bytes.fromhex(mac[-6:])

class LoRaUSB:
    """
    Thin USB transport mirroring lora_proto on Host<->Device.

    Framing v1.1: [0x00][LEN][TYPE][DATA...]
      Host->Device DATA: [recv3(3)][Body]
      Device->Host DATA: [Header7(7)][Body][RSSI(LE16)][SNR(i8)] for LoRa replies,
                         or payload per EV_* for USB events.
    """
    def __init__(self, port:str=None, baud:int=921600, on_event=None):
        self.port = port
        self.baud = baud
        self.ident_mac = None  # Identified MAC from device (if any)
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

    @staticmethod
    def _is_usb_port(portinfo):
        """Return True if the given ListPortInfo looks like a USB serial adapter."""
        try:
            # On Linux, /dev/ttyUSB* or /dev/ttyACM* are typical USB serial
            dev = (getattr(portinfo, "device", "") or "").lower()
            if dev.startswith("/dev/ttyusb") or dev.startswith("/dev/ttyacm"):
                return True
            # Many platforms expose vid/pid for USB devices
            if getattr(portinfo, "vid", None) is not None or getattr(portinfo, "pid", None) is not None:
                return True
            # Fallback: description contains 'USB'
            desc = (getattr(portinfo, "description", "") or "").upper()
            if "USB" in desc:
                return True
        except Exception:
            pass
        return False
    
    # Keep legacy ASCII identify "GateCommunicator_v4" for discovery
    
    def discover_and_open(self) -> bool:
        # Explizit gesetzter Port? -> direkt öffnen, kein Scan
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
                    pretty = f"Port busy: {self.port} is in use by another process (exclusive lock failed). Close the other program (e.g. esptool, screen/minicom) and retry."
                    logger.warning(pretty)
                    raise serial.SerialException(pretty) from e
                raise

        # Kein Port vorgegeben -> nur USB-Ports scannen
        payload = struct.pack(">BBBB", 0x00, 0x01, 1, 0xFF)  # IDENTIFY legacy
        ident = b"GateCommunicator_v4"

        for p in serial.tools.list_ports.comports():
            if not self._is_usb_port(p):
                continue

            # WICHTIG: Kein finally:close() hier, wir schließen nur bei Nicht-Treffer
            try:
                self.ser.port = p.device
                try:
                    self.ser.exclusive = True  # type: ignore[attr-defined]
                except Exception:
                    pass
                self.ser.open()
                time.sleep(1.0)
                self.ser.reset_input_buffer()
                self.ser.write(payload)
                # etwas Puffer, z. B. "GateCommunicator_v4" + MAC (ohne \r\n)
                resp = self.ser.read(len(ident) + 17)

                if not resp:
                    logger.debug("Port %s: no identify response", p.device)
                    self.ser.close()
                    continue

                if resp.startswith(ident):
                    # MATCH -> Port GEÖFFNET LASSEN, kein Close!
                    self.port = p.device

                    # ---- MAC extrahieren (ASCII hinter dem Ident-String), ohne CR/LF/NULLs
                    mac_ascii = ""
                    try:
                        mac_ascii = resp[len(ident):].decode('ascii', errors='ignore').strip().strip('\x00')
                    except Exception:
                        mac_ascii = ""

                    # transport-weit verfügbar machen (für UI/Logs im Host)
                    self.ident_mac = mac_ascii if mac_ascii else None

                    if self.ident_mac:
                        logger.info("Identify matched on %s (MAC: %s)", p.device, self.ident_mac)
                    else:
                        # Fallback wie früher, falls mal keine MAC kommt
                        logger.info("Identify matched on %s (%r)", p.device, resp[:len(ident)+6])

                    return True

                logger.debug("Port %s: unexpected identify reply: %r", p.device, resp[:32])
                self.ser.close()

            except serial.SerialException as e:
                msg = str(e)
                if "Could not exclusively lock port" in msg or "Resource temporarily unavailable" in msg:
                    logger.debug("Skip busy port %s (exclusive lock failed)", p.device)
                else:
                    logger.debug("Open/identify failed on %s: %s", p.device, msg)
                # sicherheitshalber schließen, falls geöffnet
                try: 
                    if getattr(self.ser, "is_open", False): self.ser.close()
                except Exception:
                    pass

        return False


    def open(self):
        if not self.port: raise RuntimeError("LoRaUSB: no port set")
        self.ser.port = self.port
        if not self.ser.is_open: self.ser.open()

    def start(self):
        if not self.ser.is_open: self.open()
        self._stop = False
        import threading
        self._rx_thread = threading.Thread(target=self._reader, daemon=True)
        self._rx_thread.start()

    def close(self):
        self._stop = True
        if self._rx_thread: self._rx_thread.join(timeout=1.0)
        try: self.ser.close()
        except: pass

    # ---------------- Host -> Device ----------------
    # ---------------- Listeners ----------------
    def add_listener(self, cb):
        """Add a receive/event listener (non-destructive; multiple listeners supported)."""
        if cb and cb not in self._listeners:
            self._listeners.append(cb)

    def remove_listener(self, cb):
        try:
            if cb in self._listeners:
                self._listeners.remove(cb)
        except Exception:
            pass

    def add_tx_listener(self, cb):
        """Add a TX listener called for every Host->Device send."""
        if cb and cb not in self._tx_listeners:
            self._tx_listeners.append(cb)

    def remove_tx_listener(self, cb):
        try:
            if cb in self._tx_listeners:
                self._tx_listeners.remove(cb)
        except Exception:
            pass

    def _emit_tx(self, ev:dict):
        for cb in list(self._tx_listeners):
            try:
                cb(ev)
            except Exception:
                pass

    def _send_m2n(self, type_full:int, recv3:bytes, body:bytes=b""):
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
                (type_full & 0x7F),
                recv3.hex().upper(),
                len(body),
                (body or b"").hex().upper()
            )
            self._emit_tx({"type": "TX_M2N", "type_full": type_full, "dir": (type_full & 0x80), "opc": (type_full & 0x7F), "recv3": recv3, "body_len": len(body or b"")})
        except serial.SerialException as e:
            logger.error("TX write failed: %s", e)
            raise

    def send_get_devices(self, recv3=b'\xFF\xFF\xFF', group_id=0, flags=0):
        body = struct.pack("<BB", group_id & 0xFF, flags & 0xFF)  # P_GetDevices
        self._send_m2n(LP.make_type(LP.DIR_M2N, LP.OPC_DEVICES), recv3, body)

    def send_set_group(self, recv3:bytes, group_id:int):
        body = struct.pack("<B", group_id & 0xFF)                 # P_SetGroup
        self._send_m2n(LP.make_type(LP.DIR_M2N, LP.OPC_SET_GROUP), recv3, body)
        
    def send_control(self, recv3:bytes, group_id:int, flags:int, preset_id:int, brightness:int):
        """Send CONTROL (4B): groupId, flags, presetId, brightness."""
        body = struct.pack(
            "<BBBB",
            int(group_id) & 0xFF,
            int(flags) & 0xFF,
            int(preset_id) & 0xFF,
            int(brightness) & 0xFF,
        )
        self._send_m2n(LP.make_type(LP.DIR_M2N, LP.OPC_CONTROL), recv3, body)

    def send_config(self, recv3:bytes=b'\xFF\xFF\xFF', option:int=0, data0:int=0, data1:int=0, data2:int=0, data3:int=0):
        """Send CONFIG (5B): option, data0..data3."""
        body = struct.pack(
            "<BBBBB",
            int(option) & 0xFF,
            int(data0) & 0xFF,
            int(data1) & 0xFF,
            int(data2) & 0xFF,
            int(data3) & 0xFF,
        )
        self._send_m2n(LP.make_type(LP.DIR_M2N, LP.OPC_CONFIG), recv3, body)

    def send_sync(self, recv3:bytes=b'\xFF\xFF\xFF', ts24:int=0, brightness:int=0):
        """Send SYNC (4B): ts24(LE24), brightness."""
        ts = int(ts24) & 0xFFFFFF
        body = bytes([(ts & 0xFF), ((ts >> 8) & 0xFF), ((ts >> 16) & 0xFF), (int(brightness) & 0xFF)])
        self._send_m2n(LP.make_type(LP.DIR_M2N, LP.OPC_SYNC), recv3, body)

    def send_stream(self, recv3:bytes, ctrl:int, data:bytes):
        """Send STREAM_M2N (9B): ctrl byte + 8 data bytes."""
        if not isinstance(data, (bytes, bytearray)):
            raise ValueError("data must be bytes")
        if len(data) != 8:
            raise ValueError("data must be exactly 8 bytes")
        body = struct.pack("<B", int(ctrl) & 0xFF) + bytes(data)
        self._send_m2n(LP.make_type(LP.DIR_M2N, LP.OPC_STREAM), recv3, body)

    # Backwards helper (old opcode name), kept to avoid breaking older host code.
    # Prefer send_control() to fully control flags.
    def send_wled_control(self, recv3:bytes, group_id:int, state:int, effect:int, brightness:int):
        flags = (0x01 if int(state) else 0x00)  # POWER_ON bit (bit layout defined in host/plugin)
        self.send_control(recv3, group_id, flags, int(effect), int(brightness))

    def send_get_status(self, recv3=b'\xFF\xFF\xFF', group_id=0, flags=0):
        body = struct.pack("<BB", group_id & 0xFF, flags & 0xFF)  # P_GetStatus
        self._send_m2n(LP.make_type(LP.DIR_M2N, LP.OPC_STATUS), recv3, body)

    # ---------------- Device -> Host ----------------
    def _reader(self):
        in_frame = False; need = 0; buf = bytearray()
        while not self._stop:
            b = self.ser.read(1)
            if not b: 
                continue
            x = b[0]
            if not in_frame:
                if x == 0x00:
                    in_frame = True; need = 0; buf.clear()
                continue
            if need == 0:
                need = x; continue
            buf.append(x)
            if len(buf) == need:
                in_frame = False
                self._handle_frame(buf[0], memoryview(buf)[1:].tobytes())

    def _emit(self, ev:dict):
        if len(self._q) < self._qmax:
            self._q.append(ev)

        # Non-destructive listeners (preferred)
        for cb in list(self._listeners):
            try:
                cb(ev)
            except Exception:
                pass

        # Legacy single callback (kept for backwards compatibility)
        if self.on_event and self.on_event not in self._listeners:
            try:
                self.on_event(ev)
            except Exception:
                pass

    def _handle_frame(self, type_byte:int, data:bytes):
        """Parse one framed message from device and emit events.

        Framing v1.1 delivers TYPE + DATA, where TYPE is either:
        - EV_* (USB-only event codes), or
        - DIR_N2M|OPC_* (LoRa reply from node to master to host)
        """
        now = time.time()

        # USB-only events
        if type_byte in (EV_ERROR, EV_RX_WINDOW_OPEN, EV_RX_WINDOW_CLOSED, EV_TX_DONE):
            if type_byte == EV_RX_WINDOW_OPEN and len(data) >= 2:
                ev = {"type": type_byte, "window_ms": _u16le(data[:2]), "ts": now}
            elif type_byte == EV_RX_WINDOW_CLOSED and len(data) >= 2:
                ev = {"type": type_byte, "rx_count_delta": _u16le(data[:2]), "ts": now}
            elif type_byte == EV_TX_DONE and len(data) >= 1:
                ev = {"type": type_byte, "last_len": data[0], "ts": now}
            else:
                ev = {"type": type_byte, "data": data, "ts": now}
            self._emit(ev)
            return

        # LoRa replies (TYPE = DIR_N2M | opcode7)
        if (type_byte & 0x80) != LP.DIR_N2M:
            return

        # Expect: Header7(7) + Body + RSSI(LE16) + SNR(i8)
        if len(data) < 10:
            return

        hdr = data[:7]
        body = data[7:-3]
        rssi_raw = _u16le(data[-3:-1])
        rssi = rssi_raw - 0x10000 if (rssi_raw & 0x8000) else rssi_raw
        snr = struct.unpack("<b", data[-1:])[0]

        sender3 = bytes(hdr[0:3])
        receiver3 = bytes(hdr[3:6])
        opc = (type_byte & 0x7F)

        ev = {
            "type": type_byte,   # keep legacy behavior (numeric)
            "dir": (type_byte & 0x80),
            "opc": opc,
            "sender3": sender3,
            "receiver3": receiver3,
            "host_rssi": rssi,
            "host_snr": snr,
            "ts": now,
        }

        # Compatibility parsing for common replies
        if opc == LP.OPC_DEVICES:
            # IDENTIFY_REPLY = 9B: [version, caps, groupId, mac6]
            if len(body) == 9:
                ev.update({
                    "reply": "IDENTIFY_REPLY",
                    "version": body[0],
                    "caps": body[1],
                    "groupId": body[2],
                    "mac6": bytes(body[3:9]),
                })
            else:
                ev.update({"reply": "IDENTIFY_REPLY", "body_raw": body})

        elif opc == LP.OPC_STATUS:
            # STATUS_REPLY = 8B: [flags, configByte, presetId, brightness, vbat_mV(LE16), rssi_node(i8), snr_node(i8)]
            if len(body) == 8:
                flags, config_byte, presetId, brightness, vbat_mV, rssi_node, snr_node = struct.unpack("<BBBBHbb", body)
                ev.update({
                    "reply": "STATUS_REPLY",
                    "flags": flags,
                    "configByte": config_byte,
                    "presetId": presetId,
                    "brightness": brightness,
                    "vbat_mV": vbat_mV,
                    "node_rssi": rssi_node,
                    "node_snr": snr_node,
                })
            elif len(body) == 7:
                flags, presetId, brightness, vbat_mV, rssi_node, snr_node = struct.unpack("<BBBHbb", body)
                ev.update({
                    "reply": "STATUS_REPLY",
                    "flags": flags,
                    "configByte": 0,
                    "presetId": presetId,
                    "brightness": brightness,
                    "vbat_mV": vbat_mV,
                    "node_rssi": rssi_node,
                    "node_snr": snr_node,
                })
            else:
                ev.update({"reply": "STATUS_REPLY", "body_raw": body})

        elif opc == LP.OPC_ACK:
            # ACK = min 2B: [ack_of(opc7), status]
            if len(body) >= 2:
                ack_of = body[0] & 0x7F
                ack_status = body[1]
                ev.update({"reply": "ACK", "ack_of": ack_of, "ack_status": ack_status})
                if len(body) >= 3:
                    ev.update({"ack_seq": body[2]})
            else:
                ev.update({"reply": "ACK", "body_raw": body})

        else:
            ev.update({"reply": "OTHER", "body_raw": body})

        self._emit(ev)

    def drain_events(self, timeout_s:float=0.0):
        t0 = time.time(); out = []
        while True:
            if self._q: out.append(self._q.pop(0))
            else:
                if time.time() - t0 >= timeout_s: break
                time.sleep(0.01)
        return out
