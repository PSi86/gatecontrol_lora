from __future__ import annotations

import ast
from abc import ABC, abstractmethod
from typing import Callable, Iterable

from ...data import RL_Device, RL_DeviceGroup, build_specials_state, create_device, RL_FLAG_HAS_BRI, RL_FLAG_POWER_ON


class DeviceRepository(ABC):
    @abstractmethod
    def get(self, addr: str) -> RL_Device | None:
        raise NotImplementedError

    @abstractmethod
    def add(self, device: RL_Device) -> None:
        raise NotImplementedError

    @abstractmethod
    def update(self, device: RL_Device) -> None:
        raise NotImplementedError

    @abstractmethod
    def find(self, predicate: Callable[[RL_Device], bool]) -> list[RL_Device]:
        raise NotImplementedError

    @abstractmethod
    def by_group(self, group_id: int) -> list[RL_Device]:
        raise NotImplementedError

    @abstractmethod
    def all(self) -> list[RL_Device]:
        raise NotImplementedError


class InMemoryDeviceRepository(DeviceRepository):
    def __init__(self, *, devices: Iterable[RL_Device] | None = None, groups: Iterable[RL_DeviceGroup] | None = None):
        self._devices: list[RL_Device] = list(devices or [])
        self._groups: list[RL_DeviceGroup] = list(groups or [RL_DeviceGroup("All WLED Nodes", 1, 0)])

    # compatibility shim for existing list-based UI code
    @property
    def device_items(self) -> list[RL_Device]:
        return self._devices

    @property
    def group_items(self) -> list[RL_DeviceGroup]:
        return self._groups

    def clear_devices(self) -> None:
        self._devices.clear()

    def clear_groups(self) -> None:
        self._groups.clear()

    def all_groups(self) -> list[RL_DeviceGroup]:
        return list(self._groups)

    def set_groups(self, groups: Iterable[RL_DeviceGroup]) -> None:
        self._groups = list(groups)

    def add_group(self, group: RL_DeviceGroup) -> None:
        self._groups.append(group)

    def get(self, addr: str) -> RL_Device | None:
        if not addr:
            return None
        s = str(addr).strip().upper()
        if len(s) == 12:
            return next((d for d in self._devices if (d.addr or "").upper() == s), None)
        if len(s) == 6:
            return next((d for d in self._devices if (d.addr or "").upper().endswith(s)), None)
        return None

    def add(self, device: RL_Device) -> None:
        existing = self.get(getattr(device, "addr", ""))
        if existing is None:
            self._devices.append(device)
        else:
            self.update(device)

    def update(self, device: RL_Device) -> None:
        addr = str(getattr(device, "addr", "") or "").upper()
        for idx, item in enumerate(self._devices):
            if str(getattr(item, "addr", "") or "").upper() == addr:
                self._devices[idx] = device
                return
        self._devices.append(device)

    def find(self, predicate: Callable[[RL_Device], bool]) -> list[RL_Device]:
        return [d for d in self._devices if predicate(d)]

    def by_group(self, group_id: int) -> list[RL_Device]:
        gid = int(group_id)
        return [d for d in self._devices if int(getattr(d, "groupId", 0) or 0) == gid]

    def all(self) -> list[RL_Device]:
        return list(self._devices)


class LegacyConfigMigration:
    @staticmethod
    def _safe_list(value: str | None) -> list[dict]:
        if value is None:
            return []
        raw = str(value).strip()
        if raw == "":
            return []
        parsed = ast.literal_eval(raw)
        return list(parsed) if isinstance(parsed, list) else []

    @classmethod
    def load_devices_into_repo(cls, config_str_devices: str | None, repo: InMemoryDeviceRepository) -> None:
        repo.clear_devices()
        for item in cls._safe_list(config_str_devices):
            try:
                flags = item.get("flags", None)
                preset_id = item.get("presetId", None)

                if flags is None:
                    legacy_state = int(item.get("state", 1) or 0)
                    flags = RL_FLAG_POWER_ON if legacy_state else 0
                    if "brightness" in item:
                        flags |= RL_FLAG_HAS_BRI

                if preset_id is None:
                    preset_id = int(item.get("effect", 1) or 1)

                brightness = int(item.get("brightness", 70) or 0)

                dev_type = item.get("dev_type", None)
                if dev_type is None:
                    dev_type = item.get("device_type", None)
                if dev_type is None:
                    dev_type = item.get("caps", item.get("type", 0))

                special_state = build_specials_state(int(dev_type or 0), item)
                repo.add(
                    create_device(
                        addr=str(item.get("addr", "")).upper(),
                        dev_type=int(dev_type or 0),
                        name=str(item.get("name", "")),
                        groupId=int(item.get("groupId", 0) or 0),
                        version=int(item.get("version", 0) or 0),
                        caps=int(dev_type or 0),
                        flags=int(flags) & 0xFF,
                        presetId=int(preset_id) & 0xFF,
                        brightness=brightness & 0xFF,
                        specials=special_state,
                    )
                )
            except Exception:
                continue

    @classmethod
    def load_groups_into_repo(cls, config_str_groups: str | None, repo: InMemoryDeviceRepository) -> None:
        groups: list[RL_DeviceGroup] = []
        for group in cls._safe_list(config_str_groups):
            try:
                group_dev_type = group.get("dev_type", group.get("device_type", 0))
                groups.append(RL_DeviceGroup(group["name"], group["static_group"], group_dev_type))
            except Exception:
                continue

        groups = [
            g
            for g in groups
            if str(getattr(g, "name", "")).strip().lower() not in {"unconfigured", "all wled devices"}
        ]

        if not any(str(getattr(g, "name", "")).strip().lower() == "all wled nodes" for g in groups):
            groups.append(RL_DeviceGroup("All WLED Nodes", static_group=1, dev_type=0))
        else:
            for g in groups:
                if str(getattr(g, "name", "")).strip().lower() == "all wled nodes":
                    g.name = "All WLED Nodes"
                    g.static_group = 1
                    g.dev_type = 0
        repo.set_groups(groups)

    @staticmethod
    def dump_devices(repo: InMemoryDeviceRepository) -> str:
        return str([obj.__dict__ for obj in repo.all()])

    @staticmethod
    def dump_groups(repo: InMemoryDeviceRepository) -> str:
        return str([obj.__dict__ for obj in repo.all_groups()])
