"""Core event, source, and sink abstractions for RaceLink."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class AppEvent:
    """Simple application-level event envelope for future integrations."""

    name: str
    payload: Dict[str, Any] = field(default_factory=dict)
    source: str = "unknown"


class EventSource:
    """Adapter interface for external systems providing RaceLink data."""

    source_name = "source"

    def describe(self) -> Dict[str, Any]:
        return {"name": self.source_name}

    def get_current_heat_slot_list(self) -> List[tuple]:
        return []

    def snapshot(self) -> Dict[str, Any]:
        return {}

    def emit_events(self) -> List[AppEvent]:
        return []


class DataSink:
    """Adapter interface for external systems consuming RaceLink data."""

    sink_name = "sink"

    def describe(self) -> Dict[str, Any]:
        return {"name": self.sink_name}

    def publish(self, event: AppEvent) -> None:
        return None

    def flush(self) -> None:
        return None


class NullSource(EventSource):
    """No-op event source used when no external producer is wired."""

    source_name = "null"


class NullSink(DataSink):
    """No-op data sink used when no external consumer is wired."""

    sink_name = "null"
