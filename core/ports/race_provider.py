from __future__ import annotations

from typing import Protocol


PilotAssignment = tuple[int, str]


class RaceProviderPort(Protocol):
    """Abstraction over race runtime (RotorHazard or third-party software)."""

    def get_current_heat(self) -> int | None:
        """Return current heat identifier or ``None`` if unavailable."""

    def get_pilot_assignments(self) -> list[PilotAssignment]:
        """Return list of ``(node_index, pilot_callsign)`` assignments for current heat."""

    def get_frequency_channels(self) -> list[str]:
        """Return race channel labels in node order (e.g. ``R1``, ``F4``)."""
