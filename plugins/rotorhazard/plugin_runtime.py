from __future__ import annotations

from ...controller import RaceLink_LoRa
from ...core.app import RaceLinkApp
from ...core.repository import InMemoryDeviceRepository
from .providers import RotorHazardRaceEventAdapter, RotorHazardRaceProvider
from .features import config_io, events, ui_extensions, web_blueprint
from .ui import RotorHazardHostUIAdapter


class RotorHazardPlugin:
    """RH plugin composition root.

    Owns RH-specific wiring and feature activation; core app only receives abstract ports.
    """

    def __init__(self, rhapi, app: RaceLinkApp, controller: RaceLink_LoRa, repository: InMemoryDeviceRepository, feature_flags=None):
        self.rhapi = rhapi
        self.app = app
        self.controller = controller
        self.repository = repository
        self.feature_flags = {
            "events": True,
            "ui_extensions": True,
            "web_blueprint": True,
            "config_io": True,
            **(feature_flags or {}),
        }

    @classmethod
    def build(cls, rhapi, *, feature_flags=None) -> "RotorHazardPlugin":
        repository = InMemoryDeviceRepository()
        race_provider = RotorHazardRaceProvider(rhapi)
        race_events = RotorHazardRaceEventAdapter(rhapi)

        controller = RaceLink_LoRa(
            rhapi,
            "RaceLink_LoRa",
            "RaceLink",
            repository=repository,
            race_provider=race_provider,
            race_event_port=race_events,
        )
        controller.bind_host_ui(RotorHazardHostUIAdapter(controller))

        return cls(
            rhapi=rhapi,
            app=controller.app,
            controller=controller,
            repository=repository,
            feature_flags=feature_flags,
        )

    def start(self) -> RaceLink_LoRa:
        if self.feature_flags.get("web_blueprint", True):
            web_blueprint.activate(self)
        if self.feature_flags.get("config_io", True):
            config_io.activate(self)
        if self.feature_flags.get("ui_extensions", True):
            ui_extensions.activate(self)
        if self.feature_flags.get("events", True):
            events.activate(self)
        self.app.start_event_stream()

        return self.controller

    def stop(self) -> None:
        self.app.stop_event_stream()


# Backward-compatible alias for older imports.
RotorHazardPluginRuntime = RotorHazardPlugin
