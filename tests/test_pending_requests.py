"""Tests for the Host-side request/reply registry (plan Transport Redesign B)."""

from __future__ import annotations

import threading
import time
import unittest

from racelink.services.pending_requests import (
    RESP_ACK,
    RESP_SPECIFIC,
    PendingRequestRegistry,
)
from racelink.transport import LP


class PendingRequestRegistryTests(unittest.TestCase):
    def test_ack_match_completes_request(self):
        reg = PendingRequestRegistry()
        sender = bytes.fromhex("DDEEFF")
        req = reg.register(
            sender_last3=sender,
            expected_key=int(LP.OPC_SET_GROUP),
            policy=RESP_ACK,
            timeout_s=1.0,
        )
        matched = reg.try_match(
            {
                "opc": LP.OPC_ACK,
                "ack_of": int(LP.OPC_SET_GROUP),
                "ack_status": 0,
                "sender3": sender,
            }
        )
        self.assertIs(matched, req)
        self.assertTrue(req.done.is_set())
        self.assertIsNotNone(req.reply)

    def test_different_sender_does_not_match(self):
        reg = PendingRequestRegistry()
        req = reg.register(
            sender_last3=bytes.fromhex("AAAAAA"),
            expected_key=int(LP.OPC_SET_GROUP),
            policy=RESP_ACK,
            timeout_s=1.0,
        )
        matched = reg.try_match(
            {
                "opc": LP.OPC_ACK,
                "ack_of": int(LP.OPC_SET_GROUP),
                "ack_status": 0,
                "sender3": bytes.fromhex("BBBBBB"),
            }
        )
        self.assertIsNone(matched)
        self.assertFalse(req.done.is_set())

    def test_wrong_ack_of_does_not_match(self):
        reg = PendingRequestRegistry()
        sender = bytes.fromhex("DDEEFF")
        req = reg.register(
            sender_last3=sender,
            expected_key=int(LP.OPC_SET_GROUP),
            policy=RESP_ACK,
            timeout_s=1.0,
        )
        matched = reg.try_match(
            {
                "opc": LP.OPC_ACK,
                "ack_of": int(LP.OPC_CONFIG),  # ACK for something else
                "ack_status": 0,
                "sender3": sender,
            }
        )
        self.assertIsNone(matched)
        self.assertFalse(req.done.is_set())

    def test_specific_reply_matches_on_opcode(self):
        reg = PendingRequestRegistry()
        sender = bytes.fromhex("DDEEFF")
        req = reg.register(
            sender_last3=sender,
            expected_key=int(LP.OPC_STATUS),
            policy=RESP_SPECIFIC,
            timeout_s=1.0,
        )
        matched = reg.try_match(
            {
                "opc": LP.OPC_STATUS,
                "reply": "STATUS_REPLY",
                "sender3": sender,
            }
        )
        self.assertIs(matched, req)
        self.assertTrue(req.done.is_set())

    def test_cancel_is_idempotent(self):
        reg = PendingRequestRegistry()
        req = reg.register(
            sender_last3=bytes.fromhex("DDEEFF"),
            expected_key=int(LP.OPC_SET_GROUP),
            policy=RESP_ACK,
            timeout_s=1.0,
        )
        reg.cancel(req)
        reg.cancel(req)  # must not raise
        self.assertEqual(reg.pending_count(), 0)

    def test_multiple_waiters_each_complete_independently(self):
        reg = PendingRequestRegistry()
        r1 = reg.register(
            sender_last3=bytes.fromhex("111111"),
            expected_key=int(LP.OPC_SET_GROUP),
            policy=RESP_ACK,
            timeout_s=1.0,
        )
        r2 = reg.register(
            sender_last3=bytes.fromhex("222222"),
            expected_key=int(LP.OPC_SET_GROUP),
            policy=RESP_ACK,
            timeout_s=1.0,
        )
        reg.try_match(
            {
                "opc": LP.OPC_ACK,
                "ack_of": int(LP.OPC_SET_GROUP),
                "ack_status": 0,
                "sender3": bytes.fromhex("222222"),
            }
        )
        self.assertFalse(r1.done.is_set())
        self.assertTrue(r2.done.is_set())

    def test_registry_dispatch_unblocks_waiter_from_other_thread(self):
        """End-to-end: waiter blocks on done, dispatcher sets it in <10 ms."""
        reg = PendingRequestRegistry()
        sender = bytes.fromhex("DDEEFF")
        req = reg.register(
            sender_last3=sender,
            expected_key=int(LP.OPC_SET_GROUP),
            policy=RESP_ACK,
            timeout_s=2.0,
        )

        def dispatcher():
            time.sleep(0.02)
            reg.try_match(
                {
                    "opc": LP.OPC_ACK,
                    "ack_of": int(LP.OPC_SET_GROUP),
                    "ack_status": 0,
                    "sender3": sender,
                }
            )

        t = threading.Thread(target=dispatcher)
        t.start()
        t0 = time.monotonic()
        completed = req.done.wait(timeout=1.0)
        elapsed = time.monotonic() - t0
        t.join()
        self.assertTrue(completed)
        self.assertLess(elapsed, 0.2)


if __name__ == "__main__":
    unittest.main()
