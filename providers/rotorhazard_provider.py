from __future__ import annotations

import json

from eventmanager import Evt

from ..core.events import HostRaceEvent, HostRaceEventType
from ..core.ports.host_race_events import HostRaceEventSink
from ..core.ports.race_provider import RaceProviderPort


class RotorHazardRaceProvider(RaceProviderPort):
    """RaceProvider implementation backed by RotorHazard ``rhapi``."""

    def __init__(self, rhapi):
        self._rhapi = rhapi

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


class RotorHazardRaceEventAdapter:
    """Maps RotorHazard `Evt.*` events to the internal host-race event model."""

    def __init__(self, rhapi):
        self._rhapi = rhapi
        self._sink: HostRaceEventSink | None = None

    def start(self, event_sink: HostRaceEventSink) -> None:
        self._sink = event_sink
        self._rhapi.events.on(Evt.RACE_START, self._on_race_start)
        self._rhapi.events.on(Evt.RACE_FINISH, self._on_race_finish)
        self._rhapi.events.on(Evt.RACE_STOP, self._on_race_stop)

    def stop(self) -> None:
        self._sink = None

    def _emit(self, event_type: HostRaceEventType, payload: object = None) -> None:
        if self._sink is None:
            return
        self._sink(HostRaceEvent(type=event_type, payload={"source_payload": payload}))

    def _on_race_start(self, payload=None) -> None:
        self._emit(HostRaceEventType.RACE_STARTED, payload)

    def _on_race_finish(self, payload=None) -> None:
        self._emit(HostRaceEventType.RACE_FINISHED, payload)

    def _on_race_stop(self, payload=None) -> None:
        self._emit(HostRaceEventType.RACE_STOPPED, payload)
