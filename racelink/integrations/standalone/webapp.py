"""Standalone Flask app factory for RaceLink."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Optional

from flask import Flask

from ...app import RaceLinkApp, create_runtime
from ...core import HostApi, HostEventBus, HostOptionStore, HostUiNotifier, NullSink, NullSource
from ...domain import RL_DeviceGroup
from ...web import RaceLinkWebRuntime, register_racelink_web
from controller import RaceLink_Host
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


class StandaloneHostApiShim:
    """Host-API shim so the shared controller and web code can run standalone.

    Satisfies :class:`racelink.core.HostApi` structurally: exposes ``db`` (an
    option store), an optional ``ui`` surface, and a callable ``__`` for
    translations. ``events`` is deliberately ``None`` because the standalone
    runtime does not emit lifecycle events.
    """

    # Explicit type annotations aid static checkers verifying HostApi
    # compliance without forcing nominal inheritance.
    db: HostOptionStore
    ui: Optional[HostUiNotifier]
    events: Optional[HostEventBus]

    def __init__(self, app: Flask, config: StandaloneConfig):
        self.app = app
        self.config = config
        self.db = StandaloneOptionStore(config)
        self.ui = _StandaloneUiShim(app)
        self.events = None
        self.fields = _StandaloneFieldsShim()
        self.race = SimpleNamespace(frequencyset=SimpleNamespace(frequencies='{"b":[],"c":[]}'))
        self._racecontext = SimpleNamespace(rhdata=None, race=SimpleNamespace(current_heat=0))

    def __(self, text: str) -> str:
        return text


def create_standalone_app(config: StandaloneConfig | None = None) -> tuple[Flask, RaceLinkApp]:
    cfg = config or StandaloneConfig.load()
    app = Flask("racelink_standalone")
    host_api: HostApi = StandaloneHostApiShim(app, cfg)
    event_source = NullSource()
    data_sink = NullSink()
    # Auxiliary slots carried through the shim for downstream services;
    # not part of HostApi itself but tolerated by structural typing.
    host_api.event_source = event_source  # type: ignore[attr-defined]
    host_api.data_sink = data_sink  # type: ignore[attr-defined]

    rl_app = create_runtime(
        host_api,
        controller_class=RaceLink_Host,
        integrations={"standalone": host_api, "flask_app": app},
        event_source=event_source,
        data_sink=data_sink,
    )
    state_repository = rl_app.state_repository

    web_runtime = RaceLinkWebRuntime(
        rl_instance=rl_app.rl_instance,
        state_repository=state_repository,
        rl_devicelist=None,
        rl_grouplist=None,
        services=rl_app.services,
        RL_DeviceGroup=RL_DeviceGroup,
        logger=logger,
        option_getter=host_api.db.option,
        translator=host_api.__,
    )
    register_racelink_web(app, web_runtime, url_prefix="/racelink")

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
