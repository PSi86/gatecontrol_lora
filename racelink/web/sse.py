"""SSE client management and transport-event mirroring for RaceLink."""

from __future__ import annotations

import json
import threading
import time

from flask import Response, stream_with_context

try:
    from gevent.lock import Semaphore as _RLLock  # type: ignore

    _DefaultLock = _RLLock
except Exception:  # pragma: no cover
    try:
        from gevent.lock import RLock as _RLLock  # type: ignore

        _DefaultLock = _RLLock
    except Exception:  # pragma: no cover
        _DefaultLock = threading.Lock

try:
    from gevent.queue import Queue as _RLQueue  # type: ignore
except Exception:  # pragma: no cover
    try:
        from queue import Queue as _RLQueue  # type: ignore
    except Exception:  # pragma: no cover
        _RLQueue = None

try:
    from ..transport import EV_ERROR, EV_RX_WINDOW_CLOSED, EV_RX_WINDOW_OPEN, EV_TX_DONE
except Exception:  # pragma: no cover
    try:
        from racelink.transport import EV_ERROR, EV_RX_WINDOW_CLOSED, EV_RX_WINDOW_OPEN, EV_TX_DONE  # type: ignore
    except Exception:  # pragma: no cover
        EV_ERROR = 0xF0
        EV_RX_WINDOW_OPEN = 0xF1
        EV_RX_WINDOW_CLOSED = 0xF2
        EV_TX_DONE = 0xF3


class MasterState:
    def __init__(self, broadcaster):
        self._broadcast = broadcaster
        self._state = {
            "state": "IDLE",
            "tx_pending": False,
            "rx_window_open": False,
            "rx_windows": 0,
            "rx_window_ms": 0,
            "last_event": None,
            "last_event_ts": 0.0,
            "last_tx_len": 0,
            "last_rx_count_delta": 0,
            "last_error": None,
        }

    def snapshot(self):
        return dict(self._state)

    def set(self, **updates):
        changed = False
        for key, value in updates.items():
            if self._state.get(key) != value:
                self._state[key] = value
                changed = True
        if changed:
            self._state["last_event_ts"] = time.time()
            self._broadcast("master", self.snapshot())


class SSEBridge:
    def __init__(self, *, logger=None):
        self._logger = logger
        self._clients_lock = _DefaultLock()
        self._clients = set()
        self.master = MasterState(self.broadcast)
        self._task_manager = None
        self._hooked_lora = {"ok": False}

    def attach_task_manager(self, task_manager):
        self._task_manager = task_manager

    def log(self, msg):
        try:
            if self._logger:
                self._logger.info(msg)
            else:
                print(msg)
        except Exception:
            print(msg)

    def broadcast(self, event_name: str, payload):
        with self._clients_lock:
            dead = []
            for q in list(self._clients):
                try:
                    q.put((event_name, payload), timeout=0.01)
                except Exception:
                    dead.append(q)
            for q in dead:
                try:
                    self._clients.remove(q)
                except Exception:
                    pass

    def ensure_transport_hooked(self, rl_instance):
        if self._hooked_lora["ok"]:
            return

        lora = getattr(rl_instance, "lora", None)
        if not lora:
            return

        if hasattr(lora, "add_listener"):
            try:
                lora.add_listener(self.on_transport_event)  # type: ignore[attr-defined]
                self._hooked_lora["ok"] = True
                self.log("RaceLink: transport event listener installed (add_listener)")
                return
            except Exception as ex:
                self.log(f"RaceLink: add_listener failed, falling back to on_event: {ex}")

        if not hasattr(lora, "on_event"):
            return

        prev = getattr(lora, "on_event", None)

        def _mux(ev: dict):
            try:
                self.on_transport_event(ev)
            except Exception:
                pass
            try:
                if prev and prev is not _mux:
                    prev(ev)
            except Exception:
                pass

        try:
            lora.on_event = _mux
            self._hooked_lora["ok"] = True
            self.log("RaceLink: transport event hook installed")
        except Exception as ex:
            self.log(f"RaceLink: transport hook failed: {ex}")

    def _task_is_running(self):
        return bool(self._task_manager and self._task_manager.is_running())

    def _task_snapshot(self):
        if not self._task_manager:
            return None
        return self._task_manager.snapshot()

    def _task_update(self, **updates):
        if self._task_manager:
            self._task_manager.update(**updates)

    def on_transport_event(self, ev: dict):
        event_type = ev.get("type", None)

        if event_type == EV_RX_WINDOW_OPEN:
            rx_state = int(ev.get("rx_windows", 1) or 0)
            rx_open = rx_state == 1
            self.master.set(
                state="RX" if rx_open else ("TX" if self.master.snapshot().get("tx_pending") else "IDLE"),
                rx_windows=rx_state,
                rx_window_open=rx_open,
                rx_window_ms=int(ev.get("window_ms", 0) or 0),
                last_event="RX_WINDOW_OPEN",
                last_error=None,
            )
            if self._task_is_running():
                snap = self._task_snapshot() or {}
                self._task_update(rx_window_events=int(snap.get("rx_window_events", 0)) + 1)
            return

        if event_type == EV_RX_WINDOW_CLOSED:
            delta = int(ev.get("rx_count_delta", 0) or 0)
            rx_state = int(ev.get("rx_windows", 0) or 0)
            rx_open = rx_state == 1
            self.master.set(
                state="RX" if rx_open else ("TX" if self.master.snapshot().get("tx_pending") else "IDLE"),
                rx_windows=rx_state,
                rx_window_open=rx_open,
                rx_window_ms=0,
                last_event="RX_WINDOW_CLOSED",
                last_rx_count_delta=delta,
                last_error=None,
            )
            if self._task_is_running():
                snap = self._task_snapshot() or {}
                self._task_update(
                    rx_count_delta_total=int(snap.get("rx_count_delta_total", 0)) + delta,
                    rx_window_events=int(snap.get("rx_window_events", 0)) + 1,
                )
            return

        if event_type == EV_TX_DONE:
            self.master.set(
                tx_pending=False,
                state="RX" if self.master.snapshot().get("rx_window_open") else "IDLE",
                last_event="TX_DONE",
                last_tx_len=int(ev.get("last_len", 0) or 0),
                last_error=None,
            )
            return

        if event_type == EV_ERROR:
            raw = ev.get("data", b"")
            try:
                if isinstance(raw, (bytes, bytearray)):
                    raw = raw.hex().upper()
            except Exception:
                pass
            self.master.set(state="ERROR", last_event="USB_ERROR", last_error=str(raw))
            if self._task_is_running():
                self._task_update(last_error=str(raw))
            return

        reply = ev.get("reply")
        if not reply:
            return

        with self._clients_lock:
            has_clients = bool(self._clients)
        if reply == "ACK" and has_clients:
            self.broadcast("refresh", {"what": ["devices"]})

        if self._task_is_running():
            snap = self._task_snapshot() or {}
            task_name = snap.get("name")
            if task_name == "discover" and reply == "IDENTIFY_REPLY":
                self._task_update(rx_replies=int(snap.get("rx_replies", 0)) + 1)
            elif task_name == "status" and reply == "STATUS_REPLY":
                self._task_update(rx_replies=int(snap.get("rx_replies", 0)) + 1)

        self.master.set(last_event=reply, last_error=None)

    def register_routes(self, bp, task_manager, rl_instance):
        self.attach_task_manager(task_manager)

        @bp.route("/racelink/api/events")
        def api_events():
            self.ensure_transport_hooked(rl_instance)

            q = _RLQueue()
            with self._clients_lock:
                self._clients.add(q)

            try:
                q.put(("master", self.master.snapshot()), timeout=0.01)
                q.put(("task", task_manager.snapshot()), timeout=0.01)
            except Exception:
                pass

            def _encode(event_name: str, payload) -> str:
                return f"event: {event_name}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"

            @stream_with_context
            def gen():
                last_ping = time.time()
                try:
                    while True:
                        try:
                            item = q.get(timeout=1.0)
                        except Exception:
                            item = None

                        now = time.time()
                        if item is None:
                            if now - last_ping >= 15.0:
                                last_ping = now
                                yield ": ping\n\n"
                            continue

                        event_name, payload = item
                        yield _encode(event_name, payload)
                finally:
                    with self._clients_lock:
                        try:
                            self._clients.remove(q)
                        except Exception:
                            pass

            headers = {
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            }
            return Response(gen(), mimetype="text/event-stream", headers=headers)
