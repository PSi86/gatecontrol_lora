from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class RaceLinkState:
    """Shared mutable state for RaceLink runtime data."""

    devices: List[object] = field(default_factory=list)
    groups: List[object] = field(default_factory=list)
    backup_devices: List[object] = field(default_factory=list)
    backup_groups: List[object] = field(default_factory=list)
    _groups_by_capability: Dict[str, List[object]] = field(default_factory=dict, init=False, repr=False)

    def find_device_by_addr(self, addr: str | None) -> Optional[object]:
        if not addr:
            return None
        norm = str(addr).upper()
        for dev in self.devices:
            if str(getattr(dev, "addr", "")).upper() == norm:
                return dev
        return None

    def groups_for_capability(self, capability: str) -> List[object]:
        if capability not in self._groups_by_capability:
            self.update_group_cache()
        return list(self._groups_by_capability.get(capability, []))

    def update_group_cache(self) -> Dict[str, List[object]]:
        by_cap: Dict[str, List[object]] = {}
        for grp in self.groups:
            dev_type = int(getattr(grp, "dev_type", 0) or 0)
            caps = self._caps_for_dev_type(dev_type)
            for cap in caps:
                by_cap.setdefault(cap, []).append(grp)
        self._groups_by_capability = by_cap
        return by_cap

    @staticmethod
    def _caps_for_dev_type(type_id: int) -> List[str]:
        try:
            from ..data import get_dev_type_info  # local import to avoid circular import at module load

            info = get_dev_type_info(type_id)
            return list(info.get("caps", []))
        except Exception:
            return []
