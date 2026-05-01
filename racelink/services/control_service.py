"""Control message service for active device/group operations.

Post-Phase-D naming:
- ``send_wled_preset`` sends a WLED preset id (OPC_PRESET, 4 B fixed).
- ``send_wled_control`` sends a direct effect-parameter packet (OPC_CONTROL,
  variable length). Pre-rename: ``send_wled_control_advanced``.
- ``send_rl_preset_by_id`` resolves a stable RL-preset id and dispatches
  through ``send_wled_control`` with the persisted parameter snapshot.
"""

from __future__ import annotations

import logging

from ..domain import USER_FLAG_KEYS, build_flags_byte
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
        """Return the active transport or ``None`` after logging a warning.

        Callers should bind the return value to a local variable and guard on
        it so both the runtime check and the static type-narrowing apply at
        the same site.
        """
        transport = self.transport
        if transport is None:
            logger.warning("%s: communicator not ready", context)
            return None
        return transport

    @staticmethod
    def _coerce_preset_values(flags, preset_id, brightness, *, fallback=None):
        if fallback is not None:
            flags = fallback.flags if flags is None else flags
            preset_id = fallback.presetId if preset_id is None else preset_id
            brightness = fallback.brightness if brightness is None else brightness
        return int(flags) & 0xFF, int(preset_id) & 0xFF, int(brightness) & 0xFF

    def _update_group_preset_cache(self, group_id: int, flags: int, preset_id: int, brightness: int) -> None:
        for device in self.controller.device_repository.list():
            try:
                if (int(getattr(device, "groupId", 0)) & 0xFF) != group_id:
                    continue
                device.flags = flags
                device.presetId = preset_id
                device.brightness = brightness
            except Exception:
                # swallow-ok: bulk cache update keeps going on the
                # remaining devices. Per-device failure here means
                # malformed groupId / non-int field — a data quality
                # issue worth diagnosing, so debug-log with traceback
                # rather than silently dropping (B5).
                logger.debug(
                    "group-preset cache update skipped device %r",
                    getattr(device, "addr", "?"),
                    exc_info=True,
                )
                continue

    def send_device_preset(self, target_device, flags=None, preset_id=None, brightness=None) -> bool:
        """Send OPC_PRESET to a single node (receiver = last3 of targetDevice.addr).

        Returns ``True`` if a frame was queued for the gateway, ``False``
        if the transport is not ready. Pre-B2 fix this fell off the end
        as ``None``, which the wrapper :meth:`send_wled_preset`
        misinterpreted as success — operators saw 200 OK with no packet
        on the wire.
        """
        transport = self._require_transport("sendDevicePreset")
        if transport is None:
            return False

        recv3 = mac_last3_from_hex(target_device.addr)
        group_id = int(target_device.groupId) & 0xFF
        flags_b, preset_b, brightness_b = self._coerce_preset_values(
            flags,
            preset_id,
            brightness,
            fallback=target_device,
        )

        transport.send_preset(
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
        return True

    def send_group_preset(self, group_id, flags, preset_id, brightness) -> bool:
        """Broadcast OPC_PRESET to a group; update local cache for matching devices.

        Returns ``True`` if a frame was queued, ``False`` when the
        transport is not ready. Same B2 contract as
        :meth:`send_device_preset`.
        """
        transport = self._require_transport("sendGroupPreset")
        if transport is None:
            return False

        group_b = int(group_id) & 0xFF
        flags_b, preset_b, brightness_b = self._coerce_preset_values(flags, preset_id, brightness)
        self._update_group_preset_cache(group_b, flags_b, preset_b, brightness_b)

        transport.send_preset(
            recv3=b"\xFF\xFF\xFF",
            group_id=group_b,
            flags=flags_b,
            preset_id=preset_b,
            brightness=brightness_b,
        )
        return True

    def send_wled_preset(self, *, targetDevice=None, targetGroup=None, params=None):
        """Apply a classical WLED preset (OPC_PRESET) to a device or group.

        Accepts numeric ``presetId`` only. RaceLink-native RL-presets follow a
        separate path via :meth:`send_rl_preset_by_id`.

        Flag handling is identical to :meth:`send_wled_control` (protocol
        contract, ``racelink_proto.h`` byte 1 on both opcodes). Honours the
        four user-intent flags from ``params``: ``arm_on_sync``, ``force_tt0``,
        ``force_reapply``, ``offset_mode``. ``POWER_ON``/``HAS_BRI`` are
        derived from brightness and never passed through from the caller.
        """
        if params is None:
            params = {}
        preset_id = int(params.get("presetId", 1))
        brightness = int(params.get("brightness", 0))
        flags = build_flags_byte(
            power_on=brightness > 0,
            has_bri=True,
            arm_on_sync=bool(params.get("arm_on_sync")),
            force_tt0=bool(params.get("force_tt0")),
            force_reapply=bool(params.get("force_reapply")),
            offset_mode=bool(params.get("offset_mode")),
        )

        # B2: propagate the underlying boolean — ``send_group_preset`` /
        # ``send_device_preset`` return False when the transport is
        # missing. Pre-fix this wrapper unconditionally returned True,
        # so a route or scene-runner caller saw success even though no
        # frame went out.
        if targetGroup is not None:
            return self.send_group_preset(int(targetGroup), flags, preset_id, brightness)
        if targetDevice is not None:
            return self.send_device_preset(targetDevice, flags, preset_id, brightness)
        return False

    def send_offset(self, *, targetDevice=None, targetGroup=None,
                    mode="none", **mode_params) -> bool:
        """Send OPC_OFFSET (variable-length, 2..7 B body).

        ``mode`` selects the formula stored on the receiver and accepts
        either the int enum value (``OFFSET_MODE_LINEAR``) or its lowercase
        name (``"linear"``). Per-mode kwargs:

            "none":     (no extra args; clears stored config)
            "explicit": offset_ms
            "linear":   base_ms, step_ms
            "vshape":   base_ms, step_ms, center
            "modulo":   base_ms, step_ms, cycle

        With ``targetGroup=255`` the body's groupId is the broadcast sentinel
        — every device picks it up. The acceptance gate on subsequent
        OPC_CONTROL packets selects the participating subset.

        Returns ``True`` if a frame was queued, ``False`` if the transport is
        not ready or no target was provided.
        """
        transport = self._require_transport("sendOffset")
        if transport is None:
            return False

        if targetGroup is not None:
            group_b = int(targetGroup) & 0xFF
            transport.send_offset(
                recv3=b"\xFF\xFF\xFF", group_id=group_b,
                mode=mode, **mode_params,
            )
            return True
        if targetDevice is not None:
            recv3 = mac_last3_from_hex(targetDevice.addr)
            group_b = int(getattr(targetDevice, "groupId", 0)) & 0xFF
            transport.send_offset(
                recv3=recv3, group_id=group_b,
                mode=mode, **mode_params,
            )
            return True
        return False

    def send_rl_preset_by_id(
        self,
        preset_id,
        *,
        targetDevice=None,
        targetGroup=None,
        brightness_override=None,
    ):
        """Apply a RaceLink-native preset (OPC_CONTROL) by its stable int id.

        Loads the persisted parameter snapshot via ``rl_presets_service`` and
        delegates to :meth:`send_wled_control` with the merged params.
        ``brightness_override`` (e.g. from a RotorHazard Quickset slider) takes
        precedence over the value saved in the preset. Flags stored with the
        preset (``arm_on_sync`` / ``force_tt0`` / ``force_reapply``) are
        forwarded as ``params[...]`` toggles so the direct sender picks them
        up via its existing flag-derivation logic.

        Returns ``True`` on success, ``False`` on unknown id / missing service.
        """
        rl_service = getattr(self.controller, "rl_presets_service", None)
        if rl_service is None:
            logger.warning("sendRlPresetById: rl_presets_service not wired")
            return False
        try:
            pid = int(preset_id)
        except (TypeError, ValueError):
            logger.warning("sendRlPresetById: invalid preset id %r", preset_id)
            return False

        preset = rl_service.get_by_id(pid)
        if preset is None:
            logger.warning("sendRlPresetById: preset id=%d not found", pid)
            return False

        merged = dict(preset.get("params") or {})
        if brightness_override is not None:
            merged["brightness"] = int(brightness_override)

        flags_meta = preset.get("flags") or {}
        for key in USER_FLAG_KEYS:
            if flags_meta.get(key):
                merged[key] = True

        return self.send_wled_control(
            targetDevice=targetDevice,
            targetGroup=targetGroup,
            params=merged,
        )

    def send_wled_control(self, *, targetDevice=None, targetGroup=None, params=None):
        """Send OPC_CONTROL with the WLED effect parameters from ``params``
        (pre-rename: ``send_wled_control_advanced``).

        Full-state semantics: every field present in ``params`` is included in
        the serialized body. Fields set to ``None`` (or missing entirely) are
        omitted via the ``fieldMask``/``extMask`` presence bits, so the
        WLED node leaves them untouched.

        Flag handling matches :meth:`send_wled_preset`: ``POWER_ON`` derived
        from brightness, ``HAS_BRI`` always set when brightness is provided.
        """
        transport = self._require_transport("sendWledControl")
        if transport is None:
            return False

        params = params or {}
        brightness = params.get("brightness")
        brightness_val = int(brightness) & 0xFF if brightness is not None else 0
        # Flag byte is identical for OPC_PRESET and OPC_CONTROL by protocol
        # contract (see comment in ``racelink_proto.h``). Composed via the
        # shared ``build_flags_byte`` helper.
        flags = build_flags_byte(
            power_on=brightness_val > 0,
            has_bri=True,
            arm_on_sync=bool(params.get("arm_on_sync")),
            force_tt0=bool(params.get("force_tt0")),
            force_reapply=bool(params.get("force_reapply")),
            offset_mode=bool(params.get("offset_mode")),
        )

        # Collect the kwargs for build_control_body(). Only pass fields
        # actually present in params so the builder emits a minimal body (any
        # unsupplied field stays out of fieldMask/extMask).
        ctrl_kwargs: dict = {}
        if brightness is not None:
            ctrl_kwargs["brightness"] = brightness_val
        for key in ("mode", "speed", "intensity", "custom1", "custom2", "custom3", "palette"):
            if key in params and params[key] is not None:
                ctrl_kwargs[key] = int(params[key]) & 0xFF
        for key in ("check1", "check2", "check3"):
            if key in params and params[key] is not None:
                ctrl_kwargs[key] = bool(params[key])
        for key in ("color1", "color2", "color3"):
            if key in params and params[key] is not None:
                ctrl_kwargs[key] = tuple(int(c) & 0xFF for c in params[key])

        if targetGroup is not None:
            group_b = int(targetGroup) & 0xFF
            transport.send_control(
                recv3=b"\xFF\xFF\xFF",
                group_id=group_b,
                flags=flags,
                **ctrl_kwargs,
            )
            return True
        if targetDevice is not None:
            recv3 = mac_last3_from_hex(targetDevice.addr)
            group_b = int(getattr(targetDevice, "groupId", 0)) & 0xFF
            transport.send_control(
                recv3=recv3,
                group_id=group_b,
                flags=flags,
                **ctrl_kwargs,
            )
            logger.debug(
                "RL: Sent CONTROL to %s: flags=0x%02X fields=%s",
                targetDevice.addr,
                flags,
                sorted(ctrl_kwargs.keys()),
            )
            return True
        return False
