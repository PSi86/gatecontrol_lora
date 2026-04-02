from __future__ import annotations

import time

from ...data import RL_Device, RL_FLAG_HAS_BRI, RL_FLAG_POWER_ON, rl_devicelist
from ...racelink_transport import LP, _mac_last3_from_hex


class ControlService:
    def __init__(self, transport_coordinator):
        self._transport = transport_coordinator

    @staticmethod
    def _coerce(flags, preset_id, brightness, fallback: RL_Device | None = None):
        if fallback is not None:
            flags = fallback.flags if flags is None else flags
            preset_id = fallback.presetId if preset_id is None else preset_id
            brightness = fallback.brightness if brightness is None else brightness
        return int(flags) & 0xFF, int(preset_id) & 0xFF, int(brightness) & 0xFF

    @staticmethod
    def _update_group_cache(group_id: int, flags: int, preset_id: int, brightness: int):
        for device in rl_devicelist:
            if (int(getattr(device, "groupId", 0)) & 0xFF) != group_id:
                continue
            device.flags = flags
            device.presetId = preset_id
            device.brightness = brightness

    @staticmethod
    def _stream_ctrl(start: bool, stop: bool, packets_left: int) -> int:
        ctrl = (0x80 if start else 0x00) | (0x40 if stop else 0x00)
        return ctrl | (int(packets_left) & 0x3F)

    def send_racelink(self, target_device, flags=None, preset_id=None, brightness=None):
        if not self._transport.ensure_ready("sendRaceLink"):
            return
        recv3 = _mac_last3_from_hex(target_device.addr)
        group_id = int(target_device.groupId) & 0xFF
        f, p, b = self._coerce(flags, preset_id, brightness, fallback=target_device)
        self._transport.lora.send_control(recv3=recv3, group_id=group_id, flags=f, preset_id=p, brightness=b)
        target_device.flags = f
        target_device.presetId = p
        target_device.brightness = b

    def send_group_control(self, group_id, flags, preset_id, brightness):
        if not self._transport.ensure_ready("sendGroupControl"):
            return
        gid = int(group_id) & 0xFF
        f, p, b = self._coerce(flags, preset_id, brightness)
        self._update_group_cache(gid, f, p, b)
        self._transport.lora.send_control(recv3=b"\xFF\xFF\xFF", group_id=gid, flags=f, preset_id=p, brightness=b)


    @staticmethod
    def _flags_for_brightness(brightness: int) -> int:
        bri = int(brightness)
        return (RL_FLAG_POWER_ON if bri > 0 else 0) | RL_FLAG_HAS_BRI

    def apply_device_switch(self, *, target_device: RL_Device, brightness: int, preset_id: int):
        flags = self._flags_for_brightness(brightness)
        self.send_racelink(target_device, flags, int(preset_id), int(brightness))

    def apply_group_switch(self, *, group_id: int, brightness: int, preset_id: int):
        flags = self._flags_for_brightness(brightness)
        self.send_group_control(int(group_id), flags, int(preset_id), int(brightness))

    def send_wled_control(self, *, target_device=None, target_group=None, params=None):
        params = params or {}
        preset_id = int(params.get("presetId", 1))
        brightness = int(params.get("brightness", 0))
        flags = self._flags_for_brightness(brightness)
        if target_group is not None:
            self.send_group_control(int(target_group), flags, preset_id, brightness)
            return True
        if target_device is not None:
            self.send_racelink(target_device, flags, preset_id, brightness)
            return True
        return False

    def send_stream(self, payload: bytes, group_id: int | None = None, device: RL_Device | None = None, retries: int = 2, timeout_s: float = 8.0):
        if not self._transport.ensure_ready("sendStream"):
            return {}
        self._transport.install_hooks()
        data = bytes(payload or b"")
        if len(data) > 128:
            raise ValueError("payload too large (max 128 bytes)")
        if device is None and group_id is None:
            raise ValueError("sendStream requires groupId or device")

        total_packets = max(1, (len(data) + 7) // 8)
        ctrl = self._stream_ctrl(True, total_packets == 1, 0 if total_packets == 1 else total_packets)
        targets = [device] if device is not None else [d for d in rl_devicelist if int(getattr(d, "groupId", 0) or 0) == int(group_id)]
        target_last3 = {_mac_last3_from_hex(dev.addr) for dev in targets if dev and dev.addr}
        target_last3.discard(b"\xFF\xFF\xFF")
        expected = len(target_last3)
        if expected == 0:
            return {"expected": 0, "acked": 0}

        recv3 = b"\xFF\xFF\xFF" if device is None else _mac_last3_from_hex(device.addr)
        if recv3 == b"\xFF\xFF\xFF" and device is not None:
            return {"expected": expected, "acked": 0}

        try:
            self._transport.lora.drain_events(0.0)
        except Exception:
            pass

        acked = set()

        def _collect(ev: dict) -> bool:
            if ev.get("opc") != LP.OPC_ACK:
                return False
            if int(ev.get("ack_of", -1)) != int(LP.OPC_STREAM):
                return False
            sender3 = ev.get("sender3")
            if not isinstance(sender3, (bytes, bytearray)):
                return False
            sender3_b = bytes(sender3)
            if sender3_b not in target_last3:
                return False
            acked.add(sender3_b)
            return True

        for attempt in range(max(0, int(retries)) + 1):
            self._transport.wait_rx_window(
                lambda: self._transport.lora.send_stream(recv3=recv3, ctrl=ctrl, data=data),
                collect_pred=_collect,
                fail_safe_s=timeout_s,
            )
            if len(acked) >= expected:
                break
            if attempt < int(retries):
                time.sleep(0.1)

        return {"expected": expected, "acked": len(acked)}
