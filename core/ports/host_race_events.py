from __future__ import annotations

from typing import Callable, Protocol

from ..events import HostRaceEvent


HostRaceEventSink = Callable[[HostRaceEvent], None]


class HostRaceEventPort(Protocol):
    """Generic host-race event stream (RotorHazard or any third-party host)."""

    def start(self, event_sink: HostRaceEventSink) -> None:
        """Start forwarding host race events to `event_sink`."""

    def stop(self) -> None:
        """Stop forwarding events."""
