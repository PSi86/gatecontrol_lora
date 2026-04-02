from __future__ import annotations

from eventmanager import Evt


def activate(plugin) -> None:
    """Register Quickset / Settings / Actions UI integrations."""
    controller = plugin.controller
    rhapi = plugin.rhapi

    rhapi.events.on(Evt.ACTIONS_INITIALIZE, controller.host_ui.register_actions)
