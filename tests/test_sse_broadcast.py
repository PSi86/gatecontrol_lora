"""SSE broadcast lock-discipline regression tests (A4).

The bug class: ``broadcast()`` used to hold ``_clients_lock`` for the
entire fan-out, including the per-client ``q.put(timeout=0.01)`` calls.
A disconnected-but-not-yet-cleaned-up client (or a slow consumer) would
therefore stall every other broadcaster *and* every new SSE client
registration for up to 10 ms per dead client. With many dead clients
that compounds into UI-visible starvation.

These tests pin the new invariant: the lock is released before any
queue interaction, and the lock is only re-acquired briefly to remove
dead clients.
"""

from __future__ import annotations

import threading
import time
import unittest

from racelink.web.sse import SSEBridge


class SlowQueue:
    """Queue stub whose ``put_nowait`` records the lock state at call
    time. Tests inspect the recorded states to verify the lock was
    released before the put."""

    def __init__(self, lock_under_test, *, sleep_s: float = 0.0):
        self._lock = lock_under_test
        self._sleep = sleep_s
        self.received: list[tuple] = []
        self.lock_held_during_put: list[bool] = []

    def put_nowait(self, item):
        # ``threading.Lock.locked()`` is the cheapest way to check
        # without disturbing acquisition order. The invariant we want:
        # this method is never called while the broadcast holds the
        # client-set lock.
        self.lock_held_during_put.append(self._lock.locked())
        if self._sleep:
            time.sleep(self._sleep)
        self.received.append(item)


class FailingQueue:
    """Queue stub whose ``put_nowait`` always raises — used to exercise
    the dead-client cleanup path."""

    def put_nowait(self, item):
        raise RuntimeError("simulated dead client")


class BroadcastLockDisciplineTests(unittest.TestCase):

    def _make_bridge(self) -> SSEBridge:
        bridge = SSEBridge(logger=None)
        # Replace the gevent-flavoured default lock with a plain
        # ``threading.Lock`` so we can introspect ``.locked()``. The
        # broadcast() implementation only relies on context-manager
        # behaviour, which both flavours provide.
        bridge._clients_lock = threading.Lock()
        return bridge

    def test_put_nowait_runs_outside_clients_lock(self):
        """A4 invariant: per-client puts happen with the clients lock
        released. Without this the next broadcaster / new SSE
        registration would block behind a slow consumer."""
        bridge = self._make_bridge()
        clients = [SlowQueue(bridge._clients_lock) for _ in range(5)]
        bridge._clients = set(clients)

        bridge.broadcast("test", {"k": "v"})

        # Every queue saw the put — and none of them saw the lock held.
        for q in clients:
            self.assertEqual(len(q.lock_held_during_put), 1)
            self.assertFalse(
                any(q.lock_held_during_put),
                "broadcast must release _clients_lock before put_nowait",
            )
            self.assertEqual(q.received, [("test", {"k": "v"})])

    def test_concurrent_register_does_not_block_during_slow_fan_out(self):
        """Even if a slow consumer takes 50 ms inside its put_nowait,
        a concurrent SSE registration must complete promptly because
        the broadcast no longer holds the lock during fan-out."""
        bridge = self._make_bridge()
        bridge._clients = {SlowQueue(bridge._clients_lock, sleep_s=0.05) for _ in range(3)}

        register_completed_at: list[float] = []
        broadcast_completed_at: list[float] = []
        start_event = threading.Event()

        def broadcaster():
            start_event.wait()
            bridge.broadcast("test", {"k": "v"})
            broadcast_completed_at.append(time.monotonic())

        def registerer():
            start_event.wait()
            # Give the broadcaster a head start so we register
            # *during* its fan-out.
            time.sleep(0.005)
            with bridge._clients_lock:
                bridge._clients.add(SlowQueue(bridge._clients_lock))
            register_completed_at.append(time.monotonic())

        tb = threading.Thread(target=broadcaster, daemon=True)
        tr = threading.Thread(target=registerer, daemon=True)
        tb.start(); tr.start()
        t0 = time.monotonic()
        start_event.set()
        tb.join(timeout=5.0); tr.join(timeout=5.0)

        self.assertFalse(tb.is_alive())
        self.assertFalse(tr.is_alive())
        self.assertEqual(len(register_completed_at), 1)
        self.assertEqual(len(broadcast_completed_at), 1)

        # Registration finished well before the broadcast (which is
        # still spinning through its 3 × 50 ms slow puts). With the
        # old lock-held-during-put discipline, registration had to
        # wait for the full fan-out (~150 ms) before it could acquire
        # the lock.
        register_elapsed = register_completed_at[0] - t0
        broadcast_elapsed = broadcast_completed_at[0] - t0
        self.assertLess(register_elapsed, 0.05,
                        f"register took {register_elapsed*1000:.1f} ms — likely "
                        f"still blocked on the broadcaster's lock")
        self.assertGreater(broadcast_elapsed, 0.10,
                           "broadcast should still be fanning out — "
                           "test premise is invalid otherwise")

    def test_dead_clients_are_removed_from_the_set(self):
        """Cleanup invariant: a queue that raises in ``put_nowait`` is
        removed from ``_clients`` after the fan-out."""
        bridge = self._make_bridge()
        good = SlowQueue(bridge._clients_lock)
        bad = FailingQueue()
        bridge._clients = {good, bad}

        bridge.broadcast("evt", {"x": 1})

        # Good client kept; bad client dropped.
        self.assertIn(good, bridge._clients)
        self.assertNotIn(bad, bridge._clients)
        self.assertEqual(good.received, [("evt", {"x": 1})])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
