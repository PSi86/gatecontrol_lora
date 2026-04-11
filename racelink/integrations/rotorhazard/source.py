"""RotorHazard-specific data source helpers."""

from __future__ import annotations

import json

from ...core import EventSource


class RotorHazardSource(EventSource):
    source_name = "rotorhazard"

    def __init__(self, controller, rhapi):
        self.controller = controller
        self.rhapi = rhapi

    def describe(self):
        return {
            "name": self.source_name,
            "kind": "rotorhazard",
            "has_rhapi": self.rhapi is not None,
        }

    def snapshot(self):
        return {"current_heat_slots": self.get_current_heat_slot_list()}

    def get_current_heat_slot_list(self):
        freq = json.loads(self.rhapi.race.frequencyset.frequencies)
        bands = freq["b"]
        channels = freq["c"]
        racechannels = [
            "--" if band is None else f"{band}{channels[i]}"
            for i, band in enumerate(bands)
        ]

        ctx = self.rhapi._racecontext
        rhdata = ctx.rhdata
        race = ctx.race
        heat_nodes = rhdata.get_heatNodes_by_heat(race.current_heat) or []

        callsign_by_slot = {}
        for hn in heat_nodes:
            slot = int(getattr(hn, "node_index"))
            pid = getattr(hn, "pilot_id", None)
            p = rhdata.get_pilot(pid) if pid else None
            callsign_by_slot[slot] = p.callsign if p else ""

        n = min(len(racechannels), 8)
        return [(i, callsign_by_slot.get(i, ""), racechannels[i]) for i in range(n)]
