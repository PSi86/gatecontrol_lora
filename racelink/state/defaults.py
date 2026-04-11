"""Default state values for RaceLink repositories."""

from __future__ import annotations

from ..domain.models import RL_DeviceGroup


def default_backup_devices() -> list:
    return []


def default_backup_groups() -> list[RL_DeviceGroup]:
    return [RL_DeviceGroup("All WLED Nodes", 1, 0)]
