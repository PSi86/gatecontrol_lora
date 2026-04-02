from __future__ import annotations

import threading
import time
from collections.abc import Callable

from ..core.events import HostRaceEvent, HostRaceEventType
from ..core.ports.host_race_events import HostRaceEventSink
from ..core.ports.race_provider import RaceProviderPort


class MockRaceProvider(RaceProviderPort):
    """Reference data provider for integrating non-RotorHazard software."""

    def __init__(self):
        self._current_heat: int | None = 1
        self._pilot_assignments: list[tuple[int, str]] = [
            (0, "ALPHA"),
            (1, "BRAVO"),
            (2, "CHARLIE"),
            (3, "DELTA"),
        ]
        self._channels: list[str] = ["R1", "R2", "R3", "R4", "--", "--", "--", "--"]

    def get_current_heat(self) -> int | None:
        return self._current_heat

    def get_pilot_assignments(self) -> list[tuple[int, str]]:
        return list(self._pilot_assignments)

    def get_frequency_channels(self) -> list[str]:
        return list(self._channels)


class MockPollingRaceEventAdapter:
    """Minimal polling adapter that emits internal race events from state transitions."""

    def __init__(self, state_supplier: Callable[[], dict], interval_s: float = 0.5):
        self._state_supplier = state_supplier
        self._interval_s = max(0.05, float(interval_s))
        self._sink: HostRaceEventSink | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._last_state: str | None = None

    def start(self, event_sink: HostRaceEventSink) -> None:
        self._sink = event_sink
        self._stop_event.clear()
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run_loop, name="mock-race-poll", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        self._thread = None
        if thread and thread.is_alive():
            thread.join(timeout=self._interval_s * 2)

    def poll_once(self) -> None:
        snapshot = self._state_supplier() or {}
        state = str(snapshot.get("race_state", "stopped")).strip().lower()

        if self._last_state != state:
            self._emit_transition(self._last_state, state, snapshot)
        self._last_state = state

        self._emit(
            HostRaceEventType.RACE_SNAPSHOT,
            {"race_state": state, "heat_id": snapshot.get("heat_id")},
        )

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            self.poll_once()
            time.sleep(self._interval_s)

    def _emit_transition(self, prev_state: str | None, state: str, snapshot: dict) -> None:
        payload = {"previous_state": prev_state, "race_state": state, "heat_id": snapshot.get("heat_id")}
        if state == "running":
            self._emit(HostRaceEventType.RACE_STARTED, payload)
        elif state == "finished":
            self._emit(HostRaceEventType.RACE_FINISHED, payload)
        elif state in {"stopped", "idle", "ready"}:
            self._emit(HostRaceEventType.RACE_STOPPED, payload)

    def _emit(self, event_type: HostRaceEventType, payload: dict) -> None:
        if self._sink is None:
            return
        self._sink(HostRaceEvent(type=event_type, payload=payload))
