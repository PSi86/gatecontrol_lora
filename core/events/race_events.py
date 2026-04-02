from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


class HostRaceEventType(StrEnum):
    RACE_STARTED = "RACE_STARTED"
    RACE_FINISHED = "RACE_FINISHED"
    RACE_STOPPED = "RACE_STOPPED"
    RACE_SNAPSHOT = "RACE_SNAPSHOT"


@dataclass(slots=True)
class HostRaceEvent:
    type: HostRaceEventType
    payload: dict[str, Any] = field(default_factory=dict)
    ts_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
