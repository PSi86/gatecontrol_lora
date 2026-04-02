from __future__ import annotations

import logging
from typing import Callable, Optional

from ...data import RL_Device, rl_devicelist
from ...racelink_transport import LP, _mac_last3_from_hex

logger = logging.getLogger(__name__)


class DeviceService:
    def __init__(self, transport_coordinator, notifier=None):
        self._transport = transport_coordinator
        self._notifier = notifier

    @staticmethod
    def get_device_from_address(addr: str) -> Optional[RL_Device]:
        if not addr:
            return None
        s = str(addr).strip().upper()
        if len(s) == 12:
            for d in rl_devicelist:
                if (d.addr or "").upper() == s:
                    return d
            return None
        if len(s) == 6:
            for d in rl_devicelist:
                if (d.addr or "").upper().endswith(s):
                    return d
            return None
        return None

    def discover_devices(self, group_filter=255, target_device=None, add_to_group=-1, set_group_fn: Callable | None = None):
        if not self._transport.ensure_ready("getDevices"):
            return 0

        self._transport.install_hooks()
        if target_device is None:
            recv3 = b"\xFF\xFF\xFF"
            group_id = int(group_filter) & 0xFF
        else:
            recv3 = _mac_last3_from_hex(target_device.addr)
            group_id = int(target_device.groupId) & 0xFF

        found = 0
        responders = set()

        def _collect(ev: dict) -> bool:
            nonlocal found
            if ev.get("opc") == LP.OPC_DEVICES and ev.get("reply") == "IDENTIFY_REPLY":
                found += 1
                mac6 = ev.get("mac6")
                if isinstance(mac6, (bytes, bytearray)) and len(mac6) == 6:
                    responders.add(bytes(mac6).hex().upper())
                else:
                    sender3 = ev.get("sender3")
                    if isinstance(sender3, (bytes, bytearray)) and len(sender3) == 3:
                        responders.add(bytes(sender3).hex().upper())
                return True
            return False

        try:
            self._transport.lora.drain_events(0.0)
        except Exception:
            pass

        self._transport.wait_rx_window(
            lambda: self._transport.lora.send_get_devices(recv3=recv3, group_id=group_id, flags=0),
            collect_pred=_collect,
            fail_safe_s=8.0,
        )

        if add_to_group > 0 and add_to_group < 255 and callable(set_group_fn):
            for addr in responders:
                dev = self.get_device_from_address(addr)
                if dev:
                    dev.groupId = add_to_group
                    set_group_fn(dev)

        if self._notifier:
            if add_to_group > 0 and add_to_group < 255:
                msg = f"Device Discovery finished with {found} devices found and added to GroupId: {add_to_group}"
            else:
                msg = f"Device Discovery finished with {found} devices found."
            self._notifier.notify(msg)
        return found

    def get_status(self, group_filter=255, target_device=None):
        if not self._transport.ensure_ready("getStatus"):
            return 0
        self._transport.install_hooks()

        if target_device is None:
            recv3 = b"\xFF\xFF\xFF"
            group_id = int(group_filter) & 0xFF
            sender_filter = None
        else:
            recv3 = _mac_last3_from_hex(target_device.addr)
            group_id = int(target_device.groupId) & 0xFF
            sender_filter = recv3.hex().upper()

        updated = 0
        responders = set()

        def _collect(ev: dict) -> bool:
            nonlocal updated
            if ev.get("opc") == LP.OPC_STATUS and ev.get("reply") == "STATUS_REPLY":
                if sender_filter:
                    sender3 = ev.get("sender3")
                    if isinstance(sender3, (bytes, bytearray)) and bytes(sender3).hex().upper() != sender_filter:
                        return False
                updated += 1
                mac6 = ev.get("mac6")
                if isinstance(mac6, (bytes, bytearray)) and len(mac6) == 6:
                    responders.add(bytes(mac6).hex().upper())
                else:
                    sender3 = ev.get("sender3")
                    if isinstance(sender3, (bytes, bytearray)) and len(sender3) == 3:
                        responders.add(bytes(sender3).hex().upper())
                return True
            return False

        try:
            self._transport.lora.drain_events(0.0)
        except Exception:
            pass

        _, got_closed = self._transport.wait_rx_window(
            lambda: self._transport.lora.send_get_status(recv3=recv3, group_id=group_id, flags=0),
            collect_pred=_collect,
            fail_safe_s=8.0,
        )

        if got_closed:
            if target_device is not None:
                if updated == 0:
                    target_device.mark_offline("Missing reply (STATUS)")
            else:
                targets = list(rl_devicelist) if group_filter == 255 else [
                    dev for dev in rl_devicelist if int(getattr(dev, "groupId", 0)) == int(group_filter)
                ]
                for dev in targets:
                    mac = (dev.addr or "").upper()
                    if mac and mac not in responders and mac[-6:] not in responders:
                        dev.mark_offline("Missing reply (STATUS)")
        return updated
