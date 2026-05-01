"""Retired (Batch B, 2026-04-28).

Pre-Batch-B the host's ``_send_m2n`` used a body-length-scaled TX barrier
(``TX_BARRIER_FLOOR_S`` + ``_tx_barrier_timeout_for``) to wait between
writes for the gateway's previous ``EV_TX_DONE``. The hazard was the
gateway's no-NACK silent-drop on ``txPending``; the barrier's role was
to make the wait long enough that the gateway always finished first.

Batch B replaced this entirely:

* The gateway firmware now wraps every ``scheduleSend`` in
  ``try_schedule_or_nack`` and emits ``EV_TX_REJECTED(type, reason)`` on
  rejection.
* The host's ``_send_m2n`` is synchronous and waits for the matching
  outcome event (``EV_TX_DONE`` -> ``SUCCESS`` /
  ``EV_TX_REJECTED`` -> ``REJECTED(reason)``) bounded by a single 2 s
  deadlock guard.
* Pipelining isn't supported; one frame in flight at a time enforced by
  the ``_tx_lock``.

The barrier-pinning tests in this module no longer have anything to pin
— the constants and helper they referenced were deleted from
``gateway_serial.py``. The new contract is exercised by
``tests/test_synchronous_send.py`` (outcome cases), ``tests/test_state_query.py``
(STATE_REQUEST round-trip) and ``tests/test_state_mirror.py`` (state byte
to pill state).

This file is kept as a tombstone so ``git log -- tests/test_transport_tx_barrier.py``
keeps showing the historical context for anyone investigating barrier
behaviour later.
"""

# Intentionally no test classes — the contract this file used to pin no
# longer exists. See module docstring above.
