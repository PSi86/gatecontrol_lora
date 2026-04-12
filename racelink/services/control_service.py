"""Control message service for active device/group operations."""

from __future__ import annotations

import logging

from ..domain import RL_FLAG_HAS_BRI, RL_FLAG_POWER_ON
from ..transport import mac_last3_from_hex

logger = logging.getLogger(__name__)


class ControlService:
    def __init__(self, controller, gateway_service):
        self.controller = controller
        self.gateway_service = gateway_service

    @property
    def transport(self):
        return getattr(self.controller, "transport", None)

    def _require_transport(self, context: str):
        if self.transport:
            return True
        logger.warning("%s: communicator not ready", context)
        return False

    @staticmethod
    def _coerce_control_values(flags, preset_id, brightness, *, fallback=None):
        if fallback is not None:
            flags = fallback.flags if flags is None else flags
            preset_id = fallback.presetId if preset_id is None else preset_id
            brightness = fallback.brightness if brightness is None else brightness
        return int(flags) & 0xFF, int(preset_id) & 0xFF, int(brightness) & 0xFF

    def _update_group_control_cache(self, group_id: int, flags: int, preset_id: int, brightness: int) -> None:
        for device in self.controller.device_repository.list():
            try:
                if (int(getattr(device, "groupId", 0)) & 0xFF) != group_id:
                    continue
                device.flags = flags
                device.presetId = preset_id
                device.brightness = brightness
            except Exception:
                continue

    def send_device_control(self, target_device, flags=None, preset_id=None, brightness=None):
        """Send CONTROL to a single node (receiver = last3 of targetDevice.addr)."""
        if not self._require_transport("sendRaceLink"):
            return

        recv3 = mac_last3_from_hex(target_device.addr)
        group_id = int(target_device.groupId) & 0xFF
        flags_b, preset_b, brightness_b = self._coerce_control_values(
            flags,
            preset_id,
            brightness,
            fallback=target_device,
        )

        self.transport.send_control(
            recv3=recv3,
            group_id=group_id,
            flags=flags_b,
            preset_id=preset_b,
            brightness=brightness_b,
        )

        target_device.flags = flags_b
        target_device.presetId = preset_b
        target_device.brightness = brightness_b
        logger.debug(
            "RL: Updated Device %s: flags=0x%02X presetId=%d brightness=%d",
            target_device.addr,
            target_device.flags,
            target_device.presetId,
            target_device.brightness,
        )

    def send_group_control(self, group_id, flags, preset_id, brightness):
        """Broadcast CONTROL to a group; update local cache for matching devices."""
        if not self._require_transport("sendGroupControl"):
            return

        group_b = int(group_id) & 0xFF
        flags_b, preset_b, brightness_b = self._coerce_control_values(flags, preset_id, brightness)
        self._update_group_control_cache(group_b, flags_b, preset_b, brightness_b)

        self.transport.send_control(
            recv3=b"\xFF\xFF\xFF",
            group_id=group_b,
            flags=flags_b,
            preset_id=preset_b,
            brightness=brightness_b,
        )

    def send_wled_control(self, *, targetDevice=None, targetGroup=None, params=None):
        if params is None:
            params = {}
        preset_id = int(params.get("presetId", 1))
        brightness = int(params.get("brightness", 0))
        flags = (RL_FLAG_POWER_ON if brightness > 0 else 0) | RL_FLAG_HAS_BRI

        if targetGroup is not None:
            self.send_group_control(int(targetGroup), flags, preset_id, brightness)
            return True
        if targetDevice is not None:
            self.send_device_control(targetDevice, flags, preset_id, brightness)
            return True
        return False
