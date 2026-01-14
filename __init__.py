"""Created by Peter Simandl "psi" in 2025.
Works with Rotorhazard 4.0.
"""

import logging

from eventmanager import Evt

from .gatecontrol_webui import register_gc_blueprint
from .controller import GateControl_LoRa
from .data import (
    GC_Device,
    GC_DeviceGroup,
    GC_Type,
    GC_FLAG_POWER_ON,
    GC_FLAG_ARM_ON_SYNC,
    GC_FLAG_HAS_BRI,
    GC_FLAG_FORCE_TT0,
    GC_FLAG_FORCE_REAPPLY,
    gc_backup_devicelist,
    gc_backup_grouplist,
    gc_devicelist,
    gc_grouplist,
)

logger = logging.getLogger(__name__)


def initialize(rhapi):
    global gc_instance

    gc_instance = GateControl_LoRa(
        rhapi,
        "GateControl_LoRa",
        "GateControl",
    )

    register_gc_blueprint(
        rhapi,
        gc_instance=gc_instance,
        gc_devicelist=gc_devicelist,
        gc_grouplist=gc_grouplist,
        GC_DeviceGroup=GC_DeviceGroup,
        logger=logger,
    )

    rhapi.events.on(Evt.DATA_IMPORT_INITIALIZE, gc_instance.register_gc_dataimporter)
    rhapi.events.on(Evt.DATA_EXPORT_INITIALIZE, gc_instance.register_gc_dataexporter)
    rhapi.events.on(Evt.ACTIONS_INITIALIZE, gc_instance.registerActions)

    rhapi.events.on(Evt.STARTUP, gc_instance.onStartup)

    rhapi.events.on(Evt.RACE_START, gc_instance.onRaceStart)
    rhapi.events.on(Evt.RACE_FINISH, gc_instance.onRaceFinish)
    rhapi.events.on(Evt.RACE_STOP, gc_instance.onRaceStop)


__all__ = [
    "GC_Device",
    "GC_DeviceGroup",
    "GC_Type",
    "GC_FLAG_POWER_ON",
    "GC_FLAG_ARM_ON_SYNC",
    "GC_FLAG_HAS_BRI",
    "GC_FLAG_FORCE_TT0",
    "GC_FLAG_FORCE_REAPPLY",
    "GateControl_LoRa",
    "gc_backup_devicelist",
    "gc_backup_grouplist",
    "gc_devicelist",
    "gc_grouplist",
    "initialize",
]
