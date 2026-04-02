from __future__ import annotations

import logging
from typing import Any

from ..controller import RaceLink_LoRa
from ..core import events as core_events
from ..core.event_bus import InMemoryEventBus
from ..core.repository import InMemoryDeviceRepository
from ..data import RL_DeviceGroup
from ..integrations.rotorhazard.event_bridge import RHEventBridge
from ..racelink_webui import register_rl_blueprint
from ..providers.rotorhazard_provider import RotorHazardRaceProvider
from .ports import ConfigStorePort, UINotificationPort

logger = logging.getLogger(__name__)


class RHConfigStore(ConfigStorePort):
    def __init__(self, rhapi):
        self._rhapi = rhapi

    def get(self, key: str, default: Any = None) -> Any:
        return self._rhapi.db.option(key, default)

    def set(self, key: str, value: Any) -> None:
        self._rhapi.db.option_set(key, value)


class RHUINotifier(UINotificationPort):
    def __init__(self, rhapi):
        self._rhapi = rhapi

    def notify(self, message: str, level: str = "info") -> None:
        notifier = getattr(self._rhapi.ui, "notify", None)
        if callable(notifier):
            notifier(message, level)
        else:
            logger.info("[%s] %s", level.upper(), message)

    def broadcast_ui(self, panel: str) -> None:
        self._rhapi.ui.broadcast_ui(panel)


class RotorHazardAdapter:
    """Adapter wiring RaceLink to RotorHazard runtime (`rhapi`)."""

    def __init__(self, rhapi):
        self.rhapi = rhapi
        self.repository = InMemoryDeviceRepository()
        self.event_bus = InMemoryEventBus()
        self.rh_event_bridge = RHEventBridge(rhapi, self.event_bus)
        self.config_store = RHConfigStore(rhapi)
        self.ui = RHUINotifier(rhapi)
        self.race_provider = RotorHazardRaceProvider(rhapi, self.event_bus)
        self.rl_instance: RaceLink_LoRa | None = None

    def initialize(self) -> RaceLink_LoRa:
        self.rl_instance = RaceLink_LoRa(
            self.rhapi,
            "RaceLink_LoRa",
            "RaceLink",
            repository=self.repository,
            race_provider=self.race_provider,
        )

        register_rl_blueprint(
            self.rhapi,
            rl_instance=self.rl_instance,
            rl_devicelist=self.repository.device_items,
            rl_grouplist=self.repository.group_items,
            RL_DeviceGroup=RL_DeviceGroup,
            logger=logger,
        )

        self.event_bus.subscribe(core_events.DATA_IMPORT_INITIALIZE, self.rl_instance.register_rl_dataimporter)
        self.event_bus.subscribe(core_events.DATA_EXPORT_INITIALIZE, self.rl_instance.register_rl_dataexporter)
        self.event_bus.subscribe(core_events.ACTIONS_INITIALIZE, self.rl_instance.registerActions)
        self.event_bus.subscribe(core_events.STARTUP, self.rl_instance.onStartup)
        self.race_provider.on_race_start(self.rl_instance.onRaceStart)
        self.race_provider.on_race_finish(self.rl_instance.onRaceFinish)
        self.race_provider.on_race_stop(self.rl_instance.onRaceStop)

        self.rh_event_bridge.install()
        return self.rl_instance
