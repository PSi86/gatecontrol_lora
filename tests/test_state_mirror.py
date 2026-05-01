"""MasterState 1:1 mirror tests (Batch B, 2026-04-28).

Pre-Batch-B the SSE bridge ran a derived state machine that combined
EV_RX_WINDOW_OPEN/CLOSED + EV_TX_DONE + explicit web-layer
``master.set(state="TX", tx_pending=True)`` calls into a "best guess"
pill state. The v4 redesign collapses this into a 1:1 mirror of the
gateway's reported state byte; these tests pin the behaviour:

* Every state byte translates to its canonical string label.
* RX_WINDOW preserves its ``min_ms`` metadata in the snapshot.
* EV_STATE_REPORT updates the mirror identically to EV_STATE_CHANGED.
* Diagnostic events (EV_TX_DONE, EV_TX_REJECTED) update ``last_event``
  but leave the state byte alone — outcome and state are orthogonal.
"""

from __future__ import annotations

import unittest

from racelink.transport.gateway_events import (
    EV_ERROR,
    EV_STATE_CHANGED,
    EV_STATE_REPORT,
    EV_TX_DONE,
    EV_TX_REJECTED,
    GATEWAY_STATE_ERROR,
    GATEWAY_STATE_IDLE,
    GATEWAY_STATE_RX_WINDOW,
    GATEWAY_STATE_TX,
    TX_REJECT_TXPENDING,
)
from racelink.web.sse import MasterState, SSEBridge


def _new_bridge() -> SSEBridge:
    """Return an SSEBridge whose broadcasts are silently swallowed.

    The default bridge would try to fan out to the live SSE client set;
    the tests don't care about the broadcasted payload, only about the
    snapshot side-effect.
    """
    bridge = SSEBridge()
    # Override the broadcast so test code can inspect it without
    # going through the gevent queue. Replace with a no-op to keep tests
    # fast — the snapshot read is the assertion target.
    bridge.broadcast = lambda *a, **kw: None  # type: ignore[assignment]
    bridge.master = MasterState(bridge.broadcast)
    return bridge


class StateMirrorByteToNameTests(unittest.TestCase):

    def test_idle_state_byte(self):
        bridge = _new_bridge()
        bridge.on_transport_event({
            "type": EV_STATE_CHANGED,
            "state_byte": GATEWAY_STATE_IDLE,
            "state_metadata_ms": 0,
        })
        snap = bridge.master.snapshot()
        self.assertEqual(snap["state"], "IDLE")
        self.assertEqual(snap["state_byte"], GATEWAY_STATE_IDLE)
        self.assertEqual(snap["state_metadata_ms"], 0)
        self.assertEqual(snap["last_event"], "STATE_CHANGED")

    def test_tx_state_byte(self):
        bridge = _new_bridge()
        bridge.on_transport_event({
            "type": EV_STATE_CHANGED,
            "state_byte": GATEWAY_STATE_TX,
        })
        self.assertEqual(bridge.master.snapshot()["state"], "TX")

    def test_rx_window_carries_metadata(self):
        bridge = _new_bridge()
        bridge.on_transport_event({
            "type": EV_STATE_CHANGED,
            "state_byte": GATEWAY_STATE_RX_WINDOW,
            "state_metadata_ms": 800,
        })
        snap = bridge.master.snapshot()
        self.assertEqual(snap["state"], "RX_WINDOW")
        self.assertEqual(snap["state_metadata_ms"], 800)

    def test_error_state_byte(self):
        bridge = _new_bridge()
        bridge.on_transport_event({
            "type": EV_STATE_CHANGED,
            "state_byte": GATEWAY_STATE_ERROR,
        })
        self.assertEqual(bridge.master.snapshot()["state"], "ERROR")

    def test_state_report_updates_mirror_same_as_changed(self):
        bridge = _new_bridge()
        bridge.on_transport_event({
            "type": EV_STATE_REPORT,
            "state_byte": GATEWAY_STATE_RX_WINDOW,
            "state_metadata_ms": 250,
        })
        snap = bridge.master.snapshot()
        self.assertEqual(snap["state"], "RX_WINDOW")
        self.assertEqual(snap["state_metadata_ms"], 250)
        self.assertEqual(snap["last_event"], "STATE_REPORT")


class StateMirrorOutcomeOrthogonalityTests(unittest.TestCase):
    """Outcome events (TX_DONE / TX_REJECTED) must not drive the state byte.

    The gateway emits the matching EV_STATE_CHANGED transition near the
    outcome; the host should follow that, not synthesise a state from the
    outcome alone (which is what pre-Batch-B code did).
    """

    def test_tx_done_does_not_change_state_byte(self):
        bridge = _new_bridge()
        bridge.on_transport_event({"type": EV_STATE_CHANGED, "state_byte": GATEWAY_STATE_TX})
        bridge.on_transport_event({"type": EV_TX_DONE, "last_len": 1})
        snap = bridge.master.snapshot()
        # State stays TX until the gateway emits the IDLE transition.
        self.assertEqual(snap["state"], "TX")
        self.assertEqual(snap["last_event"], "TX_DONE")

    def test_tx_rejected_records_diagnostic_only(self):
        bridge = _new_bridge()
        bridge.on_transport_event({"type": EV_STATE_CHANGED, "state_byte": GATEWAY_STATE_IDLE})
        bridge.on_transport_event({
            "type": EV_TX_REJECTED,
            "type_full": 0x08,
            "opc": 0x08,
            "reason": TX_REJECT_TXPENDING,
            "reason_name": "txPending",
        })
        snap = bridge.master.snapshot()
        # State unchanged: gateway is still IDLE post-rejection.
        self.assertEqual(snap["state"], "IDLE")
        self.assertIn("TX_REJECTED", str(snap["last_event"]))

    def test_ev_error_records_last_error_without_state_change(self):
        bridge = _new_bridge()
        bridge.on_transport_event({"type": EV_STATE_CHANGED, "state_byte": GATEWAY_STATE_IDLE})
        bridge.on_transport_event({"type": EV_ERROR, "data": b"USB hiccup"})
        snap = bridge.master.snapshot()
        self.assertEqual(snap["state"], "IDLE")
        self.assertIn("USB hiccup", str(snap["last_error"]))
        self.assertEqual(snap["last_event"], "USB_ERROR")


if __name__ == "__main__":
    unittest.main()
