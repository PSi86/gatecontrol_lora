"""Central timing constants for RF wait-for-reply paths.

Single source of truth for every timeout / retry decision the RF
code makes. Per-attempt timeouts are sized to typical LoRa
round-trip on a healthy link (SF7BW250, ~50 B payload, < 1 s
nominal). Bounded retries absorb transient packet loss without
inflating the worst case beyond the previous single-attempt
budget.

Pre-2026-04-29 the values lived as magic numbers scattered across
``controller.py``, ``gateway_service.py``, ``stream_service.py``,
``status_service.py``, ``discovery_service.py`` and the
config_service / control_service. Centralising them here means
future tuning is one edit, not a grep-and-pray sweep.

Used by:

* :class:`GatewayService.send_and_wait_for_reply` (default
  ``timeout_s``)
* :class:`GatewayService.send_and_wait_with_retries` (per-attempt
  + retry policy for unicast paths)
* :meth:`RL.setNodeGroupId` (controller.py) — SET_GROUP unicast
* :meth:`ConfigService.send_config` — OPC_CONFIG unicast
* :class:`StreamService.send_stream` — OPC_STREAM with retries
* :class:`StatusService.get_status` — collect-style window
* :class:`DiscoveryService.discover_devices` — collect-style
  window with hard ceiling

Worst-case budgets (with current values):

* Unicast: ``UNICAST_ATTEMPT_TIMEOUT_S × UNICAST_MAX_ATTEMPTS +
  UNICAST_RETRY_DELAY_S × (UNICAST_MAX_ATTEMPTS - 1)`` ≈ 4.7 s
  (vs. 8.0 s pre-2026-04-29 single-attempt).
* Stream: ``STREAM_ATTEMPT_TIMEOUT_S × STREAM_MAX_ATTEMPTS +
  STREAM_RETRY_DELAY_S × (STREAM_MAX_ATTEMPTS - 1)`` ≈ 18.2 s
  worst case (was ~24 s).

The unicast worst case is *shorter* than the previous
single-attempt timeout, so even genuinely-offline devices that
would have hit the old 8 s ceiling now hit ~4.7 s instead — and
combined with the route-level skip-offline pattern (in
``_apply_device_meta_updates`` and ``api_groups_force``) the
operator's UX cost for offline devices drops to zero.
"""

from __future__ import annotations


# ---------------------------------------------------------------
# Unicast request/response (SET_GROUP, CONFIG, ...)
# ---------------------------------------------------------------

UNICAST_ATTEMPT_TIMEOUT_S: float = 1.5
"""Per-attempt timeout for a unicast wait-for-reply (seconds).

Sized to typical LoRa round-trip + Gateway settle + Host RX
matching: ~300–600 ms on a healthy link. 1.5 s gives ~3× margin
without becoming the operator-visible "is something broken?"
ceiling. Lower than this risks false-negatives on slow links;
higher than this loses the responsiveness gain over the old 8 s.
"""

UNICAST_MAX_ATTEMPTS: int = 3
"""Total attempts (initial + retries) for unicast wait-for-reply.

3 = 1 initial + 2 retries. Enough to absorb a single dropped
frame in either direction without blocking the operator for
unbounded time.
"""

UNICAST_RETRY_DELAY_S: float = 0.1
"""Cooldown between attempts (seconds).

Short enough to not bloat the worst case, long enough that the
gateway radio finishes its RX → TX transition before the next
TX queues. Mirrors the value ``send_stream`` already used.
"""


# ---------------------------------------------------------------
# Collect-style (broadcast / multi-reply windows)
# ---------------------------------------------------------------

COLLECT_IDLE_TIMEOUT_S: float = 0.6
"""Idle-after-first-match timeout for collect windows (seconds).

Once the first reply lands, the collector returns when no new
matching reply arrives for this long. Short = responsive,
risks cutting off stragglers; long = waits for slow nodes,
risks operator-visible delay.
"""

COLLECT_MAX_CEILING_S: float = 5.0
"""Hard ceiling for collect windows (seconds).

Used by discover (no expected count, so this is the entire
budget) and as the cap for the dynamic
``compute_collect_max_timeout`` ceiling.
"""

COLLECT_PER_DEVICE_S: float = 0.15
"""Per-expected-device scale factor used by
``GatewayService.compute_collect_max_timeout`` (seconds).

Status / stream collect-windows compute their max timeout as
``base + expected_count × COLLECT_PER_DEVICE_S``, capped at
``COLLECT_MAX_CEILING_S``.
"""

COLLECT_BASE_S: float = 1.0
"""Fixed base for ``compute_collect_max_timeout`` (seconds).

Covers the gateway settle / network propagation overhead before
the first reply window opens.
"""


# ---------------------------------------------------------------
# Stream (OPC_STREAM, fragmented payload with ACK collection)
# ---------------------------------------------------------------

STREAM_ATTEMPT_TIMEOUT_S: float = 6.0
"""Per-attempt timeout for OPC_STREAM (seconds).

Streams ship fragmented payloads; each attempt opens a window
sized to the expected ACK count via
``compute_collect_max_timeout``. This value is the upper bound
on that window — the dynamic computation may yield less.
"""

STREAM_MAX_ATTEMPTS: int = 3
"""Total attempts (initial + retries) for OPC_STREAM."""

STREAM_RETRY_DELAY_S: float = 0.1
"""Cooldown between stream attempts (seconds)."""
