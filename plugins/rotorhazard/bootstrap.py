from __future__ import annotations

from ...controller import RaceLink_LoRa
from . import RotorHazardPlugin


class RotorHazardAdapter:
    """Compatibility adapter delegating RH runtime wiring to `RotorHazardPlugin`."""

    def __init__(self, rhapi, feature_flags=None):
        self.rhapi = rhapi
        self.feature_flags = feature_flags or {}
        self.plugin: RotorHazardPlugin | None = None

    def initialize(self) -> RaceLink_LoRa:
        self.plugin = RotorHazardPlugin.build(self.rhapi, feature_flags=self.feature_flags)
        return self.plugin.start()
