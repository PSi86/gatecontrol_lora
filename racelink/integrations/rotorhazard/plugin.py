"""RotorHazard plugin bootstrap for RaceLink.

RL-002 moves the RotorHazard-specific initialization flow out of the root
plugin module while keeping the existing runtime behavior unchanged.
"""

import logging

from eventmanager import Evt

from ...app import RaceLinkApp
from ...core import NullSink
from ...services import HostWifiService, OTAService, PresetsService
from ...state import get_runtime_state_repository
from ...web import register_rl_blueprint
from .ui import RotorHazardUIAdapter
from ....controller import RaceLink_LoRa
from ....data import (
    RL_DeviceGroup,
)

logger = logging.getLogger(__name__)

rl_app = None
rl_instance = None


def initialize(rhapi):
    global rl_app, rl_instance

    state_repository = get_runtime_state_repository()

    controller = RaceLink_LoRa(
        rhapi,
        "RaceLink_LoRa",
        "RaceLink",
        state_repository=state_repository,
    )
    rh_adapter = RotorHazardUIAdapter(controller, rhapi)
    controller.rh_adapter = rh_adapter
    controller.rh_source = rh_adapter.source
    presets_service = PresetsService(
        option_getter=rhapi.db.option,
        option_setter=rhapi.db.option_set,
        apply_options=rh_adapter.apply_presets_options,
    )
    host_wifi_service = HostWifiService()
    ota_service = OTAService(host_wifi_service=host_wifi_service, presets_service=presets_service)
    rl_app = RaceLinkApp(
        controller=controller,
        transport=getattr(controller, "lora", None),
        state_repository=state_repository,
        services={
            "config": controller.config_service,
            "control": controller.control_service,
            "gateway": controller.gateway_service,
            "discovery": controller.discovery_service,
            "host_wifi": host_wifi_service,
            "ota": ota_service,
            "presets": presets_service,
            "startblock": controller.startblock_service,
            "status": controller.status_service,
            "stream": controller.stream_service,
            "sync": controller.sync_service,
        },
        integrations={"rotorhazard": rhapi, "rotorhazard_ui": rh_adapter, "rotorhazard_source": rh_adapter.source},
        event_source=rh_adapter.source,
        data_sink=NullSink(),
    )
    rl_instance = rl_app.rl_instance

    register_rl_blueprint(
        rhapi,
        rl_instance=rl_app.rl_instance,
        state_repository=state_repository,
        services=rl_app.services,
        RL_DeviceGroup=RL_DeviceGroup,
        logger=logger,
    )

    rhapi.events.on(Evt.DATA_IMPORT_INITIALIZE, rh_adapter.register_rl_dataimporter)
    rhapi.events.on(Evt.DATA_EXPORT_INITIALIZE, rh_adapter.register_rl_dataexporter)
    rhapi.events.on(Evt.ACTIONS_INITIALIZE, rh_adapter.registerActions)

    rhapi.events.on(Evt.STARTUP, rl_app.rl_instance.onStartup)

    rhapi.events.on(Evt.RACE_START, rl_app.rl_instance.onRaceStart)
    rhapi.events.on(Evt.RACE_FINISH, rl_app.rl_instance.onRaceFinish)
    rhapi.events.on(Evt.RACE_STOP, rl_app.rl_instance.onRaceStop)
