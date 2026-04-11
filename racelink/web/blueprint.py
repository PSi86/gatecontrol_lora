"""Blueprint assembly for the RaceLink web layer."""

from __future__ import annotations

import os

from flask import Blueprint, templating

from ..services import HostWifiService, OTAService, PresetsService
from .api import register_api_routes
from .sse import _DefaultLock, SSEBridge
from .tasks import TaskManager


class _WebContext:
    def __init__(
        self,
        *,
        rhapi,
        rl_instance,
        state_repository,
        rl_devicelist,
        rl_grouplist,
        services,
        RL_DeviceGroup,
        logger,
    ):
        self.rhapi = rhapi
        self.rl_instance = rl_instance
        self.state_repository = state_repository
        self.rl_devicelist = rl_devicelist
        self.rl_grouplist = rl_grouplist
        self.services = services or {}
        self.RL_DeviceGroup = RL_DeviceGroup
        self.logger = logger
        self.rl_lock = _DefaultLock()
        self.device_repo = getattr(state_repository, "devices", None)
        self.group_repo = getattr(state_repository, "groups", None)

    def default_lock_factory(self):
        return _DefaultLock()

    def log(self, msg):
        try:
            if self.logger:
                self.logger.info(msg)
            else:
                print(msg)
        except Exception:
            print(msg)

    def devices(self):
        if self.device_repo is not None:
            return self.device_repo.list()
        return self.rl_devicelist if self.rl_devicelist is not None else []

    def groups(self):
        if self.group_repo is not None:
            return self.group_repo.list()
        return self.rl_grouplist if self.rl_grouplist is not None else []


def register_rl_blueprint(
    rhapi,
    *,
    rl_instance,
    state_repository=None,
    rl_devicelist=None,
    rl_grouplist=None,
    services=None,
    RL_DeviceGroup,
    logger=None,
):
    """Register the RaceLink blueprint with RotorHazard."""

    ctx = _WebContext(
        rhapi=rhapi,
        rl_instance=rl_instance,
        state_repository=state_repository,
        rl_devicelist=rl_devicelist,
        rl_grouplist=rl_grouplist,
        services=services,
        RL_DeviceGroup=RL_DeviceGroup,
        logger=logger,
    )

    if "host_wifi" not in ctx.services:
        ctx.services["host_wifi"] = HostWifiService()
    if "presets" not in ctx.services:
        ctx.services["presets"] = PresetsService()
    if "ota" not in ctx.services:
        ctx.services["ota"] = OTAService(
            host_wifi_service=ctx.services["host_wifi"],
            presets_service=ctx.services["presets"],
        )

    root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    bp = Blueprint(
        "racelink",
        __name__,
        template_folder=os.path.join(root_dir, "pages"),
        static_folder=os.path.join(root_dir, "static"),
        static_url_path="/racelink/static",
    )

    sse = SSEBridge(logger=logger)
    tasks = TaskManager(broadcaster=sse.broadcast, master_state=sse.master, logger=logger)
    sse.attach_task_manager(tasks)
    ctx.sse = sse
    ctx.tasks = tasks

    @bp.route("/racelink")
    def rl_render():
        sse.ensure_transport_hooked(rl_instance)
        return templating.render_template(
            "racelink.html",
            serverInfo=None,
            getOption=rhapi.db.option,
            __=rhapi.__,
        )

    sse.register_routes(bp, tasks, rl_instance)
    api_state = register_api_routes(bp, ctx)

    api_state["ensure_presets_loaded"]()
    rhapi.ui.blueprint_add(bp)
    ctx.log("RaceLink UI blueprint registered at /racelink")

    return bp
