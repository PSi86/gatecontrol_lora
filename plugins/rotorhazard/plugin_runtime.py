from __future__ import annotations

import logging

from eventmanager import Evt

from ...controller import RaceLink_LoRa
from ...core.app import RaceLinkApp
from ...core.repository import InMemoryDeviceRepository
from ...data import RL_DeviceGroup
from ...providers.rotorhazard_provider import RotorHazardRaceProvider
from ...racelink_webui import register_rl_blueprint
from .ui import RotorHazardHostUIAdapter

logger = logging.getLogger(__name__)


class RotorHazardPluginRuntime:
    """RH-specific facade that owns `rhapi` and bridges RH events to RaceLinkApp callbacks."""

    def __init__(self, rhapi):
        self.rhapi = rhapi
        self.repository = InMemoryDeviceRepository()
        self.race_provider = RotorHazardRaceProvider(rhapi)
        self.controller: RaceLink_LoRa | None = None
        self.app: RaceLinkApp | None = None

    def initialize(self) -> RaceLink_LoRa:
        self.controller = RaceLink_LoRa(
            self.rhapi,
            "RaceLink_LoRa",
            "RaceLink",
            repository=self.repository,
            race_provider=self.race_provider,
        )
        self.app = self.controller.app
        host_ui = RotorHazardHostUIAdapter(self.controller)
        self.controller.bind_host_ui(host_ui)

        register_rl_blueprint(
            self.rhapi,
            rl_instance=self.controller,
            rl_devicelist=self.repository.device_items,
            rl_grouplist=self.repository.group_items,
            RL_DeviceGroup=RL_DeviceGroup,
            logger=logger,
        )

        self.rhapi.events.on(Evt.DATA_IMPORT_INITIALIZE, self.controller.host_ui.register_rl_dataimporter)
        self.rhapi.events.on(Evt.DATA_EXPORT_INITIALIZE, self.controller.host_ui.register_rl_dataexporter)
        self.rhapi.events.on(Evt.ACTIONS_INITIALIZE, self.controller.host_ui.registerActions)
        self.rhapi.events.on(Evt.STARTUP, self.controller.onStartup)

        # RH callbacks forward into app callbacks (host-agnostic app remains RH-unaware).
        self.race_provider.on_race_start(lambda args: self.app.on_race_start(args) if self.app else None)
        self.race_provider.on_race_finish(lambda args: self.app.on_race_finish(args) if self.app else None)
        self.race_provider.on_race_stop(lambda args: self.app.on_race_stop(args) if self.app else None)

        return self.controller
