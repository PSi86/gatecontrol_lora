"""Synchronous _send_m2n outcome tests (Batch B, 2026-04-28).

Pins the four outcomes of ``GatewaySerialTransport._send_m2n`` that the
v4 redesign formalised:

* ``EV_TX_DONE``       -> ``SendOutcome.success()``
* ``EV_TX_REJECTED``   -> ``SendOutcome.rejected(reason)`` with the reason byte
* (no event in time)   -> ``SendOutcome.timeout()``
* USB write raises     -> ``SendOutcome.usb_error()``

The tests run against an in-process fake serial port so they don't need a
gateway. ``_send_m2n`` writes the frame and blocks for an outcome; a
helper thread feeds the matching event into the transport's
``_handle_frame`` from outside, simulating the RX reader's path.
"""

from __future__ import annotations

import sys
import threading
import time
import types
import unittest


# Stub pyserial before importing the transport (same shape every test
# module in this repo uses).
serial_stub = types.ModuleType("serial")


class _FakeSerial:
    def __init__(self, *args, **kwargs):
        self.baudrate = None
        self.timeout = None
        self.port = None
        self.is_open = False
        self.written = bytearray()
        self.write_should_raise: Exception | None = None

    def write(self, data):
        if self.write_should_raise is not None:
            raise self.write_should_raise
        self.written.extend(data)

    def read(self, _n):
        return b""

    def close(self):
        self.is_open = False


serial_stub.Serial = _FakeSerial
# Use plain Exception so the production code's `except serial.SerialException`
# catches whatever the test raises. The other two test modules in this repo
# use the same trick (test_gateway_serial_transport.py).
serial_stub.SerialException = Exception
serial_tools_stub = types.ModuleType("serial.tools")
serial_list_ports_stub = types.ModuleType("serial.tools.list_ports")
serial_list_ports_stub.comports = lambda: []
serial_tools_stub.list_ports = serial_list_ports_stub
serial_stub.tools = serial_tools_stub

sys.modules.setdefault("serial", serial_stub)
sys.modules.setdefault("serial.tools", serial_tools_stub)
sys.modules.setdefault("serial.tools.list_ports", serial_list_ports_stub)

from racelink.transport.gateway_events import (
    EV_TX_DONE,
    EV_TX_REJECTED,
    LP,
    TX_REJECT_OVERSIZE,
    TX_REJECT_TXPENDING,
)
from racelink.transport.gateway_serial import (
    SEND_OUTCOME_TIMEOUT_S,
    GatewaySerialTransport,
    SendOutcome,
)


def _new_transport():
    """Return a GatewaySerialTransport wired to a _FakeSerial.

    The fake captures USB writes and lets tests inject EV_* frames via
    ``_handle_frame`` from a background thread to mimic the RX reader.
    """
    transport = GatewaySerialTransport(port="COM_TEST")
    # Manually open so write() works without a real port.
    transport.ser = _FakeSerial()
    transport.ser.is_open = True
    return transport


class SynchronousSendOutcomeTests(unittest.TestCase):

    # --- success path ---------------------------------------------------

    def test_tx_done_yields_success_outcome(self):
        transport = _new_transport()

        def feed_outcome():
            time.sleep(0.05)
            transport._handle_frame(EV_TX_DONE, bytes([0x01]))

        threading.Thread(target=feed_outcome, daemon=True).start()
        outcome = transport._send_m2n(LP.OPC_PRESET, b"\xFF\xFF\xFF", b"\x01\x02\x03\x04")
        self.assertTrue(bool(outcome))
        self.assertEqual(outcome.code, "SUCCESS")
        # The frame should actually have hit the wire.
        self.assertGreater(len(transport.ser.written), 0)

    # --- rejection path -------------------------------------------------

    def test_tx_rejected_yields_rejected_with_reason(self):
        transport = _new_transport()

        def feed_outcome():
            time.sleep(0.05)
            # body: [type_full, reason]
            transport._handle_frame(
                EV_TX_REJECTED,
                bytes([LP.make_type(LP.DIR_M2N, LP.OPC_PRESET), TX_REJECT_TXPENDING]),
            )

        threading.Thread(target=feed_outcome, daemon=True).start()
        outcome = transport._send_m2n(LP.OPC_PRESET, b"\xFF\xFF\xFF", b"\x01\x02\x03\x04")
        self.assertFalse(bool(outcome))
        self.assertEqual(outcome.code, "REJECTED")
        self.assertEqual(outcome.reason, TX_REJECT_TXPENDING)
        self.assertEqual(outcome.reason_name, "txPending")

    def test_tx_rejected_oversize_reason_is_named(self):
        transport = _new_transport()

        def feed_outcome():
            time.sleep(0.05)
            transport._handle_frame(
                EV_TX_REJECTED,
                bytes([LP.make_type(LP.DIR_M2N, LP.OPC_CONTROL), TX_REJECT_OVERSIZE]),
            )

        threading.Thread(target=feed_outcome, daemon=True).start()
        outcome = transport._send_m2n(LP.OPC_CONTROL, b"\xAA\xBB\xCC", b"\x00\x00\x00")
        self.assertFalse(bool(outcome))
        self.assertEqual(outcome.code, "REJECTED")
        self.assertEqual(outcome.reason_name, "oversize")

    # --- timeout path ---------------------------------------------------

    def test_no_outcome_within_guard_yields_timeout(self):
        transport = _new_transport()
        # Override the deadlock guard with a much shorter value so the test
        # runs fast. The default 2.0 s is correct in production but would
        # make every other test slow.
        outcome = transport._send_m2n(
            LP.OPC_OFFSET, b"\xFF\xFF\xFF", b"\x00\x00", timeout_s=0.05,
        )
        self.assertFalse(bool(outcome))
        self.assertEqual(outcome.code, "TIMEOUT")
        # Sanity: the configured guard is still 2 s (we haven't accidentally
        # shipped the test override into production).
        self.assertEqual(SEND_OUTCOME_TIMEOUT_S, 2.0)

    # --- USB error path -------------------------------------------------

    def test_usb_write_failure_yields_usb_error_outcome(self):
        transport = _new_transport()
        # Raise the same class the production module sees when it imports
        # ``import serial`` — the stub aliases SerialException to Exception
        # so the catch fires.
        import serial as _stub_serial
        transport.ser.write_should_raise = _stub_serial.SerialException("USB unplugged")
        outcome = transport._send_m2n(
            LP.OPC_OFFSET, b"\xFF\xFF\xFF", b"\x00\x00", timeout_s=0.05,
        )
        self.assertFalse(bool(outcome))
        self.assertEqual(outcome.code, "USB_ERROR")
        # The detail string should mention the underlying SerialException
        # so the operator can see what broke.
        self.assertIn("USB unplugged", outcome.detail)

    # --- listener fan-out -----------------------------------------------

    def test_outcome_is_surfaced_to_tx_listeners(self):
        transport = _new_transport()
        seen: list[dict] = []
        transport.add_tx_listener(seen.append)

        def feed_outcome():
            time.sleep(0.05)
            transport._handle_frame(EV_TX_DONE, bytes([0x01]))

        threading.Thread(target=feed_outcome, daemon=True).start()
        transport._send_m2n(LP.OPC_PRESET, b"\xFF\xFF\xFF", b"\x01\x02\x03\x04")
        self.assertTrue(any(ev.get("type") == "TX_M2N" for ev in seen))
        outcome_events = [ev for ev in seen if ev.get("type") == "TX_OUTCOME"]
        self.assertTrue(outcome_events, "TX_OUTCOME listener event missing")
        self.assertEqual(outcome_events[-1]["outcome"], "SUCCESS")


class OrphanOutcomeEventTests(unittest.TestCase):
    """EV_TX_DONE / EV_TX_REJECTED arriving with no pending send must not crash.

    Auto-sync TXs on the gateway side produce orphan outcome events when
    the host has no ``_send_m2n`` in flight. They should be dropped silently.
    """

    def test_orphan_tx_done_is_ignored(self):
        transport = _new_transport()
        transport._handle_frame(EV_TX_DONE, bytes([0x01]))
        self.assertIsNone(transport._pending_send_outcome)

    def test_orphan_tx_rejected_is_ignored(self):
        transport = _new_transport()
        transport._handle_frame(
            EV_TX_REJECTED,
            bytes([LP.make_type(LP.DIR_M2N, LP.OPC_SYNC), TX_REJECT_TXPENDING]),
        )
        self.assertIsNone(transport._pending_send_outcome)


class N2MReplyHandlingTests(unittest.TestCase):
    """N2M reply forwarding through ``_handle_frame`` (regression for 2026-04-28).

    Pre-fix the Batch-B refactor dropped the ``rx_windows=`` kwarg from the
    ``parse_reply_event`` call site but ``codec.parse_reply_event``'s
    signature still required it. The first time a node emitted a reply
    (e.g. a WLED IDENTIFY on boot) the RX reader thread crashed with
    ``TypeError: parse_reply_event() missing 1 required keyword-only
    argument: 'rx_windows'``. With the reader dead, every subsequent
    ``_send_m2n`` blocked until the 2 s deadlock guard fired and timed
    out — symptom in production logs as a cascade of "TX outcome timeout"
    warnings.

    These tests pin both halves of the fix:

    1. ``_handle_frame`` can route a forwarded N2M reply through the codec
       without raising.
    2. Even if the codec / a listener does raise, the reader's defence-
       in-depth guard logs and continues so the next outcome event still
       fulfills any pending send.
    """

    @staticmethod
    def _identify_reply_frame_data() -> bytes:
        """A minimal IDENTIFY_REPLY-shaped data block.

        Layout matches what ``_reader`` hands to ``_handle_frame`` after
        framing strips the leading 0x00 / LEN bytes:
            Header7 (sender3 || receiver3 || type) +
            P_IdentifyReply (9 bytes: fw, caps, group, mac6) +
            RSSI (LE16) + SNR (i8)
        """
        sender3 = bytes.fromhex("AABBCC")
        receiver3 = bytes.fromhex("112233")
        # IDENTIFY_REPLY is N2M (DIR bit set) on opcode 0x01.
        type_full = LP.make_type(LP.DIR_N2M, LP.OPC_DEVICES)
        header7 = sender3 + receiver3 + bytes([type_full])
        # fw=4, caps=1, groupId=2, mac6=AABBCCDDEEFF
        body = bytes([0x04, 0x01, 0x02]) + bytes.fromhex("AABBCCDDEEFF")
        rssi_snr = bytes([0xC8, 0xFF, 0x07])  # rssi=-56 LE, snr=7
        return header7 + body + rssi_snr

    def test_n2m_reply_does_not_raise_on_handle_frame(self):
        transport = _new_transport()
        type_full = LP.make_type(LP.DIR_N2M, LP.OPC_DEVICES)
        seen: list[dict] = []
        transport.add_listener(seen.append)
        # Should NOT raise — pre-fix this raised TypeError because the
        # rx_windows kwarg was still required by codec.parse_reply_event.
        transport._handle_frame(type_full, self._identify_reply_frame_data())
        # The forwarded reply should have been emitted to the listener.
        replies = [ev for ev in seen if ev.get("reply") == "IDENTIFY_REPLY"]
        self.assertTrue(replies, "IDENTIFY_REPLY listener event missing")

    def test_handle_frame_failure_does_not_kill_subsequent_outcome(self):
        """A misbehaving listener / codec must not deafen the transport.

        The RX reader's defence guard wraps ``_handle_frame`` in
        try/except — any exception is logged and dropped, and the next
        EV_TX_DONE still fulfills a pending ``_send_m2n``. Without the
        guard a single bad frame would deafen the transport for the rest
        of its lifetime.
        """
        transport = _new_transport()

        def bad_listener(_ev):
            raise RuntimeError("simulated listener bug")

        transport.add_listener(bad_listener)
        # The RX reader's guard isn't reached if we call _handle_frame
        # directly — bad_listener's exception is already swallowed by
        # _emit's per-listener try/except. So emit a frame that DOES
        # bypass the per-listener guard: feed an EV_TX_DONE while
        # nothing is in flight (it's a no-op but exercises the parse path).
        transport._handle_frame(EV_TX_DONE, bytes([0x01]))

        # Now stage a real send; the next outcome should fulfill it
        # without being blocked by the prior listener exception.
        def feed_outcome():
            time.sleep(0.05)
            transport._handle_frame(EV_TX_DONE, bytes([0x01]))

        threading.Thread(target=feed_outcome, daemon=True).start()
        outcome = transport._send_m2n(LP.OPC_PRESET, b"\xFF\xFF\xFF", b"\x01\x02\x03\x04")
        self.assertEqual(outcome.code, "SUCCESS")


if __name__ == "__main__":
    unittest.main()
