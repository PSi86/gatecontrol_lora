from __future__ import annotations

from typing import Callable

from ..core.ports.race_provider import RaceProviderPort


class MockRaceProvider(RaceProviderPort):
    """Reference provider for integrating non-RotorHazard software."""

    def __init__(self):
        self._current_heat: int | None = 1
        self._pilot_assignments: list[tuple[int, str]] = [
            (0, "ALPHA"),
            (1, "BRAVO"),
            (2, "CHARLIE"),
            (3, "DELTA"),
        ]
        self._channels: list[str] = ["R1", "R2", "R3", "R4", "--", "--", "--", "--"]
        self._start_handlers: list[Callable[[object], None]] = []
        self._finish_handlers: list[Callable[[object], None]] = []
        self._stop_handlers: list[Callable[[object], None]] = []

    def get_current_heat(self) -> int | None:
        return self._current_heat

    def get_pilot_assignments(self) -> list[tuple[int, str]]:
        return list(self._pilot_assignments)

    def get_frequency_channels(self) -> list[str]:
        return list(self._channels)

    def on_race_start(self, handler: Callable[[object], None]) -> None:
        self._start_handlers.append(handler)

    def on_race_finish(self, handler: Callable[[object], None]) -> None:
        self._finish_handlers.append(handler)

    def on_race_stop(self, handler: Callable[[object], None]) -> None:
        self._stop_handlers.append(handler)

    # Helper methods for tests / reference integrations
    def emit_race_start(self, payload: object = None) -> None:
        for handler in list(self._start_handlers):
            handler(payload)

    def emit_race_finish(self, payload: object = None) -> None:
        for handler in list(self._finish_handlers):
            handler(payload)

    def emit_race_stop(self, payload: object = None) -> None:
        for handler in list(self._stop_handlers):
            handler(payload)
