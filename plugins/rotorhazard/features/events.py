from __future__ import annotations

from eventmanager import Evt


def activate(plugin) -> None:
    """Bind RotorHazard event hooks to the controller/UI callbacks."""
    controller = plugin.controller
    rhapi = plugin.rhapi

    rhapi.events.on(Evt.STARTUP, controller.on_startup)
