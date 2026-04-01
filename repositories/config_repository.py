from __future__ import annotations

import ast
import json
import logging
from typing import Iterable

from ..data import (
    RL_Device,
    RL_DeviceGroup,
    RL_FLAG_HAS_BRI,
    RL_FLAG_POWER_ON,
    build_specials_state,
    create_device,
    rl_backup_devicelist,
    rl_backup_grouplist,
)

logger = logging.getLogger(__name__)


class ConfigRepository:
    def __init__(self, db):
        self._db = db

    def load_devices(self) -> list[RL_Device]:
        raw_devices = self._db.option("rl_device_config", None)
        if raw_devices is None:
            raw_devices = self._serialize(rl_backup_devicelist)
            self._db.option_set("rl_device_config", raw_devices)
        elif raw_devices == "":
            raw_devices = "[]"
            self._db.option_set("rl_device_config", raw_devices)

        device_items = self._parse_records(raw_devices, "devices")
        devices: list[RL_Device] = []

        for device in device_items:
            if not isinstance(device, dict):
                continue
            try:
                devices.append(self._build_device(device))
            except Exception:
                logger.exception("RL: failed to load device entry from DB: %r", device)

        return devices

    def save_devices(self, devices: Iterable[RL_Device]) -> None:
        self._db.option_set("rl_device_config", self._serialize(devices))

    def load_groups(self) -> list[RL_DeviceGroup]:
        raw_groups = self._db.option("rl_groups_config", None)
        if raw_groups in (None, ""):
            raw_groups = self._serialize(rl_backup_grouplist)
            self._db.option_set("rl_groups_config", raw_groups)

        group_items = self._parse_records(raw_groups, "groups")
        groups: list[RL_DeviceGroup] = []

        for group in group_items:
            if not isinstance(group, dict):
                continue
            try:
                group_dev_type = group.get("dev_type", group.get("device_type", 0))
                groups.append(
                    RL_DeviceGroup(
                        str(group.get("name", "")),
                        int(group.get("static_group", 0) or 0),
                        int(group_dev_type or 0),
                    )
                )
            except Exception:
                logger.exception("RL: failed to load group entry from DB: %r", group)

        return self._normalize_groups(groups)

    def save_groups(self, groups: Iterable[RL_DeviceGroup]) -> None:
        group_list = list(groups)
        if len(group_list) < len(rl_backup_grouplist):
            group_list = list(rl_backup_grouplist)
        self._db.option_set("rl_groups_config", self._serialize(group_list))

    def load_all(self) -> tuple[list[RL_Device], list[RL_DeviceGroup]]:
        return self.load_devices(), self.load_groups()

    def save_all(self, devices: Iterable[RL_Device], groups: Iterable[RL_DeviceGroup]) -> None:
        self.save_devices(devices)
        self.save_groups(groups)

    def _serialize(self, entries: Iterable[object]) -> str:
        return json.dumps([dict(getattr(obj, "__dict__", {})) for obj in entries])

    def _parse_records(self, raw_data: object, label: str) -> list:
        if isinstance(raw_data, list):
            return raw_data
        if not isinstance(raw_data, str):
            return []

        # JSON-first parser strategy.
        try:
            parsed = json.loads(raw_data)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            pass

        # Fallback migration for historical Python-literal storage format.
        try:
            parsed = ast.literal_eval(raw_data)
            if isinstance(parsed, tuple):
                parsed = list(parsed)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            logger.warning("RL: failed to parse stored %s config, using empty list", label)
            return []

    def _build_device(self, device: dict) -> RL_Device:
        flags = device.get("flags", None)
        preset_id = device.get("presetId", None)

        # Legacy migration: state/effect -> flags/presetId.
        if flags is None:
            legacy_state = int(device.get("state", 1) or 0)
            flags = RL_FLAG_POWER_ON if legacy_state else 0
            if "brightness" in device:
                flags |= RL_FLAG_HAS_BRI

        if preset_id is None:
            preset_id = int(device.get("effect", 1) or 1)

        brightness = int(device.get("brightness", 70) or 0)

        dev_type = device.get("dev_type", None)
        if dev_type is None:
            dev_type = device.get("device_type", None)
        if dev_type is None:
            dev_type = device.get("caps", device.get("type", 0))

        special_state = build_specials_state(int(dev_type or 0), device)

        return create_device(
            addr=str(device.get("addr", "")).upper(),
            dev_type=int(dev_type or 0),
            name=str(device.get("name", "")),
            groupId=int(device.get("groupId", 0) or 0),
            version=int(device.get("version", 0) or 0),
            caps=int(dev_type or 0),
            flags=int(flags) & 0xFF,
            presetId=int(preset_id) & 0xFF,
            brightness=brightness & 0xFF,
            specials=special_state,
        )

    def _normalize_groups(self, groups: list[RL_DeviceGroup]) -> list[RL_DeviceGroup]:
        normalized = [
            g
            for g in groups
            if str(getattr(g, "name", "")).strip().lower() not in {"unconfigured", "all wled devices"}
        ]

        if not any(str(getattr(g, "name", "")).strip().lower() == "all wled nodes" for g in normalized):
            normalized.append(RL_DeviceGroup("All WLED Nodes", static_group=1, dev_type=0))
        else:
            for group in normalized:
                if str(getattr(group, "name", "")).strip().lower() == "all wled nodes":
                    group.name = "All WLED Nodes"
                    group.static_group = 1
                    group.dev_type = 0

        return normalized
