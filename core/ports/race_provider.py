from __future__ import annotations

from typing import Callable, Protocol


PilotAssignment = tuple[int, str]
RaceStartHandler = Callable[[object], None]


class RaceProviderPort(Protocol):
    """Abstraction over race runtime (RotorHazard or third-party software)."""

    def get_current_heat(self) -> int | None:
        """Return current heat identifier or ``None`` if unavailable."""

    def get_pilot_assignments(self) -> list[PilotAssignment]:
        """Return list of ``(node_index, pilot_callsign)`` assignments for current heat."""

    def get_frequency_channels(self) -> list[str]:
        """Return race channel labels in node order (e.g. ``R1``, ``F4``)."""

    def on_race_start(self, handler: RaceStartHandler) -> None:
        """Register callback for race-start notifications."""

    def on_race_finish(self, handler: RaceStartHandler) -> None:
        """Register callback for race-finish notifications."""

    def on_race_stop(self, handler: RaceStartHandler) -> None:
        """Register callback for race-stop notifications."""
