from __future__ import annotations

from collections.abc import Mapping

from eventmanager import Evt

from ...core import events as core_events


def _evt(name: str):
    return getattr(Evt, name, None)


# Central mapping table between RotorHazard runtime events and internal domain events.
rh_event_map: dict[str, str] = {
    rh_evt: internal_evt
    for rh_evt, internal_evt in {
        _evt("RACE_START"): core_events.RACE_STARTED,
        _evt("RACE_STOP"): core_events.RACE_STOPPED,
        _evt("RACE_FINISH"): core_events.RACE_FINISHED,
        _evt("RACE_LAP_RECORDED"): core_events.LAP_RECORDED,
        _evt("DATA_IMPORT_INITIALIZE"): core_events.DATA_IMPORT_INITIALIZE,
        _evt("DATA_EXPORT_INITIALIZE"): core_events.DATA_EXPORT_INITIALIZE,
        _evt("ACTIONS_INITIALIZE"): core_events.ACTIONS_INITIALIZE,
        _evt("STARTUP"): core_events.STARTUP,
    }.items()
    if rh_evt
}


class RHEventBridge:
    """Bridges RH `Evt.*` events into internal bus events."""

    def __init__(self, rhapi, event_bus, mapping: Mapping[str, str] | None = None):
        self._rhapi = rhapi
        self._event_bus = event_bus
        self._mapping = dict(mapping or rh_event_map)

    def install(self) -> None:
        for rh_event_name, internal_event in self._mapping.items():
            self._rhapi.events.on(rh_event_name, self._build_handler(internal_event))

    def _build_handler(self, internal_event: str):
        def _forward(payload):
            self._event_bus.publish(internal_event, payload)

        return _forward
