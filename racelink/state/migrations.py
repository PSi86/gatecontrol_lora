"""Schema migrations for persisted RaceLink state.

Per-version migration functions live here so that schema evolution has a single
anchor point. Add a new ``migrate_v{N}_to_v{N+1}`` function whenever
``CURRENT_SCHEMA_VERSION`` in ``persistence.py`` is bumped.
"""

from __future__ import annotations

import logging
from typing import Callable

from .persistence import CURRENT_SCHEMA_VERSION

logger = logging.getLogger(__name__)


def _noop(devices: list[dict], groups: list[dict]) -> tuple[list[dict], list[dict]]:
    return devices, groups


# Map ``from_version -> migration_callable`` producing the (devices, groups)
# tuple at ``from_version + 1``. Register new steps here.
_MIGRATIONS: dict[int, Callable[[list[dict], list[dict]], tuple[list[dict], list[dict]]]] = {}


def migrate_state(
    devices: list[dict],
    groups: list[dict],
    *,
    from_version: int,
    to_version: int = CURRENT_SCHEMA_VERSION,
) -> tuple[list[dict], list[dict], int]:
    """Apply all registered migrations to bring records up to ``to_version``.

    Returns ``(devices, groups, applied_version)``. If no migrations are
    registered between ``from_version`` and ``to_version`` the payloads are
    returned unchanged -- this is the common case today since the schema has
    only ever been at version 1.
    """
    version = max(0, int(from_version))
    target = max(version, int(to_version))
    while version < target:
        step = _MIGRATIONS.get(version, _noop)
        devices, groups = step(devices, groups)
        version += 1
    return devices, groups, version
