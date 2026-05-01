"""RaceLink application container and stable host-owned runtime entrypoints."""

from __future__ import annotations

from .core import NullSink, NullSource
from .services import (
    HostWifiService,
    OTAService,
    PresetsService,
    RLPresetsService,
    SceneRunnerService,
    SceneService,
)
from .state import get_runtime_state_repository

__all__ = [
    "RaceLinkApp",
    "create_runtime",
]


class RaceLinkApp:
    """Container for the currently wired RaceLink runtime dependencies."""

    def __init__(
        self,
        *,
        controller,
        transport=None,
        state_repository=None,
        services=None,
        integrations=None,
        event_source=None,
        data_sink=None,
    ):
        self.controller = controller
        self.transport = transport
        self.state_repository = state_repository
        self.services = services or {}
        self.integrations = integrations or {}
        self.event_source = event_source or NullSource()
        self.data_sink = data_sink or NullSink()

    @property
    def rl_instance(self):
        """Compatibility alias for the existing controller-centric runtime."""
        return self.controller

    @property
    def device_repository(self):
        return self.state_repository.devices if self.state_repository else None

    @property
    def group_repository(self):
        return self.state_repository.groups if self.state_repository else None


def create_runtime(
    host_api,
    *,
    name: str = "RaceLink_Host",
    label: str = "RaceLink",
    state_repository=None,
    controller=None,
    controller_class=None,
    event_source=None,
    data_sink=None,
    integrations=None,
    presets_apply_options=None,
    extra_services=None,
):
    """Build the standard RaceLink host runtime for any outer integration.

    This is the stable host-side factory that external integrations, including
    the future RotorHazard plugin repository, are allowed to import.
    """

    runtime_state = state_repository or get_runtime_state_repository()

    if controller is None:
        runtime_controller_class = controller_class
        if runtime_controller_class is None:
            from controller import RaceLink_Host as runtime_controller_class

        controller = runtime_controller_class(
            host_api,
            name,
            label,
            state_repository=runtime_state,
        )

    presets_service = PresetsService(
        option_getter=host_api.db.option,
        option_setter=host_api.db.option_set,
        apply_options=presets_apply_options,
    )
    rl_presets_service = RLPresetsService()
    # Expose the RL-preset store on the controller so the control service can
    # resolve preset ids in ``send_rl_preset_by_id`` without extra wiring.
    controller.rl_presets_service = rl_presets_service
    scenes_service = SceneService()
    controller.scenes_service = scenes_service
    scene_runner_service = SceneRunnerService(
        controller=controller,
        scenes_service=scenes_service,
        control_service=controller.control_service,
        sync_service=controller.sync_service,
        rl_presets_service=rl_presets_service,
    )
    controller.scene_runner_service = scene_runner_service
    host_wifi_service = HostWifiService()
    ota_service = OTAService(host_wifi_service=host_wifi_service, presets_service=presets_service)

    services = {
        "config": controller.config_service,
        "control": controller.control_service,
        "gateway": controller.gateway_service,
        "discovery": controller.discovery_service,
        "host_wifi": host_wifi_service,
        "ota": ota_service,
        "presets": presets_service,
        "rl_presets": rl_presets_service,
        "scenes": scenes_service,
        "scene_runner": scene_runner_service,
        "startblock": controller.startblock_service,
        "status": controller.status_service,
        "stream": controller.stream_service,
        "sync": controller.sync_service,
    }
    if extra_services:
        services.update(extra_services)

    return RaceLinkApp(
        controller=controller,
        transport=getattr(controller, "transport", None),
        state_repository=runtime_state,
        services=services,
        integrations=dict(integrations or {}),
        event_source=event_source or NullSource(),
        data_sink=data_sink or NullSink(),
    )
