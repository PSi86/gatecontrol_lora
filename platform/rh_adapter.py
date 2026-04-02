from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Callable

from eventmanager import Evt

from ..controller import RaceLink_LoRa
from ..core.repository import InMemoryDeviceRepository
from ..data import RL_DeviceGroup
from ..racelink_webui import register_rl_blueprint
from .ports import ConfigStorePort, EventBusPort, RacePilotDataProviderPort, UINotificationPort

logger = logging.getLogger(__name__)


class RHEventBus(EventBusPort):
    def __init__(self, rhapi):
        self._rhapi = rhapi
        self._listeners: dict[str, list[Callable[[Any], None]]] = defaultdict(list)

    def subscribe(self, event_name: str, handler: Callable[[Any], None]) -> None:
        self._listeners[event_name].append(handler)
        self._rhapi.events.on(event_name, handler)

    def publish(self, event_name: str, payload: Any = None) -> None:
        trigger = getattr(self._rhapi.events, "trigger", None)
        if callable(trigger):
            trigger(event_name, payload)
            return

        for handler in list(self._listeners.get(event_name, [])):
            handler(payload)


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
        # RH API typically offers `notify`; keep fallback to logger.
        notifier = getattr(self._rhapi.ui, "notify", None)
        if callable(notifier):
            notifier(message, level)
        else:
            logger.info("[%s] %s", level.upper(), message)

    def broadcast_ui(self, panel: str) -> None:
        self._rhapi.ui.broadcast_ui(panel)


class RHRacePilotDataProvider(RacePilotDataProviderPort):
    def __init__(self, rhapi):
        self._rhapi = rhapi

    def get_current_heat_slot_list(self) -> list[tuple[int, str, str]]:
        # Delegated to service via rhapi object in current architecture.
        return []


class RotorHazardAdapter:
    """Adapter wiring RaceLink to RotorHazard runtime (`rhapi`)."""

    def __init__(self, rhapi):
        self.rhapi = rhapi
        self.repository = InMemoryDeviceRepository()
        self.event_bus = RHEventBus(rhapi)
        self.config_store = RHConfigStore(rhapi)
        self.ui = RHUINotifier(rhapi)
        self.race_data = RHRacePilotDataProvider(rhapi)
        self.rl_instance: RaceLink_LoRa | None = None

    def initialize(self) -> RaceLink_LoRa:
        self.rl_instance = RaceLink_LoRa(
            self.rhapi,
            "RaceLink_LoRa",
            "RaceLink",
            repository=self.repository,
        )

        register_rl_blueprint(
            self.rhapi,
            rl_instance=self.rl_instance,
            rl_devicelist=self.repository.device_items,
            rl_grouplist=self.repository.group_items,
            RL_DeviceGroup=RL_DeviceGroup,
            logger=logger,
        )

        self.event_bus.subscribe(Evt.DATA_IMPORT_INITIALIZE, self.rl_instance.register_rl_dataimporter)
        self.event_bus.subscribe(Evt.DATA_EXPORT_INITIALIZE, self.rl_instance.register_rl_dataexporter)
        self.event_bus.subscribe(Evt.ACTIONS_INITIALIZE, self.rl_instance.registerActions)
        self.event_bus.subscribe(Evt.STARTUP, self.rl_instance.onStartup)
        self.event_bus.subscribe(Evt.RACE_START, self.rl_instance.onRaceStart)
        self.event_bus.subscribe(Evt.RACE_FINISH, self.rl_instance.onRaceFinish)
        self.event_bus.subscribe(Evt.RACE_STOP, self.rl_instance.onRaceStop)

        return self.rl_instance
