from __future__ import annotations

import json
from typing import Callable

from ..core import events as core_events
from ..core.event_bus import InMemoryEventBus
from ..core.ports.race_provider import RaceProviderPort


class RotorHazardRaceProvider(RaceProviderPort):
    """RaceProvider implementation backed by RotorHazard ``rhapi``."""

    def __init__(self, rhapi, event_bus=None):
        self._rhapi = rhapi
        self._event_bus = event_bus or InMemoryEventBus()

    def get_current_heat(self) -> int | None:
        race = getattr(self._rhapi, "race", None)
        return getattr(race, "current_heat", None)

    def get_pilot_assignments(self) -> list[tuple[int, str]]:
        ctx = getattr(self._rhapi, "_racecontext", None)
        if not ctx:
            return []

        current_heat = self.get_current_heat()
        if current_heat is None:
            return []

        heat_nodes = ctx.rhdata.get_heatNodes_by_heat(current_heat) or []
        assignments: list[tuple[int, str]] = []
        for heat_node in heat_nodes:
            slot = int(getattr(heat_node, "node_index", 0))
            pilot_id = getattr(heat_node, "pilot_id", None)
            pilot = ctx.rhdata.get_pilot(pilot_id) if pilot_id else None
            assignments.append((slot, str(getattr(pilot, "callsign", "") or "")))
        return assignments

    def get_frequency_channels(self) -> list[str]:
        race = getattr(self._rhapi, "race", None)
        frequencyset = getattr(race, "frequencyset", None)
        frequencies_raw = getattr(frequencyset, "frequencies", None)
        if not frequencies_raw:
            return []

        freq = json.loads(frequencies_raw)
        bands = freq.get("b", [])
        channels = freq.get("c", [])
        return ["--" if band is None else f"{band}{channels[i]}" for i, band in enumerate(bands)]

    def on_race_start(self, handler: Callable[[object], None]) -> None:
        self._event_bus.subscribe(core_events.RACE_STARTED, handler)

    def on_race_finish(self, handler: Callable[[object], None]) -> None:
        self._event_bus.subscribe(core_events.RACE_FINISHED, handler)

    def on_race_stop(self, handler: Callable[[object], None]) -> None:
        self._event_bus.subscribe(core_events.RACE_STOPPED, handler)
