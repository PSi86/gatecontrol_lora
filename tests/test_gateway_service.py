import unittest

from racelink.domain import RL_Device
from racelink.services.gateway_service import GatewayService
from racelink.state.repository import DeviceRepository, GroupRepository
from racelink.transport import EV_RX_WINDOW_CLOSED, LP


class FakeTransport:
    def __init__(self):
        self.listeners = []
        self.tx_listeners = []
        self.sent_config = []
        self.sent_stream = []
        self.sent_set_group = []

    def add_listener(self, cb):
        self.listeners.append(cb)

    def remove_listener(self, cb):
        if cb in self.listeners:
            self.listeners.remove(cb)

    def add_tx_listener(self, cb):
        self.tx_listeners.append(cb)

    def send_config(self, **kwargs):
        self.sent_config.append(kwargs)

    def send_stream(self, **kwargs):
        self.sent_stream.append(kwargs)

    def send_set_group(self, recv3, group_id):
        self.sent_set_group.append({"recv3": recv3, "group_id": group_id})

    def emit(self, ev):
        for cb in list(self.listeners):
            cb(ev)

    def drain_events(self, timeout_s=0.0):
        return []


class FakeController:
    def __init__(self):
        self.dev = RL_Device("AABBCCDDEEFF", 1, "Node")
        self.transport = FakeTransport()
        self._pending_expect = None
        self._pending_config = {}
        self._transport_hooks_installed = False
        self.applied = []
        self.group_assignments = []
        self._device_repository = DeviceRepository([self.dev])
        self._group_repository = GroupRepository([object(), object(), object(), object()])
        self.discovery_active = False

    def _to_hex_str(self, value):
        if isinstance(value, (bytes, bytearray)):
            return bytes(value).hex().upper()
        return str(value or "").upper()

    def getDeviceFromAddress(self, addr):
        addr = str(addr or "").upper()
        if addr in {self.dev.addr, self.dev.addr[-6:]}:
            return self.dev
        return None

    def _apply_config_update(self, dev, option, data0):
        self.applied.append((dev, option, data0))

    @property
    def device_repository(self):
        return self._device_repository

    @property
    def group_repository(self):
        return self._group_repository

    def setNodeGroupId(self, dev, forceSet: bool = False, wait_for_ack: bool = True) -> bool:
        self.group_assignments.append((dev.addr, dev.groupId, forceSet, wait_for_ack))
        return True  # simulate ACK ok so the async worker exits cleanly

    def is_discovery_active(self):
        return bool(self.discovery_active)


class GatewayServiceTests(unittest.TestCase):
    def test_send_and_wait_for_ack_marks_device_online(self):
        controller = FakeController()
        service = GatewayService(controller)

        def send_fn():
            controller.transport.emit(
                {
                    "opc": LP.OPC_ACK,
                    "ack_of": LP.OPC_CONFIG,
                    "ack_status": 0,
                    "sender3": bytes.fromhex("DDEEFF"),
                }
            )
            controller.transport.emit({"type": EV_RX_WINDOW_CLOSED})

        events, got_closed = service.send_and_wait_for_reply(bytes.fromhex("DDEEFF"), LP.OPC_CONFIG, send_fn)

        self.assertTrue(got_closed)
        self.assertEqual(len(events), 1)
        self.assertTrue(controller.dev.link_online)

    def test_send_and_wait_short_circuits_when_window_does_not_close(self):
        """Regression: unicast ACK arrives but gateway never emits WINDOW_CLOSED.

        Before the ``stop_on_match`` fix this call would block the full
        ``timeout_s`` even though the expected reply arrived immediately,
        producing a bogus "No ACK_OK ..." warning in production. The call must
        now return in <1 s when the match is found.
        """
        import time

        controller = FakeController()
        service = GatewayService(controller)

        def send_fn():
            # Simulate the node ACK landing ~20 ms after TX -- but never
            # emit EV_RX_WINDOW_CLOSED.
            controller.transport.emit(
                {
                    "opc": LP.OPC_ACK,
                    "ack_of": LP.OPC_SET_GROUP,
                    "ack_status": 0,
                    "sender3": bytes.fromhex("DDEEFF"),
                }
            )

        t0 = time.monotonic()
        events, completed = service.send_and_wait_for_reply(
            bytes.fromhex("DDEEFF"), LP.OPC_SET_GROUP, send_fn, timeout_s=5.0
        )
        elapsed = time.monotonic() - t0

        self.assertEqual(len(events), 1)
        # Phase B: the second tuple element now means "the wait completed",
        # not "the gateway closed its RX window". Registry-based match always
        # returns True on success.
        self.assertTrue(completed)
        self.assertLess(elapsed, 1.0, f"short-circuit took {elapsed:.3f}s")
        self.assertTrue(controller.dev.link_online)

    def test_handle_ack_applies_pending_config(self):
        controller = FakeController()
        service = GatewayService(controller)
        controller._pending_config["DDEEFF"] = {"option": 0x04, "data0": 1}

        service.handle_ack_event(
            {
                "sender3": bytes.fromhex("DDEEFF"),
                "ack_of": LP.OPC_CONFIG,
                "ack_status": 0,
                "ack_seq": 7,
                "host_rssi": -40,
                "host_snr": 6,
            }
        )

        self.assertTrue(controller.dev.ack_ok())
        self.assertEqual(controller.applied, [(controller.dev, 0x04, 1)])

    def test_pending_window_closed_marks_device_offline(self):
        controller = FakeController()
        service = GatewayService(controller)
        controller._pending_expect = {
            "dev": controller.dev,
            "rule": type("Rule", (), {"name": "STATUS"})(),
            "opcode7": LP.OPC_STATUS,
            "sender_last3": "DDEEFF",
        }

        service.pending_window_closed({"type": EV_RX_WINDOW_CLOSED})

        self.assertFalse(controller.dev.link_online)
        self.assertEqual(controller.dev.link_error, "Missing reply (STATUS)")
        self.assertIsNone(controller._pending_expect)

    def test_send_stream_passes_raw_payload_to_transport(self):
        controller = FakeController()
        service = GatewayService(controller)

        def emit_ack_and_close():
            controller.transport.emit(
                {
                    "opc": LP.OPC_ACK,
                    "ack_of": LP.OPC_STREAM,
                    "ack_status": 0,
                    "sender3": bytes.fromhex("DDEEFF"),
                }
            )
            controller.transport.emit({"type": EV_RX_WINDOW_CLOSED})

        # Plan Phase C (revised): send_stream uses send_and_collect with
        # idle/max timeouts. Wrap send_fn to emit the ACK synchronously.
        original_collect = service.send_and_collect

        def wrapped_collect(
            send_fn,
            collect_pred,
            *,
            expected=None,
            idle_timeout_s=0.6,
            max_timeout_s=5.0,
        ):
            def wrapped_send():
                send_fn()
                emit_ack_and_close()
            return original_collect(
                wrapped_send,
                collect_pred,
                expected=expected,
                idle_timeout_s=idle_timeout_s,
                max_timeout_s=max_timeout_s,
            )

        service.send_and_collect = wrapped_collect

        result = service.send_stream(b"\x01\x02\x03", device=controller.dev, retries=0)

        self.assertEqual(result, {"expected": 1, "acked": 1})
        self.assertEqual(
            controller.transport.sent_stream,
            [{"recv3": bytes.fromhex("DDEEFF"), "payload": b"\x01\x02\x03"}],
        )

    def test_identify_reply_restores_stored_group_for_known_device(self):
        controller = FakeController()
        controller.dev.groupId = 3
        service = GatewayService(controller)

        service.on_transport_event(
            {
                "opc": LP.OPC_DEVICES,
                "reply": "IDENTIFY_REPLY",
                "mac6": bytes.fromhex("AABBCCDDEEFF"),
                "groupId": 0,
                "caps": 1,
                "version": 7,
                "host_rssi": -50,
                "host_snr": 8,
            }
        )
        service._join_auto_restore_workers(timeout=2.0)

        self.assertEqual(controller.dev.groupId, 3)
        # Plan P2-6: async worker calls setNodeGroupId with wait_for_ack=True
        self.assertEqual(controller.group_assignments, [("AABBCCDDEEFF", 3, True, True)])

    def test_identify_reply_with_reported_nonzero_group_does_not_auto_reassign_known_device(self):
        controller = FakeController()
        controller.dev.groupId = 3
        service = GatewayService(controller)

        service.on_transport_event(
            {
                "opc": LP.OPC_DEVICES,
                "reply": "IDENTIFY_REPLY",
                "mac6": bytes.fromhex("AABBCCDDEEFF"),
                "groupId": 3,
                "caps": 1,
                "version": 7,
            }
        )

        self.assertEqual(controller.dev.groupId, 3)
        self.assertEqual(controller.group_assignments, [])

    def test_identify_reply_adds_unknown_device_with_reported_group_without_assignment(self):
        controller = FakeController()
        service = GatewayService(controller)

        service.on_transport_event(
            {
                "opc": LP.OPC_DEVICES,
                "reply": "IDENTIFY_REPLY",
                "mac6": bytes.fromhex("001122334455"),
                "groupId": 5,
                "caps": 2,
                "version": 4,
            }
        )

        new_dev = controller.device_repository.get_by_addr("001122334455")
        self.assertIsNotNone(new_dev)
        self.assertEqual(new_dev.groupId, 5)
        self.assertEqual(controller.group_assignments, [])

    def test_identify_reply_with_group_zero_keeps_unknown_device_unconfigured(self):
        controller = FakeController()
        service = GatewayService(controller)

        service.on_transport_event(
            {
                "opc": LP.OPC_DEVICES,
                "reply": "IDENTIFY_REPLY",
                "mac6": bytes.fromhex("001122334455"),
                "groupId": 0,
                "caps": 2,
                "version": 4,
            }
        )

        new_dev = controller.device_repository.get_by_addr("001122334455")
        self.assertIsNotNone(new_dev)
        self.assertEqual(new_dev.groupId, 0)
        self.assertEqual(controller.group_assignments, [])

    def test_identify_reply_group_zero_is_deduplicated_for_known_device(self):
        controller = FakeController()
        controller.dev.groupId = 3
        service = GatewayService(controller)

        ev = {
            "opc": LP.OPC_DEVICES,
            "reply": "IDENTIFY_REPLY",
            "mac6": bytes.fromhex("AABBCCDDEEFF"),
            "groupId": 0,
            "caps": 1,
            "version": 7,
        }

        service.on_transport_event(ev)
        service.on_transport_event(ev)
        service._join_auto_restore_workers(timeout=2.0)

        self.assertEqual(controller.group_assignments, [("AABBCCDDEEFF", 3, True, True)])

    def test_identify_reply_group_zero_is_paused_during_discovery_task(self):
        controller = FakeController()
        controller.dev.groupId = 3
        controller.discovery_active = True
        service = GatewayService(controller)

        service.on_transport_event(
            {
                "opc": LP.OPC_DEVICES,
                "reply": "IDENTIFY_REPLY",
                "mac6": bytes.fromhex("AABBCCDDEEFF"),
                "groupId": 0,
                "caps": 1,
                "version": 7,
            }
        )

        self.assertEqual(controller.group_assignments, [])

    def test_identify_reply_group_zero_skips_invalid_stored_group(self):
        controller = FakeController()
        controller.dev.groupId = 9
        service = GatewayService(controller)

        service.on_transport_event(
            {
                "opc": LP.OPC_DEVICES,
                "reply": "IDENTIFY_REPLY",
                "mac6": bytes.fromhex("AABBCCDDEEFF"),
                "groupId": 0,
                "caps": 1,
                "version": 7,
            }
        )

        self.assertEqual(controller.dev.groupId, 0)
        self.assertEqual(controller.group_assignments, [])

    def test_send_and_collect_exits_on_expected_count(self):
        """Broadcast collector returns immediately once ``expected`` is hit."""
        import time

        controller = FakeController()
        service = GatewayService(controller)

        def send_fn():
            for sender in (b"\x11\x11\x11", b"\x22\x22\x22"):
                controller.transport.emit(
                    {
                        "opc": LP.OPC_ACK,
                        "ack_of": LP.OPC_STREAM,
                        "ack_status": 0,
                        "sender3": sender,
                    }
                )

        def pred(ev):
            return ev.get("opc") == LP.OPC_ACK and int(ev.get("ack_of", -1)) == LP.OPC_STREAM

        t0 = time.monotonic()
        replies = service.send_and_collect(
            send_fn,
            pred,
            expected=2,
            idle_timeout_s=0.6,
            max_timeout_s=5.0,
        )
        elapsed = time.monotonic() - t0

        self.assertEqual(len(replies), 2)
        self.assertLess(elapsed, 0.2, f"expected-count early-exit took {elapsed:.3f}s")

    def test_send_and_collect_terminates_on_idle_after_partial_replies(self):
        """Idle-timeout: after last match + idle window, return even without expected."""
        import threading
        import time

        controller = FakeController()
        service = GatewayService(controller)

        def send_fn():
            # Emit 2 late replies, but not the 3rd expected one. The idle
            # window (120 ms here for test speed) should terminate the wait.
            def late():
                for sender in (b"\x11\x11\x11", b"\x22\x22\x22"):
                    time.sleep(0.02)
                    controller.transport.emit(
                        {
                            "opc": LP.OPC_ACK,
                            "ack_of": LP.OPC_STREAM,
                            "ack_status": 0,
                            "sender3": sender,
                        }
                    )
            threading.Thread(target=late, daemon=True).start()

        def pred(ev):
            return ev.get("opc") == LP.OPC_ACK

        t0 = time.monotonic()
        replies = service.send_and_collect(
            send_fn,
            pred,
            expected=3,  # 3 expected but only 2 arrive
            idle_timeout_s=0.12,
            max_timeout_s=5.0,
        )
        elapsed = time.monotonic() - t0

        self.assertEqual(len(replies), 2)
        # Last arrival at ~0.04 s + 0.12 s idle ~= 0.16 s, well under max.
        self.assertLess(elapsed, 0.5, f"idle termination took {elapsed:.3f}s")

    def test_send_and_collect_hits_hard_ceiling_when_no_reply(self):
        """No reply at all: return after ``max_timeout_s``, not earlier."""
        import time

        controller = FakeController()
        service = GatewayService(controller)

        def send_fn():
            pass  # no emissions

        def pred(ev):
            return True

        t0 = time.monotonic()
        replies = service.send_and_collect(
            send_fn,
            pred,
            expected=None,
            idle_timeout_s=0.6,
            max_timeout_s=0.15,
        )
        elapsed = time.monotonic() - t0

        self.assertEqual(replies, [])
        # Should respect the ceiling approximately.
        self.assertGreaterEqual(elapsed, 0.10)
        self.assertLess(elapsed, 0.6)

    def test_post_match_settle_delay_off_by_default(self):
        """Default is 0.0 -- the earlier LBT/CAD hypothesis was wrong.

        Kept as an optional knob in case a future diagnostic wants it.
        """
        import time

        controller = FakeController()
        service = GatewayService(controller)
        self.assertEqual(service.post_match_settle_s, 0.0)

        def send_fn():
            controller.transport.emit(
                {
                    "opc": LP.OPC_ACK,
                    "ack_of": LP.OPC_SET_GROUP,
                    "ack_status": 0,
                    "sender3": bytes.fromhex("DDEEFF"),
                }
            )

        t0 = time.monotonic()
        service.send_and_wait_for_reply(
            bytes.fromhex("DDEEFF"), LP.OPC_SET_GROUP, send_fn, timeout_s=2.0
        )
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 0.1)

    def test_compute_collect_max_timeout_scales_and_clamps(self):
        f = GatewayService.compute_collect_max_timeout
        self.assertAlmostEqual(f(0), 1.0)
        self.assertAlmostEqual(f(4), 1.0 + 4 * 0.15, places=5)
        self.assertAlmostEqual(f(100, ceiling_s=3.5), 3.5)  # clamped

    def test_auto_restore_marks_device_offline_when_ack_times_out(self):
        """Plan P2-6: async worker calls mark_offline if SET_GROUP returns False."""
        controller = FakeController()
        controller.dev.groupId = 3
        # Override setNodeGroupId to simulate an ACK timeout.
        def _no_ack(dev, forceSet=False, wait_for_ack=True):
            controller.group_assignments.append((dev.addr, dev.groupId, forceSet, wait_for_ack))
            return False
        controller.setNodeGroupId = _no_ack
        service = GatewayService(controller)

        service.on_transport_event(
            {
                "opc": LP.OPC_DEVICES,
                "reply": "IDENTIFY_REPLY",
                "mac6": bytes.fromhex("AABBCCDDEEFF"),
                "groupId": 0,
                "caps": 1,
                "version": 7,
            }
        )
        service._join_auto_restore_workers(timeout=2.0)

        self.assertFalse(controller.dev.link_online)
        self.assertEqual(controller.dev.link_error, "Auto-restore SET_GROUP timeout")


if __name__ == "__main__":
    unittest.main()
