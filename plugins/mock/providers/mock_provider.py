from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.ports.race_provider import RaceProviderPort
else:
    RaceProviderPort = object


class MockRaceProvider(RaceProviderPort):
    """Minimal reference implementation compatible with RaceProviderPort."""

    def __init__(
        self,
        current_heat: int | None = 1,
        pilot_assignments: list[tuple[int, str]] | None = None,
        channels: list[str] | None = None,
    ):
        self._current_heat = current_heat
        self._pilot_assignments = list(
            pilot_assignments
            or [
                (0, "ALPHA"),
                (1, "BRAVO"),
                (2, "CHARLIE"),
                (3, "DELTA"),
            ]
        )
        self._channels = list(channels or ["R1", "R2", "R3", "R4", "--", "--", "--", "--"])

    def get_current_heat(self) -> int | None:
        return self._current_heat

    def get_pilot_assignments(self) -> list[tuple[int, str]]:
        return list(self._pilot_assignments)

    def get_frequency_channels(self) -> list[str]:
        return list(self._channels)


class MockPollingRaceEventAdapter:
    """Optional inert polling adapter that only logs poll activity."""

    def __init__(self, logger: logging.Logger | None = None):
        self._logger = logger or logging.getLogger(__name__)
        self._running = False

    def start(self, _event_sink: Any | None = None) -> None:
        self._running = True
        self._logger.info("Mock polling adapter started (inert)")

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self._logger.info("Mock polling adapter stopped (inert)")

    def poll_once(self) -> None:
        self._logger.debug("Mock polling adapter polled (inert)")
