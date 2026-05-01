"""Blueprint assembly for the RaceLink web layer."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any, Callable

from flask import Blueprint, templating

from ..services import HostWifiService, OTAService, PresetsService
from .api import register_api_routes
from .sse import _DefaultLock, SSEBridge
from .tasks import TaskManager

DEFAULT_URL_PREFIX = "/racelink"


def _noop_option(_key, default=None):
    return default


def _identity(text):
    return text


def _normalize_url_prefix(url_prefix: str | None) -> str:
    raw = str(url_prefix or "").strip()
    if not raw or raw == "/":
        return ""
    return "/" + raw.strip("/")


def _join_url(base: str, suffix: str) -> str:
    base_norm = _normalize_url_prefix(base)
    suffix_norm = "/" + str(suffix or "").lstrip("/")
    return f"{base_norm}{suffix_norm}" if base_norm else suffix_norm


def _resolve_asset_dirs() -> tuple[str, str]:
    package_root = Path(__file__).resolve().parents[1]
    package_pages = package_root / "pages"
    package_static = package_root / "static"
    if not package_pages.is_dir() or not package_static.is_dir():
        raise FileNotFoundError("RaceLink web assets are missing from the installed package")
    return str(package_pages), str(package_static)


def _resolve_blueprint_registrar(app_or_host):
    registrar = getattr(app_or_host, "register_blueprint", None)
    if callable(registrar):
        return registrar

    ui = getattr(app_or_host, "ui", None)
    registrar = getattr(ui, "blueprint_add", None) if ui is not None else None
    if callable(registrar):
        return registrar

    raise TypeError("host does not provide a blueprint registration entrypoint")


@dataclass(slots=True)
class RaceLinkWebRuntime:
    rl_instance: Any
    state_repository: Any = None
    services: dict | None = None
    RL_DeviceGroup: Any = None
    logger: Any = None
    option_getter: Callable[[str, Any], Any] | None = None
    translator: Callable[[str], str] | None = None
    blueprint_registrar: Callable[[Blueprint], Any] | None = None
    rl_devicelist: Any = None
    rl_grouplist: Any = None

    def option(self, key, default=None):
        getter = self.option_getter or _noop_option
        return getter(key, default)

    def translate(self, text):
        translator = self.translator or _identity
        return translator(text)


class _WebContext:
    def __init__(
        self,
        *,
        runtime: RaceLinkWebRuntime,
        rl_instance,
        state_repository,
        rl_devicelist,
        rl_grouplist,
        services,
        RL_DeviceGroup,
        logger,
    ):
        self.runtime = runtime
        self.rl_instance = rl_instance
        self.state_repository = state_repository
        self.rl_devicelist = rl_devicelist
        self.rl_grouplist = rl_grouplist
        self.services = services or {}
        self.RL_DeviceGroup = RL_DeviceGroup
        self.logger = logger
        # Reuse the state-repository lock when available so HTTP routes and
        # the transport thread serialize against the same mutex (plan P1-4).
        state_lock = getattr(state_repository, "lock", None) if state_repository else None
        self.rl_lock = state_lock if state_lock is not None else _DefaultLock()
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
            # swallow-ok: best-effort fallback; caller proceeds with safe default
            print(msg)

    def devices(self):
        if self.device_repo is not None:
            return self.device_repo.list()
        return self.rl_devicelist if self.rl_devicelist is not None else []

    def groups(self):
        if self.group_repo is not None:
            return self.group_repo.list()
        return self.rl_grouplist if self.rl_grouplist is not None else []


def create_racelink_web_blueprint(
    runtime: RaceLinkWebRuntime,
    *,
    url_prefix: str = DEFAULT_URL_PREFIX,
    blueprint_name: str = "racelink",
):
    """Build the shared RaceLink UI blueprint for any Flask host."""

    normalized_prefix = _normalize_url_prefix(url_prefix)

    ctx = _WebContext(
        runtime=runtime,
        rl_instance=runtime.rl_instance,
        state_repository=runtime.state_repository,
        rl_devicelist=runtime.rl_devicelist,
        rl_grouplist=runtime.rl_grouplist,
        services=runtime.services,
        RL_DeviceGroup=runtime.RL_DeviceGroup,
        logger=runtime.logger,
    )

    if "host_wifi" not in ctx.services:
        ctx.services["host_wifi"] = HostWifiService()
    if "presets" not in ctx.services:
        ctx.services["presets"] = PresetsService()
    if "rl_presets" not in ctx.services:
        from ..services import RLPresetsService
        ctx.services["rl_presets"] = RLPresetsService()
    # Mirror onto the controller so send_rl_preset_by_id can resolve ids.
    if not hasattr(ctx.rl_instance, "rl_presets_service"):
        try:
            setattr(ctx.rl_instance, "rl_presets_service", ctx.services["rl_presets"])
        except Exception:
            # swallow-ok: best-effort fallback; caller proceeds with safe default
            pass
    if "scenes" not in ctx.services:
        from ..services import SceneService
        ctx.services["scenes"] = SceneService()
    if not hasattr(ctx.rl_instance, "scenes_service"):
        try:
            setattr(ctx.rl_instance, "scenes_service", ctx.services["scenes"])
        except Exception:
            # swallow-ok: best-effort attach; mirrors the rl_presets_service
            # fallback above
            pass
    if "scene_runner" not in ctx.services:
        from ..services import SceneRunnerService
        ctx.services["scene_runner"] = SceneRunnerService(
            controller=ctx.rl_instance,
            scenes_service=ctx.services["scenes"],
            control_service=getattr(ctx.rl_instance, "control_service", None),
            sync_service=getattr(ctx.rl_instance, "sync_service", None),
            rl_presets_service=ctx.services.get("rl_presets"),
        )
    if not hasattr(ctx.rl_instance, "scene_runner_service"):
        try:
            setattr(ctx.rl_instance, "scene_runner_service", ctx.services["scene_runner"])
        except Exception:
            # swallow-ok: best-effort attach; runner stays reachable via
            # ctx.services["scene_runner"] regardless
            pass
    if "ota" not in ctx.services:
        ctx.services["ota"] = OTAService(
            host_wifi_service=ctx.services["host_wifi"],
            presets_service=ctx.services["presets"],
        )

    template_dir, static_dir = _resolve_asset_dirs()
    bp = Blueprint(
        blueprint_name,
        __name__,
        url_prefix=normalized_prefix or None,
        template_folder=template_dir,
        static_folder=static_dir,
        static_url_path="/static",
    )

    sse = SSEBridge(logger=runtime.logger)
    tasks = TaskManager(broadcaster=sse.broadcast, master_state=sse.master, logger=runtime.logger)
    sse.attach_task_manager(tasks)
    attach_task_manager = getattr(runtime.rl_instance, "attach_task_manager", None)
    if callable(attach_task_manager):
        attach_task_manager(tasks)

    # Plan P1-1: push gateway-readiness changes over SSE so the UI can keep a
    # persistent banner in sync without polling /api/gateway.
    try:
        setattr(
            runtime.rl_instance,
            "on_gateway_status_changed",
            lambda status: sse.broadcast("gateway", status),
        )
    except Exception:
        # swallow-ok: not every host exposes the attribute; degrade to polling
        pass

    ctx.sse = sse
    ctx.tasks = tasks

    @bp.route("/")
    def rl_render():
        sse.ensure_transport_hooked(runtime.rl_instance)
        return templating.render_template(
            "racelink.html",
            serverInfo=None,
            getOption=runtime.option,
            __=runtime.translate,
            rl_base_path=normalized_prefix or "",
            rl_static_path=_join_url(normalized_prefix, "/static"),
        )

    @bp.route("/scenes")
    def rl_render_scenes():
        # R5a: Scene Manager moved out of dlgScenes into its own page.
        # Same render-template helpers as the Devices page so SSE wiring and
        # base-path math match exactly.
        sse.ensure_transport_hooked(runtime.rl_instance)
        return templating.render_template(
            "scenes.html",
            serverInfo=None,
            getOption=runtime.option,
            __=runtime.translate,
            rl_base_path=normalized_prefix or "",
            rl_static_path=_join_url(normalized_prefix, "/static"),
        )

    sse.register_routes(bp, tasks, runtime.rl_instance)
    api_state = register_api_routes(bp, ctx)

    api_state["ensure_presets_loaded"]()
    return bp


def register_racelink_web(
    app_or_host,
    runtime: RaceLinkWebRuntime,
    *,
    url_prefix: str = DEFAULT_URL_PREFIX,
    blueprint_name: str = "racelink",
):
    """Register the shared RaceLink web UI into a Flask host."""

    bp = create_racelink_web_blueprint(
        runtime,
        url_prefix=url_prefix,
        blueprint_name=blueprint_name,
    )
    registrar = runtime.blueprint_registrar or _resolve_blueprint_registrar(app_or_host)
    registrar(bp)
    display_prefix = _normalize_url_prefix(url_prefix) or "/"
    logger = getattr(runtime, "logger", None)
    if logger:
        logger.info("RaceLink UI blueprint registered at %s", display_prefix)

    return bp


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
    url_prefix: str = DEFAULT_URL_PREFIX,
):
    """Compatibility wrapper for legacy RotorHazard-style bootstrap code."""

    runtime = RaceLinkWebRuntime(
        rl_instance=rl_instance,
        state_repository=state_repository,
        rl_devicelist=rl_devicelist,
        rl_grouplist=rl_grouplist,
        services=services,
        RL_DeviceGroup=RL_DeviceGroup,
        logger=logger,
        option_getter=rhapi.db.option,
        translator=rhapi.__,
        blueprint_registrar=rhapi.ui.blueprint_add,
    )
    return register_racelink_web(rhapi, runtime, url_prefix=url_prefix)
