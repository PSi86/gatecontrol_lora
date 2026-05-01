"""Gateway orchestration service: transport events, reply matching, lifecycle.

The single largest service in the host. Owns:

* The **pending-request registry** (:class:`PendingRequestRegistry` from
  ``pending_requests``) used for unicast ``send_and_wait_for_reply``
  request/response matching.
* The **TX listener path** (``on_transport_tx``) that stamps a pending
  expectation when a unicast request goes out, and the matching
  **RX listener path** (``on_transport_event`` →
  ``pending_try_match`` / ``pending_window_closed``) that clears the
  expectation on a reply or window-closed.
* **Reconnect** (``schedule_reconnect``) and **auto-restore**
  (``_spawn_auto_reassign_worker`` via a bounded
  ``ThreadPoolExecutor``) when the gateway disconnects or a node
  comes back with the wrong groupId.
* **High-level dispatch helpers**: ``send_config``, ``send_sync``,
  ``send_stream`` — orchestrate the transport's primitive
  ``send_*`` ops with retries and ACK-collection.

Threading: this module is the host's primary cross-thread surface.
The transport's RX reader thread fans out to ``on_transport_event``;
web request threads + the scene runner call the dispatch helpers
synchronously. Audit findings A1–A6 + B5 (see the active project-
wide audit plan in ``.claude/plans/``) added the TX-serialization
lock, the ``_pending_config_lock`` and ``_pending_expect_lock``,
and the SSE broadcast lock-discipline fix that this service
depends on.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Optional

from ..domain import create_device, get_dev_type_info
from ..protocol import opcode_name as protocol_opcode_name
from ..protocol import request_direction, response_opcode, response_policy, rules as protocol_rules
from ..transport.framing import mac_last3_from_hex
from ..transport.gateway_events import (
    EV_ERROR,
    EV_STATE_CHANGED,
    EV_STATE_REPORT,
    EV_TX_DONE,
    EV_TX_REJECTED,
    GATEWAY_STATE_IDLE,
    GATEWAY_STATE_NAME,
    GATEWAY_STATE_RX,
    GATEWAY_STATE_RX_WINDOW,
    GATEWAY_STATE_UNKNOWN,
    LP,
)
from .pending_requests import (
    RESP_ACK as PR_RESP_ACK,
    RESP_SPECIFIC as PR_RESP_SPECIFIC,
    PendingRequestRegistry,
)
from . import rf_timing

logger = logging.getLogger(__name__)


class _NullLock:
    """Fallback context manager used when no state lock is available."""

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class GatewayService:
    def __init__(self, controller):
        self.controller = controller
        self._auto_reassign_cooldown_s = 2.0
        self._auto_reassign_recent: dict[str, float] = {}
        self._auto_reassign_lock = threading.Lock()
        # A7: bounded executor for auto-restore workers. The previous
        # implementation kept a ``list[Thread]`` and pruned dead
        # entries on every spawn — which never pruned if no further
        # spawns happened, leaving up to N+1 dead Thread objects in
        # memory until process exit. ``ThreadPoolExecutor`` caps
        # concurrent work at ``max_workers`` (8 is plenty for typical
        # fleets) and reuses threads across submissions; an idle pool
        # holds 0 active threads.
        #
        # ``_auto_restore_futures`` keeps the in-flight futures so the
        # test hook ``_join_auto_restore_workers`` can wait on them
        # deterministically (mirrors the previous behaviour).
        self._auto_restore_executor = ThreadPoolExecutor(
            max_workers=8,
            thread_name_prefix="rl-auto-restore",
        )
        self._auto_restore_futures: list[Future] = []
        # Transport redesign (plan Phase B): the Host owns request/reply
        # matching now that the Gateway stays in Continuous RX. The registry
        # unblocks unicast waiters as soon as the expected frame arrives; any
        # unmatched frame continues through the existing unsolicited pipeline
        # in ``on_transport_event``.
        self._pending_registry = PendingRequestRegistry()
        # Reserved for future use; disabled by default because the observed
        # "bulk set group times out on the second device" problem turned out
        # to be a host-side deadlock (web thread held ``ctx.rl_lock`` across
        # the blocking wait, starving the reader thread in
        # ``handle_ack_event``). See ``_apply_device_meta_updates`` in
        # ``racelink/web/api.py`` for the lock-scope fix. Keep this knob at
        # 0.0 unless a separate diagnostic specifically warrants it.
        self.post_match_settle_s: float = 0.0

    @property
    def transport(self):
        return getattr(self.controller, "transport", None)

    def _state_lock(self):
        """Return the state-repository mutation lock, or a no-op fallback.

        Callers use this as a context manager to serialize device/group
        mutations that race with web-thread reads (plan P1-4).
        """
        repo = getattr(self.controller, "state_repository", None)
        lock = getattr(repo, "lock", None) if repo is not None else None
        if lock is None:
            return _NullLock()
        return lock

    def send_and_wait_for_reply(
        self,
        recv3: bytes,
        opcode7: int,
        send_fn,
        timeout_s: float = rf_timing.UNICAST_ATTEMPT_TIMEOUT_S,
    ) -> tuple[list[dict], bool]:
        """Unicast request/response helper (plan Transport Redesign Phase B).

        The Host registers the expected ``(sender, opcode/ack_of)`` with the
        :class:`PendingRequestRegistry`, calls ``send_fn``, and blocks on the
        per-request completion event. Every inbound frame flows through
        ``on_transport_event`` -> ``_pending_registry.try_match``; a match
        sets ``done`` and the waiter returns in ≤ 1 ms from the USB dispatch.

        Broadcast requests (``recv3 == FFFFFF``) do not register -- callers
        should use :meth:`send_and_collect` instead, which is the right
        primitive for "N unknown responders within a time window".
        """
        if not self.transport:
            return [], False

        self.install_transport_hooks()

        opcode7 = int(opcode7) & 0x7F
        recv3_b = bytes(recv3 or b"")
        sender_filter = recv3_b if recv3_b and recv3_b != b"\xFF\xFF\xFF" else None
        sender_filter_hex = sender_filter.hex().upper() if sender_filter else ""
        sender_dev = self.controller.getDeviceFromAddress(sender_filter_hex) if sender_filter_hex else None

        try:
            rule = protocol_rules.find_rule(opcode7)
        except Exception:
            # swallow-ok: unknown opcode -> no rule -> caller downgrades policy to RESP_NONE
            rule = None

        policy = int(response_policy(opcode7)) if rule else int(protocol_rules.RESP_NONE)
        if policy == int(protocol_rules.RESP_NONE):
            send_fn()
            return [], False

        # Broadcast fallback: no single-sender identity, so the registry
        # cannot match. Route to ``send_and_collect`` with ``expected=1`` so
        # the first matching reply wins and the idle timeout cleans up after
        # any stragglers. This is an unusual path -- broadcast callers
        # typically use ``send_and_collect`` directly.
        if sender_filter is None:
            rsp_opc = int(response_opcode(opcode7)) if rule else -1

            def _bcast_pred(ev: dict) -> bool:
                try:
                    opc = int(ev.get("opc", -1))
                    if policy == int(protocol_rules.RESP_ACK):
                        return (
                            opc == int(LP.OPC_ACK)
                            and int(ev.get("ack_of", -1)) == opcode7
                        )
                    if policy == int(protocol_rules.RESP_SPECIFIC):
                        return opc == rsp_opc
                except Exception:
                    # swallow-ok: predicate contract - malformed event
                    return False
                return False

            replies = self.send_and_collect(
                send_fn,
                _bcast_pred,
                expected=1,
                idle_timeout_s=rf_timing.COLLECT_IDLE_TIMEOUT_S,
                max_timeout_s=float(timeout_s),
            )
            return replies, bool(replies)

        if policy == int(protocol_rules.RESP_ACK):
            registry_policy = PR_RESP_ACK
            expected_key = opcode7
        else:  # RESP_SPECIFIC
            registry_policy = PR_RESP_SPECIFIC
            expected_key = int(response_opcode(opcode7)) & 0x7F if rule else opcode7

        req = self._pending_registry.register(
            sender_last3=sender_filter,
            expected_key=expected_key,
            policy=registry_policy,
            timeout_s=timeout_s,
        )
        opcode_name = self.opcode_name(opcode7)
        t0 = time.monotonic()
        logger.debug(
            "send_and_wait ENTER sender=%s opcode=0x%02X(%s) policy=%d timeout=%.2fs",
            sender_filter.hex().upper(),
            opcode7,
            opcode_name,
            policy,
            timeout_s,
        )
        try:
            send_fn()
            completed = req.done.wait(timeout=float(timeout_s))
        finally:
            self._pending_registry.cancel(req)

        elapsed = time.monotonic() - t0
        if not completed or req.reply is None:
            logger.debug(
                "send_and_wait EXIT  TIMEOUT sender=%s opcode=0x%02X(%s) elapsed=%.3fs",
                sender_filter.hex().upper(),
                opcode7,
                opcode_name,
                elapsed,
            )
            return [], False

        logger.debug(
            "send_and_wait EXIT  MATCHED sender=%s opcode=0x%02X(%s) elapsed=%.3fs",
            sender_filter.hex().upper(),
            opcode7,
            opcode_name,
            elapsed,
        )

        if sender_dev is not None:
            try:
                with self._state_lock():
                    sender_dev.mark_online()
            except Exception:
                logger.exception("RaceLink: mark_online after match raised")

        # Post-match settle: sleep briefly so the Gateway radio has time to
        # settle between the RX (reply we just consumed) and the next TX the
        # caller is likely to queue. Set ``post_match_settle_s = 0.0`` to
        # disable. See class docstring for the underlying Gateway CAD issue.
        settle = float(getattr(self, "post_match_settle_s", 0.0) or 0.0)
        if settle > 0.0:
            time.sleep(settle)
        return [req.reply], True

    def send_and_wait_with_retries(
        self,
        recv3: bytes,
        opcode7: int,
        send_fn,
        *,
        attempts: Optional[int] = None,
        per_attempt_timeout_s: Optional[float] = None,
        retry_delay_s: Optional[float] = None,
    ) -> tuple[list[dict], bool]:
        """Wait-for-reply with bounded retries on transient timeout.

        Composes ``send_and_wait_for_reply`` in a retry loop. The
        per-attempt timeout is short (``rf_timing.UNICAST_ATTEMPT_TIMEOUT_S``,
        default 1.5 s); a single dropped frame on either direction
        triggers an automatic retry rather than a false-negative
        timeout for the caller. Success on any attempt
        short-circuits.

        Defaults pulled from :mod:`rf_timing`. Worst case
        ≈ ``per_attempt × attempts + retry_delay × (attempts - 1)``,
        which with the defaults is ~4.7 s — *shorter* than the
        old 8 s single-attempt timeout this helper replaces, even
        for genuinely-offline devices.
        """
        n = int(attempts if attempts is not None else rf_timing.UNICAST_MAX_ATTEMPTS)
        if n < 1:
            n = 1
        per = float(
            per_attempt_timeout_s
            if per_attempt_timeout_s is not None
            else rf_timing.UNICAST_ATTEMPT_TIMEOUT_S
        )
        delay = float(
            retry_delay_s if retry_delay_s is not None else rf_timing.UNICAST_RETRY_DELAY_S
        )

        last_events: list[dict] = []
        for attempt in range(n):
            events, ok = self.send_and_wait_for_reply(recv3, opcode7, send_fn, timeout_s=per)
            if ok:
                if attempt > 0:
                    logger.debug(
                        "send_and_wait_with_retries: matched on attempt %d/%d (opcode=0x%02X)",
                        attempt + 1,
                        n,
                        int(opcode7) & 0x7F,
                    )
                return events, True
            last_events = events
            if attempt < n - 1 and delay > 0.0:
                time.sleep(delay)
        logger.debug(
            "send_and_wait_with_retries: exhausted %d attempts (opcode=0x%02X, per=%.2fs)",
            n,
            int(opcode7) & 0x7F,
            per,
        )
        return last_events, False

    def send_and_collect(
        self,
        send_fn,
        collect_pred,
        *,
        expected: Optional[int] = None,
        idle_timeout_s: float = rf_timing.COLLECT_IDLE_TIMEOUT_S,
        max_timeout_s: float = rf_timing.COLLECT_MAX_CEILING_S,
    ) -> list[dict]:
        """Broadcast-style collector with idle-based termination.

        The Gateway sits in Continuous RX (Phase A), so the Host owns the
        clock for *how long* to listen. Semantics:

        1. **Early exit on count.** If ``expected`` is given, return as soon
           as that many matching replies arrive.
        2. **Idle timeout.** Once the first match arrives, return when no new
           match has arrived for ``idle_timeout_s`` seconds.
        3. **Hard ceiling.** Regardless of 1 + 2, never wait longer than
           ``max_timeout_s`` from the moment ``send_fn`` is invoked. This is a
           safety net against a faulty device that streams continuously.

        Before the first match, only the hard ceiling applies -- that covers
        the "no device responded at all" case (``GET_DEVICES`` on an empty RF
        scene waits the full 5 s and then returns with ``[]``).
        """
        transport = self.transport
        if transport is None:
            return []

        self.install_transport_hooks()

        collected: list[dict] = []
        cond = threading.Condition()
        full_flag = [False]
        last_match_ts: list[Optional[float]] = [None]

        def _cb(ev: dict):
            try:
                if not isinstance(ev, dict):
                    return
                if not collect_pred(ev):
                    return
                with cond:
                    collected.append(ev)
                    last_match_ts[0] = time.monotonic()
                    if expected is not None and len(collected) >= int(expected):
                        full_flag[0] = True
                    cond.notify_all()
            except Exception:
                logger.exception("RaceLink: send_and_collect predicate raised")

        transport.add_listener(_cb)
        reason = "unknown"
        try:
            t_start = time.monotonic()
            hard_deadline = t_start + float(max_timeout_s)
            logger.debug(
                "send_and_collect ENTER expected=%s idle=%.2fs max=%.2fs",
                expected,
                idle_timeout_s,
                max_timeout_s,
            )
            send_fn()
            with cond:
                while True:
                    now = time.monotonic()
                    if full_flag[0]:
                        reason = "count"
                        break
                    if now >= hard_deadline:
                        reason = "max_timeout" if last_match_ts[0] is not None else "no_reply"
                        break
                    if last_match_ts[0] is None:
                        # No match yet -- block up to the hard deadline.
                        wait_s = max(0.0, hard_deadline - now)
                    else:
                        idle_deadline = last_match_ts[0] + float(idle_timeout_s)
                        effective_deadline = min(idle_deadline, hard_deadline)
                        wait_s = effective_deadline - now
                        if wait_s <= 0.0:
                            # Idle window already expired since the last
                            # match -- no need to wait further.
                            reason = "idle"
                            break
                    cond.wait(timeout=wait_s)
        finally:
            try:
                transport.remove_listener(_cb)
            except Exception:
                logger.debug("RaceLink: remove_listener failed after send_and_collect", exc_info=True)
        logger.debug(
            "send_and_collect EXIT  reason=%s collected=%d elapsed=%.3fs",
            reason,
            len(collected),
            time.monotonic() - t_start,
        )
        return collected

    @staticmethod
    def compute_collect_max_timeout(
        expected: int,
        *,
        base_s: float = rf_timing.COLLECT_BASE_S,
        per_device_s: float = rf_timing.COLLECT_PER_DEVICE_S,
        ceiling_s: float = rf_timing.COLLECT_MAX_CEILING_S,
    ) -> float:
        """Derive a max-timeout ceiling from the expected responder count.

        ``base_s`` covers LBT/jitter + first-reply latency; ``per_device_s``
        scales with the known population. The final value is clamped to
        ``ceiling_s`` so very large groups cannot pin the server thread.
        """
        n = max(0, int(expected))
        return min(ceiling_s, base_s + n * float(per_device_s))

    def send_config(
        self,
        option,
        data0=0,
        data1=0,
        data2=0,
        data3=0,
        recv3=b"\xFF\xFF\xFF",
        wait_for_ack: bool = False,
        timeout_s: Optional[float] = None,
    ):
        transport = self.transport
        if transport is None:
            logger.warning("sendConfig: communicator not ready")
            return False if wait_for_ack else None

        recv3_hex = recv3.hex().upper() if isinstance(recv3, (bytes, bytearray)) else ""
        dev = None
        if recv3_hex and recv3_hex != "FFFFFF":
            # Locked stash — paired with ``take_pending_config`` on the RX
            # path below. See controller docstring for the threading
            # contract.
            self.controller.stash_pending_config(recv3_hex, option, data0)
            dev = self.controller.getDeviceFromAddress(recv3_hex)
            if dev and wait_for_ack:
                dev.ack_clear()

        def _send():
            transport.send_config(
                recv3=recv3,
                option=int(option) & 0xFF,
                data0=int(data0) & 0xFF,
                data1=int(data1) & 0xFF,
                data2=int(data2) & 0xFF,
                data3=int(data3) & 0xFF,
            )

        if wait_for_ack:
            if not dev:
                _send()
                return False
            per_attempt = (
                float(timeout_s)
                if timeout_s is not None
                else rf_timing.UNICAST_ATTEMPT_TIMEOUT_S
            )
            events, _ = self.send_and_wait_with_retries(
                recv3,
                LP.OPC_CONFIG,
                _send,
                per_attempt_timeout_s=per_attempt,
            )
            if not events:
                return False
            ev = events[-1]
            return bool(int(ev.get("ack_status", 1)) == 0)
        _send()
        return True

    def send_sync(self, ts24, brightness, recv3=b"\xFF\xFF\xFF", *, trigger_armed: bool = False):
        if not self.transport:
            logger.warning("sendSync: communicator not ready")
            return
        from ..protocol.packets import SYNC_FLAG_TRIGGER_ARMED
        flags = SYNC_FLAG_TRIGGER_ARMED if trigger_armed else 0
        self.transport.send_sync(recv3=recv3, ts24=int(ts24) & 0xFFFFFF,
                                 brightness=int(brightness) & 0xFF, flags=flags)

    def send_stream(
        self,
        payload: bytes,
        groupId: Optional[int] = None,
        device=None,
        retries: int = rf_timing.STREAM_MAX_ATTEMPTS - 1,
        timeout_s: float = rf_timing.STREAM_ATTEMPT_TIMEOUT_S,
    ) -> dict[str, int]:
        transport = self.transport
        if transport is None:
            logger.warning("sendStream: communicator not ready")
            return {}

        self.install_transport_hooks()

        # For OPC_STREAM the host provides one logical payload. The gateway is
        # responsible for fragmenting it into radio packets and assigning the
        # per-packet stream control bytes.
        data = bytes(payload or b"")
        if len(data) > 128:
            raise ValueError("payload too large (max 128 bytes)")

        if device is None and groupId is None:
            raise ValueError("sendStream requires groupId or device")

        if device is None:
            assert groupId is not None  # narrowed by the guard above
            group_filter = int(groupId)
            # A6: snapshot the matching devices under the state lock so a
            # concurrent IDENTIFY append / device delete cannot raise on
            # iteration. The list comprehension materialises the result
            # immediately, so the lock can be released before the slower
            # downstream stream-send work begins.
            with self._state_lock():
                targets = [
                    dev
                    for dev in self.controller.device_repository.list()
                    if int(getattr(dev, "groupId", 0) or 0) == group_filter
                ]
        else:
            targets = [device]

        target_last3 = {mac_last3_from_hex(dev.addr) for dev in targets if dev and dev.addr}
        target_last3.discard(b"\xFF\xFF\xFF")
        expected = len(target_last3)
        if expected == 0:
            return {"expected": 0, "acked": 0}

        recv3 = b"\xFF\xFF\xFF" if device is None else mac_last3_from_hex(device.addr)
        if recv3 == b"\xFF\xFF\xFF" and device is not None:
            return {"expected": expected, "acked": 0}

        try:
            transport.drain_events(0.0)
        except Exception:
            logger.debug("RaceLink: drain_events before send_stream raised", exc_info=True)

        acked = set()

        def _collect(ev: dict) -> bool:
            try:
                if ev.get("opc") != LP.OPC_ACK:
                    return False
                if int(ev.get("ack_of", -1)) != int(LP.OPC_STREAM):
                    return False
                sender3 = ev.get("sender3")
                if not isinstance(sender3, (bytes, bytearray)):
                    return False
                sender3_b = bytes(sender3)
                if sender3_b not in target_last3:
                    return False
                acked.add(sender3_b)
                return True
            except Exception:
                # swallow-ok: predicate contract - malformed event -> "not an ack"
                return False

        # Plan Phase C (revised): each retry iteration returns as soon as all
        # targets have ACKed, or after ``idle_timeout_s`` of silence on an
        # already-partial set, capped by a max derived from the target count.
        max_ceiling = float(timeout_s)
        max_timeout = min(
            max_ceiling,
            self.compute_collect_max_timeout(expected, ceiling_s=max_ceiling),
        )
        for attempt in range(max(0, int(retries)) + 1):
            self.send_and_collect(
                lambda: transport.send_stream(recv3=recv3, payload=data),
                _collect,
                expected=expected,
                idle_timeout_s=rf_timing.COLLECT_IDLE_TIMEOUT_S,
                max_timeout_s=max_timeout,
            )
            if len(acked) >= expected:
                break
            if attempt < int(retries):
                time.sleep(rf_timing.STREAM_RETRY_DELAY_S)

        return {"expected": expected, "acked": len(acked)}

    def wait_rx_window(
        self,
        send_fn,
        collect_pred=None,
        fail_safe_s: float = 8.0,
        *,
        stop_on_match: bool = False,
    ):
        """Legacy reply-window helper (deprecated -- plan Transport Redesign D).

        The Gateway no longer drives a Timed RX window after unicast TX; it
        stays in Continuous RX. New callers should use:

        * :meth:`send_and_wait_for_reply` for unicast request/response (uses
          :class:`PendingRequestRegistry`),
        * :meth:`send_and_collect` for broadcast collectors (wall-clock based).

        Batch B (2026-04-28) collapsed EV_RX_WINDOW_OPEN/CLOSED into
        EV_STATE_CHANGED; the "window closed" signal is now an
        EV_STATE_CHANGED transition out of the RX_WINDOW state byte. This
        helper detects that transition while remaining backwards-compatible
        with its (collected, got_closed) return tuple.
        """
        if not self.transport:
            return [], False

        transport = self.transport
        collected = []
        got_closed = False

        def _is_window_closed_transition(ev: dict) -> bool:
            # EV_STATE_CHANGED away from RX_WINDOW = the legacy "closed".
            if ev.get("type") != EV_STATE_CHANGED:
                return False
            new_state = int(ev.get("state_byte", -1))
            return new_state != GATEWAY_STATE_RX_WINDOW

        if hasattr(transport, "add_listener") and hasattr(transport, "remove_listener"):
            closed_ev = threading.Event()

            def _cb(ev: dict):
                nonlocal got_closed
                try:
                    if not isinstance(ev, dict):
                        return
                    if _is_window_closed_transition(ev):
                        got_closed = True
                        closed_ev.set()
                        return
                    if collect_pred and collect_pred(ev):
                        collected.append(ev)
                        if stop_on_match:
                            closed_ev.set()
                except Exception:
                    logger.exception("RaceLink: reply-collector callback raised")

            transport.add_listener(_cb)
            try:
                send_fn()
                closed_ev.wait(timeout=float(fail_safe_s))
            finally:
                try:
                    transport.remove_listener(_cb)
                except Exception:
                    logger.debug("RaceLink: remove_listener failed during cleanup", exc_info=True)
            return collected, got_closed

        send_fn()
        t_end = time.time() + float(fail_safe_s)
        while time.time() < t_end:
            for ev in transport.drain_events(timeout_s=0.1):
                if _is_window_closed_transition(ev):
                    got_closed = True
                    return collected, got_closed
                if collect_pred and collect_pred(ev):
                    collected.append(ev)
                    if stop_on_match:
                        return collected, got_closed
        return collected, got_closed

    def query_state(self, *, timeout_s: float = 0.5) -> dict:
        """Send GW_CMD_STATE_REQUEST and wait for the matching EV_STATE_REPORT.

        Returns a dict with the same shape the SSE layer broadcasts:

            {
                "state": "IDLE" | "TX" | "RX_WINDOW" | "RX" | "ERROR" | "UNKNOWN",
                "state_byte": int,
                "state_metadata_ms": int,
                "ok": bool,            # True iff a STATE_REPORT actually arrived
            }

        Used at startup (host-side seed of the master pill before any
        spontaneous EV_STATE_CHANGED would have fired) and from the master-
        pill ↻ refresh button. ``timeout_s`` is short by design — the round-
        trip is a USB write + USB read with no LoRa airtime; 500 ms is
        generous.
        """
        transport = self.transport
        if transport is None:
            return {
                "ok": False,
                "state": "UNKNOWN",
                "state_byte": GATEWAY_STATE_UNKNOWN,
                "state_metadata_ms": 0,
            }

        replied = threading.Event()
        result: dict = {}

        def _cb(ev: dict):
            try:
                if not isinstance(ev, dict):
                    return
                if ev.get("type") != EV_STATE_REPORT:
                    return
                result["state"] = ev.get("state") or GATEWAY_STATE_NAME.get(
                    int(ev.get("state_byte", GATEWAY_STATE_UNKNOWN)), "UNKNOWN",
                )
                result["state_byte"] = int(ev.get("state_byte", GATEWAY_STATE_UNKNOWN))
                result["state_metadata_ms"] = int(ev.get("state_metadata_ms", 0) or 0)
                replied.set()
            except Exception:
                logger.debug("query_state callback raised", exc_info=True)

        try:
            transport.add_listener(_cb)
        except Exception:
            logger.debug("query_state: add_listener failed", exc_info=True)
            # Fall through to write-and-snapshot — better than failing hard.

        try:
            ok_write = True
            try:
                send = getattr(transport, "send_state_request", None)
                if callable(send):
                    ok_write = bool(send())
            except Exception:
                logger.debug("query_state: send_state_request raised", exc_info=True)
                ok_write = False

            if ok_write:
                replied.wait(timeout=float(timeout_s))
        finally:
            try:
                transport.remove_listener(_cb)
            except Exception:
                logger.debug("query_state: remove_listener failed", exc_info=True)

        if result:
            result["ok"] = True
            return result

        # Fallback: report the transport's last-known state. Better than
        # nothing — the operator at least sees whatever the pill mirror has.
        snap = getattr(transport, "gateway_state_snapshot", None)
        if callable(snap):
            try:
                snap_obj = snap()
                if isinstance(snap_obj, dict):
                    base: dict = dict(snap_obj)
                    base["ok"] = False
                    return base
            except Exception:
                logger.debug("query_state: snapshot raised", exc_info=True)
        return {
            "ok": False,
            "state": "UNKNOWN",
            "state_byte": GATEWAY_STATE_UNKNOWN,
            "state_metadata_ms": 0,
        }

    def opcode_name(self, opcode7: int) -> str:
        return protocol_opcode_name(int(opcode7) & 0x7F)

    def log_transport_reply(self, ev: dict) -> None:
        try:
            opc = int(ev.get("opc", -1)) & 0x7F
        except Exception:
            # swallow-ok: malformed event in a best-effort log helper
            return

        sender3_hex = self.controller._to_hex_str(ev.get("sender3")) or "??????"

        if opc == int(LP.OPC_ACK):
            ack_of = ev.get("ack_of")
            ack_status = ev.get("ack_status")
            ack_seq = ev.get("ack_seq")
            if ack_of is None or ack_status is None:
                return
            ack_name = self.opcode_name(int(ack_of))
            logger.debug("ACK from %s: ack_of=%s (%s) status=%s seq=%s", sender3_hex, int(ack_of), ack_name, int(ack_status), ack_seq)
            return

        if opc == int(LP.OPC_STATUS) and ev.get("reply") == "STATUS_REPLY":
            logger.debug(
                "STATUS from %s: flags=0x%02X cfg=0x%02X effect=%s bri=%s vbat=%s rssi=%s snr=%s host_rssi=%s host_snr=%s",
                sender3_hex,
                int(ev.get("flags", 0) or 0) & 0xFF,
                int(ev.get("configByte", 0) or 0) & 0xFF,
                ev.get("effectId"),
                ev.get("brightness"),
                ev.get("vbat_mV"),
                ev.get("node_rssi"),
                ev.get("node_snr"),
                ev.get("host_rssi"),
                ev.get("host_snr"),
            )
            return

        if opc == int(LP.OPC_DEVICES) and ev.get("reply") == "IDENTIFY_REPLY":
            mac6 = ev.get("mac6")
            mac12 = bytes(mac6).hex().upper() if isinstance(mac6, (bytes, bytearray)) and len(mac6) == 6 else None
            dev_type = ev.get("caps")
            dtype_name = get_dev_type_info(dev_type).get("name")
            logger.debug(
                "IDENTIFY from %s: mac=%s group=%s ver=%s dev_type=%s (%s) host_rssi=%s host_snr=%s",
                sender3_hex,
                mac12 or sender3_hex,
                ev.get("groupId"),
                ev.get("version"),
                dev_type,
                dtype_name,
                ev.get("host_rssi"),
                ev.get("host_snr"),
            )
            return

        if ev.get("reply"):
            logger.debug("RX %s from %s (opc=0x%02X)", ev.get("reply"), sender3_hex, opc)

    def log_state_event(self, ev: dict) -> None:
        """Log a Batch-B EV_STATE_CHANGED / EV_STATE_REPORT event for diagnostics.

        Replaces the pre-Batch-B ``log_rx_window_event``: the window-open /
        window-closed pair is now expressed as transitions of the gateway's
        single state byte. RX_WINDOW carries ``state_metadata_ms`` (the
        ``min_ms`` window size); other states carry 0.
        """
        t = ev.get("type")
        if t not in (EV_STATE_CHANGED, EV_STATE_REPORT):
            return
        state_name = ev.get("state") or GATEWAY_STATE_NAME.get(int(ev.get("state_byte", -1)), "UNKNOWN")
        meta_ms = int(ev.get("state_metadata_ms", 0) or 0)
        if state_name == "RX_WINDOW":
            logger.debug("Gateway state -> RX_WINDOW (min_ms=%s)", meta_ms)
        else:
            logger.debug("Gateway state -> %s", state_name)

    def handle_ack_event(self, ev: dict) -> None:
        try:
            sender3_hex = self.controller._to_hex_str(ev.get("sender3"))
            with self._state_lock():
                dev = self.controller.getDeviceFromAddress(sender3_hex) if sender3_hex else None
                if not dev:
                    return

                ack_of = ev.get("ack_of")
                ack_status = ev.get("ack_status")
                ack_seq = ev.get("ack_seq")
                host_rssi = ev.get("host_rssi")
                host_snr = ev.get("host_snr")

                if ack_of is None or ack_status is None:
                    return

                dev.ack_update(int(ack_of), int(ack_status), ack_seq, host_rssi, host_snr)

                if int(ack_of) == int(LP.OPC_CONFIG) and int(ack_status) == 0:
                    # Locked pop — paired with ``stash_pending_config`` on
                    # the TX path. ``_apply_config_update`` runs outside
                    # the pending-config lock so a slow ConfigService
                    # callback cannot delay the next stash.
                    pending = self.controller.take_pending_config(sender3_hex)
                    if pending:
                        self.controller._apply_config_update(dev, pending.get("option", 0), pending.get("data0", 0))

        except Exception:
            logger.exception("ACK handling failed")

    def install_transport_hooks(self) -> None:
        if self.controller._transport_hooks_installed:
            return
        transport = self.transport
        if not transport:
            return

        try:
            if hasattr(transport, "add_listener"):
                transport.add_listener(self.on_transport_event)
            else:
                prev = getattr(transport, "on_event", None)

                def _mux(ev):
                    try:
                        self.on_transport_event(ev)
                    except Exception:
                        logger.exception("RaceLink: gateway service transport handler raised")
                    if prev:
                        try:
                            prev(ev)
                        except Exception:
                            logger.exception("RaceLink: downstream on_event handler raised")

                transport.on_event = _mux
        except Exception:
            logger.exception("RaceLink: failed to install transport RX listener")

        try:
            if hasattr(transport, "add_tx_listener"):
                transport.add_tx_listener(self.on_transport_tx)
        except Exception:
            logger.exception("RaceLink: failed to install transport TX listener")

        self.controller._transport_hooks_installed = True

    def on_transport_tx(self, ev: dict) -> None:
        try:
            if not ev or ev.get("type") != "TX_M2N":
                return
            recv3 = ev.get("recv3")
            if not isinstance(recv3, (bytes, bytearray)) or len(recv3) != 3:
                return
            recv3_b = bytes(recv3)

            if recv3_b == b"\xFF\xFF\xFF":
                return

            opcode7 = int(ev.get("opc", -1)) & 0x7F
            try:
                rule = protocol_rules.find_rule(opcode7)
            except Exception:
                # swallow-ok: unknown opcode treated as "no rule" -> skip TX tracking
                rule = None
            if not rule:
                return

            if int(request_direction(opcode7)) != int(protocol_rules.DIR_M2N):
                return

            policy = int(response_policy(opcode7))
            if policy == int(protocol_rules.RESP_NONE):
                return

            dev = self.controller.getDeviceFromAddress(recv3_b.hex().upper())
            if not dev:
                return

            # A5: stash via the controller helper so the TX-listener
            # write is atomic against the RX-reader's match/clear path.
            self.controller.set_pending_expect(
                dev=dev,
                rule=rule,
                opcode7=opcode7,
                sender_last3=(dev.addr or "").upper()[-6:],
                ts=time.time(),
            )
        except Exception:
            logger.exception("RaceLink: TX hook failed")

    def on_transport_event(self, ev: dict) -> None:
        try:
            if not isinstance(ev, dict):
                return

            t = ev.get("type")

            if t == EV_ERROR:
                reason = str(ev.get("data") or "unknown error")
                self.controller.ready = False
                now = time.time()
                if (now - self.controller._last_error_notify_ts) > 2:
                    self.controller._last_error_notify_ts = now
                    try:
                        host_api = getattr(self.controller, "_host_api", None)
                        ui = getattr(host_api, "ui", None) if host_api is not None else None
                        notify = getattr(ui, "message_notify", None) if ui is not None else None
                        translator = getattr(host_api, "__", None) if host_api is not None else None
                        if callable(notify):
                            template = "RaceLink Gateway disconnected: {}"
                            if callable(translator):
                                translated = translator(template)
                                template = translated if isinstance(translated, str) else template
                            notify(template.format(reason))
                    except Exception:
                        logger.exception("RaceLink: failed to notify UI about disconnect")
                self.schedule_reconnect(reason)
                return

            if t in (EV_STATE_CHANGED, EV_STATE_REPORT):
                self.log_state_event(ev)
                # Post-Batch-B: pending unicast requests time out via the
                # registry's wall-clock deadline rather than via an explicit
                # RX_WINDOW_CLOSED event. We still trigger the timeout-style
                # pending_window_closed sweep on the RX_WINDOW -> non-window
                # transition so the legacy "missing reply" path still fires
                # for the broadcast-fallback callers that depend on it.
                state_byte = int(ev.get("state_byte", -1))
                if state_byte != GATEWAY_STATE_RX_WINDOW:
                    self.pending_window_closed(ev)
                return

            if t == EV_TX_REJECTED:
                # Diagnostic surface only — _send_m2n's outcome wait already
                # received this NACK and converted it to a SendOutcome.REJECTED.
                # Logging keeps the failure observable for non-_send_m2n paths
                # (e.g. orphan NACKs from gateway-internal auto-sync).
                logger.debug(
                    "EV_TX_REJECTED type=0x%02X opc=0x%02X reason=%s",
                    int(ev.get("type_full", 0) or 0),
                    int(ev.get("opc", 0) or 0),
                    ev.get("reason_name") or ev.get("reason"),
                )
                return

            if t == EV_TX_DONE:
                # Post-redesign diagnostic: when an inbound reply never
                # arrives, knowing whether the Gateway ever emitted TX_DONE
                # distinguishes "CAD/LBT stuck" from "RF ACK lost".
                logger.debug(
                    "EV_TX_DONE last_len=%s ts=%.3f", ev.get("last_len"), ev.get("ts", time.time())
                )
                return

            opc = ev.get("opc")
            if opc is None:
                # Any unknown event byte (e.g. EV_IDLE 0xF4) -- still log so
                # we can see the full USB event stream during diagnostics.
                if t is not None:
                    logger.debug("transport event type=0x%02X data=%r", int(t), ev.get("data"))
                return

            self.log_transport_reply(ev)

            # Plan Phase B: complete any matching unicast waiter first. This
            # unblocks ``send_and_wait_for_reply`` immediately; the remainder
            # of this handler then updates device state for the same event so
            # the unsolicited pipeline keeps working.
            try:
                self._pending_registry.try_match(ev)
            except Exception:
                logger.exception("RaceLink: pending-registry match raised")

            if int(opc) == int(LP.OPC_ACK):
                self.handle_ack_event(ev)
            elif int(opc) == int(LP.OPC_STATUS) and ev.get("reply") == "STATUS_REPLY":
                sender3_hex = self.controller._to_hex_str(ev.get("sender3"))
                with self._state_lock():
                    dev = self.controller.getDeviceFromAddress(sender3_hex) if sender3_hex else None
                    if dev:
                        dev.update_from_status(
                            ev.get("flags"),
                            ev.get("configByte"),
                            ev.get("effectId"),
                            ev.get("brightness"),
                            ev.get("vbat_mV"),
                            ev.get("node_rssi"),
                            ev.get("node_snr"),
                            ev.get("host_rssi"),
                            ev.get("host_snr"),
                        )
            elif int(opc) == int(LP.OPC_DEVICES) and ev.get("reply") == "IDENTIFY_REPLY":
                mac6 = ev.get("mac6")
                if isinstance(mac6, (bytes, bytearray)) and len(mac6) == 6:
                    mac12 = bytes(mac6).hex().upper()
                    with self._state_lock():
                        dev = self.controller.getDeviceFromAddress(mac12)
                        is_known_device = dev is not None
                        if not dev:
                            dev_type = ev.get("caps", 0)
                            dev = create_device(addr=mac12, dev_type=int(dev_type or 0), name=f"WLED {mac12}")
                            self.controller.device_repository.append(dev)

                        dev.update_from_identify(
                            ev.get("version"),
                            ev.get("caps"),
                            ev.get("groupId"),
                            mac6,
                            ev.get("host_rssi"),
                            ev.get("host_snr"),
                        )
                    self._restore_known_device_group(dev, reported_group=ev.get("groupId"), is_known_device=is_known_device)

            self.pending_try_match(ev)
        except Exception:
            logger.exception("RaceLink: RX hook failed")

    def _restore_known_device_group(self, dev, *, reported_group, is_known_device: bool) -> None:
        if not is_known_device or not dev:
            return

        try:
            node_group = int(reported_group or 0) & 0xFF
        except Exception:
            # swallow-ok: malformed groupId in IDENTIFY reply -> treat as "unconfigured"
            node_group = 0

        if node_group != 0:
            return

        if self._is_discovery_active():
            return

        try:
            stored_group = int(getattr(dev, "groupId", 0) or 0) & 0xFF
        except Exception:
            logger.debug("RaceLink: unreadable stored groupId on %r", getattr(dev, "addr", "?"), exc_info=True)
            stored_group = 0

        try:
            group_count = len(self.controller.group_repository.list())
        except Exception:
            logger.debug("RaceLink: group_repository length unavailable", exc_info=True)
            group_count = 0

        if stored_group >= group_count:
            stored_group = 0
            try:
                dev.groupId = 0
            except Exception:
                logger.debug("RaceLink: could not reset invalid groupId on %r", getattr(dev, "addr", "?"), exc_info=True)

        if stored_group == node_group:
            return

        mac = str(getattr(dev, "addr", "") or "").upper()
        if not mac:
            return
        if self._auto_reassign_suppressed(mac):
            return

        # Plan P2-6: wait for the ACK, but do it off the transport thread so
        # blocking here never stalls reply collection. A 3s timeout bounds
        # the worker; on failure we mark the device offline so the UI shows
        # the mismatch instead of silently masking it.
        self._mark_auto_reassign(mac)
        self._spawn_auto_reassign_worker(dev, stored_group=stored_group)

    def _spawn_auto_reassign_worker(self, dev, *, stored_group: int) -> None:
        """Submit ``setNodeGroupId(wait_for_ack=True)`` to the bounded
        auto-restore executor. The executor caps concurrency at 8
        workers (see ``__init__``) and reuses threads across submits."""
        def _worker():
            try:
                ok = self.controller.setNodeGroupId(
                    dev, forceSet=True, wait_for_ack=True
                )
            except Exception:
                logger.exception(
                    "RaceLink: auto-restore SET_GROUP raised for %s (target group=%s)",
                    getattr(dev, "addr", "?"),
                    stored_group,
                )
                return
            if ok is False:
                logger.warning(
                    "RaceLink: auto-restore SET_GROUP not ACKed for %s (target group=%s)",
                    getattr(dev, "addr", "?"),
                    stored_group,
                )
                try:
                    with self._state_lock():
                        dev.mark_offline("Auto-restore SET_GROUP timeout")
                except Exception:
                    logger.exception(
                        "RaceLink: failed to mark %s offline after auto-restore timeout",
                        getattr(dev, "addr", "?"),
                    )

        with self._auto_reassign_lock:
            # Prune completed futures so the in-flight list stays
            # bounded between submits. The executor itself manages
            # the worker threads; this list is purely for the test
            # join-hook below.
            self._auto_restore_futures = [
                f for f in self._auto_restore_futures if not f.done()
            ]
            try:
                fut = self._auto_restore_executor.submit(_worker)
            except RuntimeError:
                # swallow-ok: executor was shut down (process is
                # exiting). Nothing else to do — the auto-restore
                # is best-effort.
                logger.debug(
                    "auto-restore executor refused submit (shut down?)",
                    exc_info=True,
                )
                return
            self._auto_restore_futures.append(fut)

    def _join_auto_restore_workers(self, timeout: float = 5.0) -> None:
        """Wait for in-flight auto-restore workers to complete.

        Test hook — production code never needs to join (the workers
        are best-effort). Iterates over a snapshot of the futures so
        a concurrent submit doesn't change what we're waiting on.
        """
        with self._auto_reassign_lock:
            futs = list(self._auto_restore_futures)
        for fut in futs:
            try:
                fut.result(timeout=timeout)
            except Exception:
                # swallow-ok: test hook just wants a deterministic
                # "all in-flight work has finished" barrier.
                # The worker itself logs any real failure.
                logger.debug(
                    "auto-restore worker future raised in test join",
                    exc_info=True,
                )

    def shutdown(self) -> None:
        """Release the auto-restore executor. Safe to call multiple
        times. Called from ``RaceLink_Host.shutdown``; tests can also
        invoke this to ensure the pool is gone before the test exits.
        """
        try:
            self._auto_restore_executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            # swallow-ok: shutdown is best-effort cleanup. ``shutdown``
            # raises only if the executor is already in a broken
            # state; nothing useful to do at that point.
            logger.debug("auto-restore executor shutdown raised", exc_info=True)

    def _is_discovery_active(self) -> bool:
        checker = getattr(self.controller, "is_discovery_active", None)
        if not callable(checker):
            return False
        try:
            return bool(checker())
        except Exception:
            # swallow-ok: best-effort query; when in doubt we assume "no discovery"
            return False

    def _auto_reassign_suppressed(self, mac: str) -> bool:
        now = time.time()
        with self._auto_reassign_lock:
            self._prune_auto_reassign_cache_locked(now)
            last_ts = float(self._auto_reassign_recent.get(mac, 0.0) or 0.0)
        return (now - last_ts) < float(self._auto_reassign_cooldown_s)

    def _mark_auto_reassign(self, mac: str) -> None:
        with self._auto_reassign_lock:
            self._auto_reassign_recent[mac] = time.time()

    def _prune_auto_reassign_cache(self, now: float | None = None) -> None:
        """Public variant (kept for backwards compatibility in tests)."""
        with self._auto_reassign_lock:
            self._prune_auto_reassign_cache_locked(now)

    def _prune_auto_reassign_cache_locked(self, now: float | None = None) -> None:
        now_ts = time.time() if now is None else float(now)
        expiry = max(float(self._auto_reassign_cooldown_s) * 4.0, 5.0)
        stale = [mac for mac, ts in self._auto_reassign_recent.items() if (now_ts - float(ts or 0.0)) >= expiry]
        for mac in stale:
            self._auto_reassign_recent.pop(mac, None)

    def schedule_reconnect(self, reason: str) -> None:
        now = time.time()
        if self.controller._reconnect_in_progress or (now - self.controller._last_reconnect_ts) < 5:
            return
        self.controller._last_reconnect_ts = now
        self.controller._reconnect_in_progress = True
        # Mark that the gateway link was lost during active use; if the next
        # ``discoverPort`` cannot find a matching device (e.g. user pulled the
        # USB cable), ``_record_gateway_error`` will upgrade the resulting
        # NOT_FOUND to LINK_LOST so the backoff timer keeps polling.
        self.controller._link_recovery_pending = True

        def _reconnect():
            try:
                logger.warning("RaceLink: attempting gateway transport reconnect after error: %s", reason)
                try:
                    if self.transport:
                        self.transport.close()
                except Exception:
                    logger.debug("RaceLink: error closing transport during reconnect", exc_info=True)
                self.controller.transport = None
                # Transport-level disconnect is automatic by definition -- mark
                # the reconnect attempt accordingly so it does not escalate to
                # ERROR on the RotorHazard log bridge.
                self.controller.discoverPort({}, origin="auto")
            finally:
                self.controller._reconnect_in_progress = False

        # A8: named so threading.enumerate() / py-spy outputs are
        # legible during a reconnect storm.
        threading.Thread(target=_reconnect, daemon=True, name="rl-reconnect").start()

    def pending_try_match(self, ev: dict) -> None:
        # A5: snapshot via the controller helper, then use compare-and-
        # clear semantics so a freshly-stamped expectation from the TX
        # thread cannot be silently wiped by our clear below.
        p = self.controller.read_pending_expect()
        if not p:
            return

        try:
            sender3_hex = self.controller._to_hex_str(ev.get("sender3")).upper()
            if not sender3_hex:
                return
            if sender3_hex != (p.get("sender_last3") or "").upper():
                return

            opcode7 = int(p.get("opcode7", -1)) & 0x7F
            policy = int(response_policy(opcode7))

            matched = False
            if policy == int(protocol_rules.RESP_ACK):
                if int(ev.get("opc", -1)) == int(LP.OPC_ACK) and int(ev.get("ack_of", -2)) == opcode7:
                    matched = True
            elif policy == int(protocol_rules.RESP_SPECIFIC):
                rsp_opc = int(response_opcode(opcode7))
                if int(ev.get("opc", -1)) == rsp_opc:
                    matched = True

            if matched:
                dev = p.get("dev")
                with self._state_lock():
                    if dev:
                        dev.mark_online()
                # CAS-clear: only drops the expectation if it's still the
                # one we matched on. If the TX thread has stamped a new
                # one mid-flight, leave it alone.
                self.controller.clear_pending_expect_if(p)
        except Exception:
            logger.exception("RaceLink: pending match failed")

    def pending_window_closed(self, ev: dict) -> None:
        # A5: snapshot + CAS-clear, same shape as pending_try_match. A
        # window-closed without a reply means *the expectation we were
        # tracking* timed out — if the TX thread has since stamped a
        # new one, that new request is for a different operation and
        # must not be wiped.
        p = self.controller.read_pending_expect()
        if not p:
            return

        try:
            dev = p.get("dev")
            rule = p.get("rule")
            opcode7 = int(p.get("opcode7", -1)) & 0x7F
            name = getattr(rule, "name", f"opc=0x{opcode7:02X}")
            with self._state_lock():
                if dev:
                    dev.mark_offline(f"Missing reply ({name})")
        finally:
            self.controller.clear_pending_expect_if(p)
