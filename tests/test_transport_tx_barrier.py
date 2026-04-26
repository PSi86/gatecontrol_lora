"""Tests for the TX flow-control barrier in :class:`GatewaySerialTransport`.

The barrier serialises USB writes against the gateway's ``EV_TX_DONE`` /
``EV_ERROR`` events so the host stops flooding the radio with packets it
cannot drain in time. See ``der-offset-mode-in-szenen-serialized-quilt.md``,
Item 4 — the bug surfaced under rapid scene-action fan-out (many
``OPC_OFFSET`` + ``OPC_CONTROL`` packets back-to-back).
"""

from __future__ import annotations

import logging
import threading
import time
import unittest
from unittest.mock import MagicMock

from racelink.transport import gateway_serial
from racelink.transport.gateway_events import EV_ERROR, EV_TX_DONE
from racelink.transport.gateway_serial import GatewaySerialTransport


def _make_transport() -> GatewaySerialTransport:
    """Construct a transport with the serial port replaced by a MagicMock.

    No port is opened; ``_send_m2n`` writes directly into the mock's
    ``write`` method, and tests drive ``EV_TX_DONE`` / ``EV_ERROR`` by
    invoking ``_handle_frame`` manually.
    """
    t = GatewaySerialTransport(port=None)
    t.ser = MagicMock()
    t.ser.is_open = True
    return t


class TxBarrierTests(unittest.TestCase):
    def test_first_send_does_not_block(self):
        """The barrier starts in the idle (set) state so the first send goes out
        immediately, no matter how long the test takes to set up."""
        t = _make_transport()
        before = time.monotonic()
        ok = t._send_m2n(0x09, b"\xFF\xFF\xFF", b"\x01\x02\x03")
        elapsed = time.monotonic() - before
        self.assertTrue(ok)
        self.assertLess(elapsed, 0.05, "first send should be near-instant")
        t.ser.write.assert_called_once()

    def test_second_send_blocks_until_ev_tx_done(self):
        """Without a TX_DONE event between two sends, the second send blocks
        until the event arrives."""
        t = _make_transport()
        # First send marks the barrier as "in flight".
        self.assertTrue(t._send_m2n(0x09, b"\xFF\xFF\xFF", b"\x01\x02\x03"))
        self.assertTrue(t._tx_in_flight,
                        "first send should leave the barrier in-flight until EV_TX_DONE")

        # Second send issued from a worker thread; it should block on the barrier
        # until we manually fire EV_TX_DONE on the reader path.
        completed = threading.Event()

        def producer():
            t._send_m2n(0x09, b"\xFF\xFF\xFF", b"\x04\x05\x06")
            completed.set()

        worker = threading.Thread(target=producer, daemon=True)
        worker.start()

        # Give the worker a moment to enter wait().
        time.sleep(0.02)
        self.assertFalse(completed.is_set(), "second send must block before EV_TX_DONE")
        self.assertEqual(t.ser.write.call_count, 1, "no second write yet")

        # Release the barrier exactly the way the gateway does: an EV_TX_DONE
        # frame coming up the reader path.
        t._handle_frame(EV_TX_DONE, bytes([28]))   # data[0] = last_len, value irrelevant

        # Worker should now finish promptly.
        self.assertTrue(completed.wait(timeout=1.0), "second send did not unblock after EV_TX_DONE")
        self.assertEqual(t.ser.write.call_count, 2)

    def test_send_proceeds_after_timeout_when_tx_done_lost(self):
        """If the gateway never sends EV_TX_DONE, the barrier times out and
        the next send proceeds anyway. A WARNING is logged for diagnosability."""
        t = _make_transport()
        # First send clears the event.
        self.assertTrue(t._send_m2n(0x09, b"\xFF\xFF\xFF", b"\x01\x02\x03"))

        # Shrink the timeout helper so the test runs fast — the production
        # path computes a per-call value sized to the previous body length;
        # we override the helper module-globally for the duration of this
        # test only.
        original = gateway_serial._tx_barrier_timeout_for
        gateway_serial._tx_barrier_timeout_for = lambda _body_len: 0.05
        try:
            with self.assertLogs("racelink_transport", level="WARNING") as captured:
                before = time.monotonic()
                ok = t._send_m2n(0x09, b"\xFF\xFF\xFF", b"\x04\x05\x06")
                elapsed = time.monotonic() - before
        finally:
            gateway_serial._tx_barrier_timeout_for = original

        self.assertTrue(ok)
        # We waited the full timeout, then proceeded.
        self.assertGreaterEqual(elapsed, 0.05)
        self.assertLess(elapsed, 0.5, "timeout should be bounded")
        self.assertEqual(t.ser.write.call_count, 2)
        joined = "\n".join(captured.output)
        self.assertIn("TX barrier timeout", joined)

    def test_dynamic_timeout_scales_with_previous_body_length(self):
        """The barrier wait timeout is sized to the *previous* packet's body
        length so larger payloads (e.g. OPC_STREAM 9-byte chunks queued in
        bursts) get more headroom than tiny OPC_PRESET frames."""
        # Floor covers small bodies. Above the floor, each byte adds the
        # configured per-byte airtime.
        self.assertEqual(
            gateway_serial._tx_barrier_timeout_for(0),
            gateway_serial.TX_BARRIER_FLOOR_S,
        )
        # A 100-byte body should exceed the floor.
        big = gateway_serial._tx_barrier_timeout_for(100)
        self.assertGreater(big, gateway_serial.TX_BARRIER_FLOOR_S)
        # Linear scaling above the floor.
        small = gateway_serial._tx_barrier_timeout_for(50)
        diff = (big - small)
        expected = 50 * gateway_serial.TX_BARRIER_PER_BYTE_S
        self.assertAlmostEqual(diff, expected, places=6)

    def test_concurrent_senders_serialize_against_each_other(self):
        """A1: ``_send_m2n`` must serialize concurrent callers so frames
        cannot interleave on the USB byte stream. Without the TX lock,
        thread B's ``ser.write`` could fire between thread A's frame
        bytes if both raced past the (Event-based) barrier together."""
        t = _make_transport()

        # Make every write slow enough to force any racing writer to be
        # observable. Each write also synthesizes the corresponding
        # EV_TX_DONE so the next caller's wait_for unblocks promptly —
        # we want to test the serialization, not the barrier's timeout.
        write_log: list[float] = []
        write_completed: list[float] = []

        def slow_write(_frame):
            write_log.append(time.monotonic())
            time.sleep(0.02)
            write_completed.append(time.monotonic())
            # Simulate the gateway acknowledging the TX so the predicate
            # flips back to "idle" without us having to call
            # ``_handle_frame`` from outside the locked region.
            threading.Thread(
                target=lambda: t._handle_frame(EV_TX_DONE, bytes([0])),
                daemon=True,
            ).start()

        t.ser.write.side_effect = slow_write

        N = 5
        threads = [
            threading.Thread(
                target=lambda i=i: t._send_m2n(
                    0x09, b"\xFF\xFF\xFF", bytes([i, i, i])
                ),
                daemon=True,
            )
            for i in range(N)
        ]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=2.0)
            self.assertFalse(th.is_alive(), "sender thread did not finish")

        self.assertEqual(t.ser.write.call_count, N)
        # Strict serialization: each write must complete before the next
        # write starts. Pairwise check on the timestamps confirms no
        # interleaving could have happened in ``ser.write``.
        for i in range(1, N):
            self.assertGreaterEqual(
                write_log[i],
                write_completed[i - 1],
                f"write {i} started before write {i-1} finished — frames could interleave",
            )

    def test_ev_error_releases_barrier(self):
        """An EV_ERROR frame after a send releases the barrier so we don't
        deadlock when the gateway reports a TX failure."""
        t = _make_transport()
        self.assertTrue(t._send_m2n(0x09, b"\xFF\xFF\xFF", b"\x01\x02\x03"))
        self.assertTrue(t._tx_in_flight)

        # Inject an EV_ERROR (any payload).
        t._handle_frame(EV_ERROR, b"\xDE\xAD")

        self.assertFalse(t._tx_in_flight,
                         "EV_ERROR must release the TX barrier to avoid deadlock")

        # And a follow-up send proceeds promptly.
        before = time.monotonic()
        ok = t._send_m2n(0x09, b"\xFF\xFF\xFF", b"\x04\x05\x06")
        elapsed = time.monotonic() - before
        self.assertTrue(ok)
        self.assertLess(elapsed, 0.05)


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.DEBUG)
    unittest.main()
