"""Task-state orchestration for the RaceLink web layer."""

from __future__ import annotations

import threading
import time
from typing import Optional

from flask import jsonify


class TaskManager:
    """Keep track of the single long-running task exposed to the UI."""

    def __init__(self, *, broadcaster, master_state, logger=None):
        self._broadcast = broadcaster
        self._master_state = master_state
        self._logger = logger
        self._lock = threading.Lock()
        self._task = None
        self._task_seq = 0

    def snapshot(self):
        with self._lock:
            return dict(self._task) if self._task else None

    def update(self, **updates):
        with self._lock:
            if not self._task:
                return
            for key, value in updates.items():
                self._task[key] = value
        self._broadcast("task", self.snapshot())

    def is_running(self) -> bool:
        with self._lock:
            return bool(self._task and self._task.get("state") == "running")

    def busy_response(self):
        return jsonify({"ok": False, "busy": True, "task": self.snapshot()}), 409

    def start(self, name: str, target_fn, meta: Optional[dict] = None):
        with self._lock:
            if self._task and self._task.get("state") == "running":
                return None
            self._task_seq += 1
            self._task = {
                "id": self._task_seq,
                "name": name,
                "state": "running",
                "started_ts": time.time(),
                "ended_ts": None,
                "meta": meta or {},
                "rx_replies": 0,
                "rx_window_events": 0,
                "rx_count_delta_total": 0,
                "last_error": None,
                "result": None,
            }

        self._broadcast("task", self.snapshot())

        def runner():
            try:
                self._master_state.set(
                    state="TX",
                    tx_pending=True,
                    last_event=f"TASK_{name.upper()}_START",
                )
                result = target_fn()
                self.update(state="done", ended_ts=time.time(), result=result)
                self._master_state.set(
                    state="IDLE" if not self._master_state.snapshot().get("rx_window_open") else "RX",
                    last_event=f"TASK_{name.upper()}_DONE",
                )
                self._broadcast("refresh", {"what": ["groups", "devices"]})
            except Exception as ex:
                self.update(state="error", ended_ts=time.time(), last_error=str(ex))
                self._master_state.set(
                    state="ERROR",
                    last_event=f"TASK_{name.upper()}_ERROR",
                    last_error=str(ex),
                )
                if self._logger:
                    try:
                        self._logger.exception("RaceLink task %s failed", name)
                    except Exception:
                        pass

        thread = threading.Thread(target=runner, daemon=True)
        thread.start()
        return self.snapshot()
