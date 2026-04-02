"""Created by Peter Simandl "PSi86" in 2026.
Works with Rotorhazard 4.0.
"""

import logging

from eventmanager import Evt

from .core.repository import InMemoryDeviceRepository
from .racelink_webui import register_rl_blueprint
from .controller import RaceLink_LoRa
from .data import (
    RL_Device,
    RL_DeviceGroup,
    RL_Dev_Type,
    RL_FLAG_POWER_ON,
    RL_FLAG_ARM_ON_SYNC,
    RL_FLAG_HAS_BRI,
    RL_FLAG_FORCE_TT0,
    RL_FLAG_FORCE_REAPPLY,
)

logger = logging.getLogger(__name__)


def initialize(rhapi):
    global rl_instance

    repository = InMemoryDeviceRepository()
    rl_instance = RaceLink_LoRa(
        rhapi,
        "RaceLink_LoRa",
        "RaceLink",
        repository=repository,
    )

    register_rl_blueprint(
        rhapi,
        rl_instance=rl_instance,
        rl_devicelist=repository.device_items,
        rl_grouplist=repository.group_items,
        RL_DeviceGroup=RL_DeviceGroup,
        logger=logger,
    )

    rhapi.events.on(Evt.DATA_IMPORT_INITIALIZE, rl_instance.register_rl_dataimporter)
    rhapi.events.on(Evt.DATA_EXPORT_INITIALIZE, rl_instance.register_rl_dataexporter)
    rhapi.events.on(Evt.ACTIONS_INITIALIZE, rl_instance.registerActions)

    rhapi.events.on(Evt.STARTUP, rl_instance.onStartup)

    rhapi.events.on(Evt.RACE_START, rl_instance.onRaceStart)
    rhapi.events.on(Evt.RACE_FINISH, rl_instance.onRaceFinish)
    rhapi.events.on(Evt.RACE_STOP, rl_instance.onRaceStop)


__all__ = [
    "RL_Device",
    "RL_DeviceGroup",
    "RL_Dev_Type",
    "RL_FLAG_POWER_ON",
    "RL_FLAG_ARM_ON_SYNC",
    "RL_FLAG_HAS_BRI",
    "RL_FLAG_FORCE_TT0",
    "RL_FLAG_FORCE_REAPPLY",
    "RaceLink_LoRa",
    "initialize",
]
