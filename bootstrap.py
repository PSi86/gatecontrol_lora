"""Plugin bootstrap helpers for RaceLink LoRa."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from eventmanager import Evt

from .controller import RaceLink_LoRa
from .data import RL_DeviceGroup, rl_devicelist, rl_grouplist
from .racelink_webui import register_rl_blueprint

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PluginComponents:
    """Container for plugin component wiring created at bootstrap time."""

    state: dict[str, Any]
    repository: dict[str, Any]
    services: dict[str, Any]
    controller: RaceLink_LoRa
    ui: dict[str, Any]


def _register_events(rhapi, rl_instance: RaceLink_LoRa) -> None:
    rhapi.events.on(Evt.DATA_IMPORT_INITIALIZE, rl_instance.register_rl_dataimporter)
    rhapi.events.on(Evt.DATA_EXPORT_INITIALIZE, rl_instance.register_rl_dataexporter)
    rhapi.events.on(Evt.ACTIONS_INITIALIZE, rl_instance.registerActions)

    rhapi.events.on(Evt.STARTUP, rl_instance.onStartup)

    rhapi.events.on(Evt.RACE_START, rl_instance.onRaceStart)
    rhapi.events.on(Evt.RACE_FINISH, rl_instance.onRaceFinish)
    rhapi.events.on(Evt.RACE_STOP, rl_instance.onRaceStop)


def build_plugin(rhapi) -> PluginComponents:
    """Build and wire all plugin components."""
    state = {
        "name": "RaceLink_LoRa",
        "label": "RaceLink",
    }

    repository = {
        "devices": rl_devicelist,
        "groups": rl_grouplist,
        "group_type": RL_DeviceGroup,
    }

    controller = RaceLink_LoRa(rhapi, state["name"], state["label"])

    ui = {
        "register_blueprint": register_rl_blueprint,
    }
    ui["register_blueprint"](
        rhapi,
        rl_instance=controller,
        rl_devicelist=repository["devices"],
        rl_grouplist=repository["groups"],
        RL_DeviceGroup=repository["group_type"],
        logger=logger,
    )

    services = {
        "events": _register_events,
    }
    services["events"](rhapi, controller)

    return PluginComponents(
        state=state,
        repository=repository,
        services=services,
        controller=controller,
        ui=ui,
    )
