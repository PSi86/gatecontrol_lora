"""SSE client management and transport-event mirroring for RaceLink."""

from __future__ import annotations

import json
import logging
import threading
import time

from flask import Response, stream_with_context

logger = logging.getLogger(__name__)

try:
    from gevent.lock import Semaphore as _RLLock  # type: ignore

    _DefaultLock = _RLLock
except Exception:  # pragma: no cover
    # swallow-ok: best-effort fallback; caller proceeds with safe default
    try:
        from gevent.lock import RLock as _RLLock  # type: ignore

        _DefaultLock = _RLLock
    except Exception:  # pragma: no cover
        # swallow-ok: gevent absent -> use threading.Lock
        _DefaultLock = threading.Lock

try:
    from gevent.queue import Queue as _RLQueue  # type: ignore
except Exception:  # pragma: no cover
    # swallow-ok: best-effort fallback; caller proceeds with safe default
    try:
        from queue import Queue as _RLQueue  # type: ignore
    except Exception:  # pragma: no cover
        # swallow-ok: no queue impl available -> callers handle None
        _RLQueue = None

from ..transport import (
    EV_ERROR,
    EV_STATE_CHANGED,
    EV_STATE_REPORT,
    EV_TX_DONE,
    EV_TX_REJECTED,
    GATEWAY_STATE_NAME,
    GATEWAY_STATE_RX_WINDOW,
    GATEWAY_STATE_UNKNOWN,
)


class MasterState:
    """Thin 1:1 mirror of the gateway's reported state (Batch B, 2026-04-28).

    Pre-Batch-B this class kept a derived host-side state machine: ``state``
    was inferred by combining EV_RX_WINDOW_OPEN/CLOSED, EV_TX_DONE, and
    explicit ``master.set(state="TX", tx_pending=True)`` calls from the web
    layer when a request was about to write. That worked but couldn't observe
    silent gateway drops or distinguish "in TX" from "TX rejected".

    The v4 redesign moves the source of truth into the gateway: every state
    transition is reported via EV_STATE_CHANGED with a state-byte body. The
    host stores the last byte verbatim and renders it. Outcome events
    (EV_TX_DONE, EV_TX_REJECTED) are orthogonal — they're surfaced via
    ``last_event`` for diagnostic display but don't drive ``state``.
    """

    def __init__(self, broadcaster):
        self._broadcast = broadcaster
        self._state = {
            "state": "UNKNOWN",
            "state_byte": GATEWAY_STATE_UNKNOWN,
            "state_metadata_ms": 0,
            "last_event": None,
            "last_event_ts": 0.0,
            "last_error": None,
        }

    def snapshot(self):
        return dict(self._state)

    def set(self, **updates):
        """Update one or more fields and broadcast on change.

        Per Batch B, the only fields that should be set externally are
        ``last_event`` (diagnostic) and ``last_error`` (notification).
        Writes to ``state`` / ``state_byte`` / ``state_metadata_ms`` from
        outside :meth:`apply_gateway_state` are tolerated for back-compat
        but are immediately overwritten by the next gateway event — so
        they're effectively no-ops in steady state. Helpful for tests
        that seed a starting state directly.
        """
        changed = False
        for key, value in updates.items():
            if self._state.get(key) != value:
                self._state[key] = value
                changed = True
        if changed:
            self._state["last_event_ts"] = time.time()
            self._broadcast("master", self.snapshot())

    def apply_gateway_state(self, state_byte: int, metadata_ms: int = 0,
                            *, source_event: str | None = None) -> None:
        """Replace the mirrored state from a STATE_CHANGED / STATE_REPORT event.

        Single source of pill truth post-Batch-B. ``source_event`` is one of
        ``"STATE_CHANGED"`` / ``"STATE_REPORT"`` / ``"INITIAL"`` and feeds
        ``last_event`` for the operator-facing detail line.
        """
        sb = int(state_byte) & 0xFF
        meta = int(metadata_ms) & 0xFFFF
        updates = {
            "state": GATEWAY_STATE_NAME.get(sb, "UNKNOWN"),
            "state_byte": sb,
            "state_metadata_ms": meta if sb == GATEWAY_STATE_RX_WINDOW else 0,
            "last_error": None,
        }
        if source_event:
            updates["last_event"] = source_event
        self.set(**updates)


class SSEBridge:
    def __init__(self, *, logger=None):
        self._logger = logger
        self._clients_lock = _DefaultLock()
        self._clients = set()
        self.master = MasterState(self.broadcast)
        self._task_manager = None
        self._hooked_transport = {"ok": False}

    def attach_task_manager(self, task_manager):
        self._task_manager = task_manager

    def log(self, msg):
        try:
            if self._logger:
                self._logger.info(msg)
            else:
                print(msg)
        except Exception:
            # swallow-ok: logger implementations vary - fall back to print
            print(msg)

    def broadcast(self, event_name: str, payload):
        # A4: never hold ``_clients_lock`` across a queue ``put`` — a
        # disconnected-but-not-yet-cleaned-up client used to stall every
        # other broadcaster + every new SSE registration for up to
        # ``timeout=0.01`` seconds *per dead client* (was: ``q.put(...,
        # timeout=0.01)`` inside the lock). With many dead clients that
        # compounds into hundreds of milliseconds of UI starvation.
        #
        # Fix: snapshot the client set under the lock, fan out outside
        # the lock with ``put_nowait`` (truly non-blocking), collect
        # dead clients, then re-acquire briefly to remove them via
        # idempotent ``discard``.
        with self._clients_lock:
            clients_snapshot = list(self._clients)

        if not clients_snapshot:
            return

        dead = []
        for q in clients_snapshot:
            try:
                q.put_nowait((event_name, payload))
            except Exception as ex:
                logger.debug(
                    "SSE queue put failed for %r, dropping client: %s",
                    event_name, ex,
                )
                dead.append(q)

        if not dead:
            return

        with self._clients_lock:
            for q in dead:
                self._clients.discard(q)

    def ensure_transport_hooked(self, rl_instance):
        if self._hooked_transport["ok"]:
            return

        transport = getattr(rl_instance, "transport", None)
        if not transport:
            return

        if hasattr(transport, "add_listener"):
            try:
                transport.add_listener(self.on_transport_event)  # type: ignore[attr-defined]
                self._hooked_transport["ok"] = True
                self.log("RaceLink: transport event listener installed (add_listener)")
                return
            except Exception as ex:
                # swallow-ok: fall through to the on_event hook below.
                # Include the exception type in the log line so a
                # transport API mismatch (AttributeError) is
                # distinguishable from a transport-state error.
                self.log(
                    f"RaceLink: add_listener failed, falling back to "
                    f"on_event: {type(ex).__name__}: {ex}"
                )
                logger.debug("add_listener failed", exc_info=True)

        if not hasattr(transport, "on_event"):
            return

        prev = getattr(transport, "on_event", None)

        def _mux(ev: dict):
            try:
                self.on_transport_event(ev)
            except Exception:
                logger.exception("RaceLink: SSE transport handler raised")
            try:
                if prev and prev is not _mux:
                    prev(ev)
            except Exception:
                logger.exception("RaceLink: previous on_event handler raised")

        try:
            transport.on_event = _mux
            self._hooked_transport["ok"] = True
            self.log("RaceLink: transport event hook installed")
        except Exception as ex:
            # swallow-ok: SSE will operate without the transport hook
            # — events from the gateway just won't fan out to clients.
            # Include exception type so a missing-attribute regression
            # is visible in the log.
            self.log(
                f"RaceLink: transport hook failed: "
                f"{type(ex).__name__}: {ex}"
            )
            logger.warning("transport hook failed", exc_info=True)

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

        if event_type in (EV_STATE_CHANGED, EV_STATE_REPORT):
            state_byte = int(ev.get("state_byte", GATEWAY_STATE_UNKNOWN))
            metadata_ms = int(ev.get("state_metadata_ms", 0) or 0)
            source = "STATE_REPORT" if event_type == EV_STATE_REPORT else "STATE_CHANGED"
            self.master.apply_gateway_state(state_byte, metadata_ms, source_event=source)
            if self._task_is_running() and state_byte == GATEWAY_STATE_RX_WINDOW:
                snap = self._task_snapshot() or {}
                self._task_update(rx_window_events=int(snap.get("rx_window_events", 0)) + 1)
            return

        if event_type == EV_TX_DONE:
            # Outcome event for the host's _send_m2n. The matching state
            # transition (TX -> IDLE under setDefaultRxContinuous, or TX ->
            # RX_WINDOW for streams) arrives as the next EV_STATE_CHANGED.
            # We surface only the diagnostic ``last_event`` so the detail
            # line shows "last: TX_DONE" between sends.
            self.master.set(last_event="TX_DONE", last_error=None)
            return

        if event_type == EV_TX_REJECTED:
            reason = ev.get("reason_name") or ev.get("reason") or "unknown"
            self.master.set(
                last_event=f"TX_REJECTED ({reason})",
                last_error=None,
            )
            return

        if event_type == EV_ERROR:
            raw = ev.get("data", b"")
            try:
                if isinstance(raw, (bytes, bytearray)):
                    raw = raw.decode("utf-8", errors="replace") or raw.hex().upper()
            except Exception:
                logger.debug("SSE: unable to stringify EV_ERROR payload", exc_info=True)
            # ERROR is a real state transition the gateway reports via
            # EV_STATE_CHANGED(ERROR); EV_ERROR carries the human-readable
            # reason. Set last_error here; the state byte updates whenever
            # the matching STATE_CHANGED arrives.
            self.master.set(last_event="USB_ERROR", last_error=str(raw))
            if self._task_is_running():
                self._task_update(last_error=str(raw))
            return

        reply = ev.get("reply")
        if not reply:
            return

        with self._clients_lock:
            has_clients = bool(self._clients)
        # Live device-list updates: every reply that mutates a device
        # row's content (ACK applies a config change, STATUS_REPLY
        # refreshes RSSI/SNR/voltage/last_seen_ts, IDENTIFY_REPLY can
        # add a new row or update caps/groupId) triggers a refresh
        # so the WebUI rebuilds the table immediately. Pre-fix only
        # ACKs broadcast — STATUS_REPLY and IDENTIFY_REPLY waited for
        # the task to fully complete before the UI saw the new data.
        if has_clients and reply in ("ACK", "STATUS_REPLY", "IDENTIFY_REPLY"):
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

        @bp.route("/api/events")
        def api_events():
            self.ensure_transport_hooked(rl_instance)

            q = _RLQueue()
            with self._clients_lock:
                self._clients.add(q)

            try:
                q.put(("master", self.master.snapshot()), timeout=0.01)
                q.put(("task", task_manager.snapshot()), timeout=0.01)
            except Exception:
                logger.debug("SSE: unable to seed initial client snapshots", exc_info=True)

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
                            # swallow-ok: queue-get timeout/empty -> idle tick, send ping
                            item = None

                        now = time.time()
                        if item is None:
                            # 2-second ping cadence (was 15 s) so the only
                            # ``yield`` in a quiet stream fires often enough
                            # that ``BrokenPipeError`` is observed quickly
                            # after a client disconnect. Without this, dead
                            # SSE generators linger up to 15 s, occupy
                            # Chrome's per-origin HTTP/1.1 slot pool (limit 6)
                            # and stall navigations between /racelink/ and
                            # /racelink/scenes after ~6 quick page switches.
                            if now - last_ping >= 2.0:
                                last_ping = now
                                yield ": ping\n\n"
                            continue

                        event_name, payload = item
                        yield _encode(event_name, payload)
                finally:
                    # ``discard`` is idempotent — broadcast()'s dead-
                    # client cleanup may have already removed ``q`` and
                    # the previous ``remove`` + ``except KeyError`` was
                    # just an awkward way of expressing the same thing.
                    with self._clients_lock:
                        self._clients.discard(q)

            headers = {
                "Cache-Control": "no-cache",
                # ``close`` (was ``keep-alive``) so Chrome doesn't retain the
                # SSE socket in its per-origin HTTP/1.1 keep-alive pool after
                # the EventSource ends — text/event-stream never terminates
                # cleanly, and Chrome was holding "half-finished" sockets
                # against its 6-slot limit, which stalled navigations between
                # /racelink/ and /racelink/scenes after ~6 quick switches.
                "Connection": "close",
                "X-Accel-Buffering": "no",
            }
            return Response(gen(), mimetype="text/event-stream", headers=headers)
