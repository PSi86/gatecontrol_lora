"""Central state repositories for RaceLink runtime data."""

from __future__ import annotations

from .defaults import default_backup_devices, default_backup_groups


class DeviceRepository:
    def __init__(self, items=None):
        self._items = items if items is not None else []

    def list(self):
        return self._items

    def append(self, item):
        self._items.append(item)
        return item

    def clear(self):
        self._items.clear()

    def replace_all(self, items):
        self._items[:] = list(items)

    def remove(self, item):
        self._items.remove(item)

    def upsert(self, device):
        existing = self.get_by_addr(getattr(device, "addr", ""))
        if existing is None:
            self._items.append(device)
            return device
        idx = self._items.index(existing)
        self._items[idx] = device
        return device

    def get_by_addr(self, addr):
        if not addr:
            return None
        s = str(addr).strip().upper()
        if len(s) == 12:
            for item in self._items:
                if (getattr(item, "addr", "") or "").upper() == s:
                    return item
            return None
        if len(s) == 6:
            for item in self._items:
                if (getattr(item, "addr", "") or "").upper().endswith(s):
                    return item
            return None
        return None


class GroupRepository:
    def __init__(self, items=None):
        self._items = items if items is not None else []

    def list(self):
        return self._items

    def append(self, item):
        self._items.append(item)
        return len(self._items) - 1

    def clear(self):
        self._items.clear()

    def replace_all(self, items):
        self._items[:] = list(items)

    def remove(self, index):
        del self._items[index]

    def get(self, index):
        return self._items[index]

    def __len__(self):
        return len(self._items)


class StateRepository:
    def __init__(
        self,
        *,
        devices=None,
        groups=None,
        backup_devices=None,
        backup_groups=None,
    ):
        self.devices = DeviceRepository(devices if devices is not None else [])
        self.groups = GroupRepository(groups if groups is not None else [])
        self.backup_devices = DeviceRepository(backup_devices if backup_devices is not None else default_backup_devices())
        self.backup_groups = GroupRepository(backup_groups if backup_groups is not None else default_backup_groups())


_runtime_state_repository = StateRepository()


def get_runtime_state_repository() -> StateRepository:
    return _runtime_state_repository
