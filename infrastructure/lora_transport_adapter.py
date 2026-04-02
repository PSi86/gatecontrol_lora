from __future__ import annotations

import logging
import threading
import time

from .. import lora_proto_auto as LPA
from ..data import create_device, rl_devicelist
from ..racelink_transport import EV_ERROR, EV_RX_WINDOW_CLOSED, EV_RX_WINDOW_OPEN, LP, LoRaUSB

logger = logging.getLogger(__name__)


class LoRaTransportAdapter:
    def __init__(self, rhapi, get_device_by_address, on_status_update, on_identify_update, on_disconnect):
        self._rhapi = rhapi
        self._get_device = get_device_by_address
        self._on_status_update = on_status_update
        self._on_identify_update = on_identify_update
        self._on_disconnect = on_disconnect
        self.lora = None
        self.lp = LP
        self._transport_hooks_installed = False
        self._pending_expect = None
        self._reconnect_in_progress = False
        self._last_reconnect_ts = 0.0
        self._last_error_notify_ts = 0.0

    def discover_port(self, args):
        port = self._rhapi.db.option("psi_comms_port", None)
        try:
            self._transport_hooks_installed = False
            self.lora = LoRaUSB(port=port, on_event=None)
            ok = self.lora.discover_and_open()
            if ok:
                self.lora.start()
                self.install_hooks()
                used = self.lora.port or "unknown"
                mac = getattr(self.lora, "ident_mac", None)
                if mac and "manual" in args:
                    self._rhapi.ui.message_notify(self._rhapi.__("RaceLink Communicator ready on {} with MAC: {}").format(used, mac))
                return True
            if "manual" in args:
                self._rhapi.ui.message_notify(self._rhapi.__("No RaceLink Communicator module discovered or configured"))
            return False
        except Exception as ex:
            logger.error("LoRaUSB init failed: %s", ex)
            if "manual" in args:
                self._rhapi.ui.message_notify(self._rhapi.__("Failed to initialize communicator: {}").format(str(ex)))
            return False

    def ensure_ready(self, context: str):
        if self.lora:
            return True
        logger.warning("%s: communicator not ready", context)
        return False

    def install_hooks(self):
        if self._transport_hooks_installed or not self.lora:
            return
        if hasattr(self.lora, "add_listener"):
            self.lora.add_listener(self._on_transport_event)
        if hasattr(self.lora, "add_tx_listener"):
            self.lora.add_tx_listener(self._on_transport_tx)
        self._transport_hooks_installed = True

    def wait_rx_window(self, send_fn, collect_pred=None, fail_safe_s: float = 8.0):
        if not self.lora:
            return [], False
        collected = []
        got_closed = False
        if hasattr(self.lora, "add_listener") and hasattr(self.lora, "remove_listener"):
            closed_ev = threading.Event()

            def _cb(ev: dict):
                nonlocal got_closed
                if not isinstance(ev, dict):
                    return
                if ev.get("type") == EV_RX_WINDOW_CLOSED:
                    got_closed = True
                    closed_ev.set()
                    return
                if collect_pred and collect_pred(ev):
                    collected.append(ev)

            self.lora.add_listener(_cb)
            try:
                send_fn()
                closed_ev.wait(timeout=float(fail_safe_s))
            finally:
                self.lora.remove_listener(_cb)
            return collected, got_closed

        send_fn()
        t_end = time.time() + float(fail_safe_s)
        while time.time() < t_end:
            for ev in self.lora.drain_events(timeout_s=0.1):
                if ev.get("type") == EV_RX_WINDOW_CLOSED:
                    got_closed = True
                    return collected, got_closed
                if collect_pred and collect_pred(ev):
                    collected.append(ev)
        return collected, got_closed

    def send_and_wait_for_reply(self, recv3: bytes, opcode7: int, send_fn, timeout_s: float = 8.0):
        if not self.lora:
            return [], False
        self.install_hooks()
        opcode7 = int(opcode7) & 0x7F
        recv3_b = bytes(recv3 or b"")
        sender_filter = recv3_b if recv3_b and recv3_b != b"\xFF\xFF\xFF" else None
        sender_filter_hex = sender_filter.hex().upper() if sender_filter else ""
        sender_dev = self._get_device(sender_filter_hex) if sender_filter_hex else None

        rule = LPA.find_rule(opcode7)
        policy = int(getattr(rule, "policy", getattr(LPA, "RESP_NONE", 0))) if rule else int(getattr(LPA, "RESP_NONE", 0))
        if policy == int(getattr(LPA, "RESP_NONE", 0)):
            send_fn()
            return [], False
        rsp_opc = int(getattr(rule, "rsp_opcode7", -1)) & 0x7F if rule else -1

        def _collect(ev: dict) -> bool:
            sender3 = ev.get("sender3")
            if sender_filter is not None:
                if not isinstance(sender3, (bytes, bytearray)) or bytes(sender3) != sender_filter:
                    return False
            opc = int(ev.get("opc", -1))
            if policy == int(getattr(LPA, "RESP_ACK", 1)) and opc == int(LP.OPC_ACK) and int(ev.get("ack_of", -1)) == opcode7:
                if sender_dev:
                    sender_dev.mark_online()
                return True
            if policy == int(getattr(LPA, "RESP_SPECIFIC", 2)) and opc == rsp_opc:
                if sender_dev:
                    sender_dev.mark_online()
                return True
            return False

        return self.wait_rx_window(send_fn, collect_pred=_collect, fail_safe_s=timeout_s)

    def _on_transport_tx(self, ev: dict):
        if not ev or ev.get("type") != "TX_M2N":
            return
        recv3 = ev.get("recv3")
        if not isinstance(recv3, (bytes, bytearray)) or len(recv3) != 3 or bytes(recv3) == b"\xFF\xFF\xFF":
            return
        opcode7 = int(ev.get("opc", -1)) & 0x7F
        rule = LPA.find_rule(opcode7)
        if not rule:
            return
        if int(getattr(rule, "req_dir", getattr(LPA, "DIR_M2N", 0))) != int(getattr(LPA, "DIR_M2N", 0)):
            return
        if int(getattr(rule, "policy", getattr(LPA, "RESP_NONE", 0))) == int(getattr(LPA, "RESP_NONE", 0)):
            return
        dev = self._get_device(recv3.hex().upper())
        if not dev:
            return
        self._pending_expect = {"dev": dev, "rule": rule, "opcode7": opcode7, "sender_last3": (dev.addr or "").upper()[-6:]}

    def _on_transport_event(self, ev: dict):
        if not isinstance(ev, dict):
            return
        t = ev.get("type")
        if t == EV_ERROR:
            reason = str(ev.get("data") or "unknown error")
            self._notify_disconnect(reason)
            self._schedule_reconnect(reason)
            return
        if t == EV_RX_WINDOW_CLOSED:
            self._pending_window_closed()
            return

        opc = ev.get("opc")
        if opc is None:
            return
        if int(opc) == int(LP.OPC_STATUS) and ev.get("reply") == "STATUS_REPLY":
            self._on_status_update(ev)
        elif int(opc) == int(LP.OPC_DEVICES) and ev.get("reply") == "IDENTIFY_REPLY":
            mac6 = ev.get("mac6")
            if isinstance(mac6, (bytes, bytearray)) and len(mac6) == 6:
                mac12 = bytes(mac6).hex().upper()
                dev = self._get_device(mac12)
                if not dev:
                    dev_type = ev.get("caps", 0)
                    dev = create_device(addr=mac12, dev_type=int(dev_type or 0), name=f"WLED {mac12}")
                    rl_devicelist.append(dev)
                self._on_identify_update(ev, dev)
        self._pending_try_match(ev)

    def _notify_disconnect(self, reason: str):
        now = time.time()
        if (now - self._last_error_notify_ts) > 2:
            self._last_error_notify_ts = now
            try:
                self._rhapi.ui.message_notify(self._rhapi.__("RaceLink Communicator disconnected: {}").format(reason))
            except Exception:
                logger.exception("RaceLink: failed to notify UI about disconnect")
        self._on_disconnect()

    def _schedule_reconnect(self, reason: str):
        now = time.time()
        if self._reconnect_in_progress or (now - self._last_reconnect_ts) < 5:
            return
        self._last_reconnect_ts = now
        self._reconnect_in_progress = True

        def _reconnect():
            try:
                logger.warning("RaceLink: attempting LoRaUSB reconnect after error: %s", reason)
                try:
                    if self.lora:
                        self.lora.close()
                except Exception:
                    pass
                self.lora = None
                self.discover_port({})
            finally:
                self._reconnect_in_progress = False

        threading.Thread(target=_reconnect, daemon=True).start()

    def _pending_try_match(self, ev: dict):
        p = self._pending_expect
        if not p:
            return
        sender3 = ev.get("sender3")
        sender3_hex = bytes(sender3).hex().upper() if isinstance(sender3, (bytes, bytearray)) else ""
        if not sender3_hex or sender3_hex != (p.get("sender_last3") or "").upper():
            return
        rule = p.get("rule")
        opcode7 = int(p.get("opcode7", -1)) & 0x7F
        policy = int(getattr(rule, "policy", getattr(LPA, "RESP_NONE", 0)))
        if policy == int(getattr(LPA, "RESP_ACK", 1)):
            if int(ev.get("opc", -1)) == int(LP.OPC_ACK) and int(ev.get("ack_of", -2)) == opcode7:
                p.get("dev").mark_online()
                self._pending_expect = None
        elif policy == int(getattr(LPA, "RESP_SPECIFIC", 2)):
            rsp_opc = int(getattr(rule, "rsp_opcode7", -1)) & 0x7F
            if int(ev.get("opc", -1)) == rsp_opc:
                p.get("dev").mark_online()
                self._pending_expect = None

    def _pending_window_closed(self):
        p = self._pending_expect
        if not p:
            return
        dev = p.get("dev")
        rule = p.get("rule")
        opcode7 = int(p.get("opcode7", -1)) & 0x7F
        name = getattr(rule, "name", f"opc=0x{opcode7:02X}")
        if dev:
            dev.mark_offline(f"Missing reply ({name})")
        self._pending_expect = None
