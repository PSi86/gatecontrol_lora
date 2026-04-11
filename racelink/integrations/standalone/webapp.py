"""Standalone Flask app factory for RaceLink."""

from __future__ import annotations

import logging
from types import SimpleNamespace

from flask import Flask

from ...app import RaceLinkApp
from ...core import NullSink, NullSource
from ...services import HostWifiService, OTAService, PresetsService
from ...state import get_runtime_state_repository
from ...web import register_rl_blueprint
from ....controller import RaceLink_LoRa
from ....data import RL_DeviceGroup
from .config import StandaloneConfig, StandaloneOptionStore

logger = logging.getLogger(__name__)


class _StandaloneUiShim:
    def __init__(self, app: Flask):
        self._app = app

    def blueprint_add(self, blueprint):
        if blueprint.name not in self._app.blueprints:
            self._app.register_blueprint(blueprint)

    def message_notify(self, message):
        logger.info("RaceLink standalone: %s", message)

    def broadcast_ui(self, *_args, **_kwargs):
        return None

    def register_panel(self, *_args, **_kwargs):
        return None

    def register_quickbutton(self, *_args, **_kwargs):
        return None


class _StandaloneFieldsShim:
    def register_option(self, *_args, **_kwargs):
        return None


class StandaloneRhApiShim:
    """Small RotorHazard-shaped shim so the existing controller/web code can run standalone."""

    def __init__(self, app: Flask, config: StandaloneConfig):
        self.app = app
        self.config = config
        self.db = StandaloneOptionStore(config)
        self.ui = _StandaloneUiShim(app)
        self.fields = _StandaloneFieldsShim()
        self.race = SimpleNamespace(frequencyset=SimpleNamespace(frequencies='{"b":[],"c":[]}'))
        self._racecontext = SimpleNamespace(rhdata=None, race=SimpleNamespace(current_heat=0))

    def __(self, text):
        return text


def create_standalone_app(config: StandaloneConfig | None = None) -> tuple[Flask, RaceLinkApp]:
    cfg = config or StandaloneConfig.load()
    app = Flask("racelink_standalone")
    rhapi = StandaloneRhApiShim(app, cfg)
    state_repository = get_runtime_state_repository()

    controller = RaceLink_LoRa(
        rhapi,
        "RaceLink_LoRa",
        "RaceLink",
        state_repository=state_repository,
    )
    controller.rh_source = NullSource()

    presets_service = PresetsService(
        option_getter=rhapi.db.option,
        option_setter=rhapi.db.option_set,
    )
    host_wifi_service = HostWifiService()
    ota_service = OTAService(host_wifi_service=host_wifi_service, presets_service=presets_service)

    rl_app = RaceLinkApp(
        controller=controller,
        transport=getattr(controller, "lora", None),
        state_repository=state_repository,
        services={
            "gateway": controller.gateway_service,
            "discovery": controller.discovery_service,
            "host_wifi": host_wifi_service,
            "ota": ota_service,
            "presets": presets_service,
            "status": controller.status_service,
        },
        integrations={"standalone": rhapi, "flask_app": app},
        event_source=NullSource(),
        data_sink=NullSink(),
    )

    register_rl_blueprint(
        rhapi,
        rl_instance=rl_app.rl_instance,
        state_repository=state_repository,
        services=rl_app.services,
        RL_DeviceGroup=RL_DeviceGroup,
        logger=logger,
    )

    @app.route("/")
    def index():
        return "", 302, {"Location": "/racelink"}

    return app, rl_app


def run_standalone(config: StandaloneConfig | None = None):
    cfg = config or StandaloneConfig.load()
    app, rl_app = create_standalone_app(cfg)
    try:
        rl_app.rl_instance.onStartup({})
    except Exception as ex:  # pragma: no cover
        logger.warning("RaceLink standalone startup encountered an issue: %s", ex)
    app.run(host=cfg.host, port=cfg.port, debug=cfg.debug)
    return app, rl_app
