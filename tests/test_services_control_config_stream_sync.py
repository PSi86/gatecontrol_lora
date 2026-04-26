import unittest

from racelink.domain import RL_Device
from racelink.services import ConfigService, ControlService, StreamService, SyncService


class FakeTransport:
    def __init__(self):
        self.preset_calls = []
        self.control_calls = []
        self.sync_calls = []
        self.offset_calls = []

    def send_preset(self, **kwargs):
        self.preset_calls.append(kwargs)

    def send_control(self, **kwargs):
        self.control_calls.append(kwargs)

    def send_offset(self, **kwargs):
        # Flatten mode_params kwargs onto the recorded call dict so tests
        # can assert on per-mode fields directly (offset_ms, base_ms, …).
        record = dict(kwargs)
        self.offset_calls.append(record)


class FakeGateway:
    def __init__(self):
        self.config_calls = []
        self.sync_calls = []
        self.stream_calls = []

    def send_config(self, *args, **kwargs):
        self.config_calls.append((args, kwargs))
        return True

    def send_sync(self, *args, **kwargs):
        self.sync_calls.append((args, kwargs))

    def send_stream(self, *args, **kwargs):
        self.stream_calls.append((args, kwargs))
        return {"expected": 1, "acked": 1}


class FakeController:
    def __init__(self):
        self.transport = FakeTransport()
        self.devices = [
            RL_Device("AABBCCDDEEFF", 1, "Node A", groupId=2),
            RL_Device("001122334455", 1, "Node B", groupId=2),
        ]

    @property
    def device_repository(self):
        class Repo:
            def __init__(self, items):
                self._items = items

            def list(self):
                return self._items

        return Repo(self.devices)


class ActiveSendServiceTests(unittest.TestCase):
    def test_control_service_updates_device_and_group_cache(self):
        controller = FakeController()
        gateway = FakeGateway()
        service = ControlService(controller, gateway)

        service.send_device_preset(controller.devices[0], flags=5, preset_id=7, brightness=80)
        service.send_group_preset(2, 1, 9, 33)

        self.assertEqual(controller.transport.preset_calls[0]["group_id"], 2)
        self.assertEqual(controller.devices[0].flags, 1)
        self.assertEqual(controller.devices[0].presetId, 9)
        self.assertEqual(controller.devices[1].brightness, 33)

    def test_b2_send_device_preset_returns_true_on_success(self):
        """B2: ``send_device_preset`` must now return ``True`` on a
        successful transport queue (was: implicit ``None``)."""
        controller = FakeController()
        service = ControlService(controller, FakeGateway())
        result = service.send_device_preset(
            controller.devices[0], flags=0, preset_id=1, brightness=0,
        )
        self.assertIs(result, True)

    def test_b2_send_group_preset_returns_true_on_success(self):
        """B2: same contract on the group-broadcast send."""
        controller = FakeController()
        service = ControlService(controller, FakeGateway())
        result = service.send_group_preset(2, 0, 1, 0)
        self.assertIs(result, True)

    def test_b2_send_device_preset_returns_false_when_transport_missing(self):
        """B2 fix: pre-fix this fell off the end as ``None``, which the
        wrapper :meth:`send_wled_preset` misinterpreted as success."""
        controller = FakeController()
        controller.transport = None
        service = ControlService(controller, FakeGateway())
        result = service.send_device_preset(
            controller.devices[0], flags=0, preset_id=1, brightness=0,
        )
        self.assertIs(result, False)

    def test_b2_send_group_preset_returns_false_when_transport_missing(self):
        controller = FakeController()
        controller.transport = None
        service = ControlService(controller, FakeGateway())
        result = service.send_group_preset(2, 0, 1, 0)
        self.assertIs(result, False)

    def test_b2_send_wled_preset_propagates_underlying_false(self):
        """B2 wrapper fix: ``send_wled_preset`` previously returned
        ``True`` unconditionally for any non-None target. Now it
        propagates the boolean from the underlying ``send_*_preset``,
        so a missing transport surfaces as ``False`` to the route /
        scene runner."""
        controller = FakeController()
        controller.transport = None
        service = ControlService(controller, FakeGateway())

        # Group path.
        result_group = service.send_wled_preset(
            targetGroup=2,
            params={"presetId": 1, "brightness": 100},
        )
        self.assertIs(result_group, False)

        # Device path.
        result_dev = service.send_wled_preset(
            targetDevice=controller.devices[0],
            params={"presetId": 1, "brightness": 100},
        )
        self.assertIs(result_dev, False)

    def test_b2_send_wled_preset_returns_true_on_success(self):
        """Sanity check the happy path still reports True."""
        controller = FakeController()
        service = ControlService(controller, FakeGateway())
        result = service.send_wled_preset(
            targetGroup=2,
            params={"presetId": 1, "brightness": 100},
        )
        self.assertIs(result, True)

    def test_send_wled_preset_honours_all_user_flags(self):
        controller = FakeController()
        gateway = FakeGateway()
        service = ControlService(controller, gateway)

        service.send_wled_preset(
            targetDevice=controller.devices[0],
            params={
                "presetId": 7, "brightness": 128,
                "arm_on_sync": True, "force_tt0": True,
                "force_reapply": True, "offset_mode": True,
            },
        )

        call = controller.transport.preset_calls[0]
        # POWER_ON (bri>0) | ARM | HAS_BRI | FORCE_TT0 | FORCE_REAPPLY | OFFSET_MODE = 0x3F
        self.assertEqual(call["flags"], 0x3F)
        self.assertEqual(call["preset_id"], 7)
        self.assertEqual(call["brightness"], 128)

    def test_send_wled_preset_offset_mode_alone(self):
        controller = FakeController()
        gateway = FakeGateway()
        service = ControlService(controller, gateway)

        service.send_wled_preset(
            targetGroup=2,
            params={"presetId": 1, "brightness": 0, "offset_mode": True},
        )

        call = controller.transport.preset_calls[0]
        # brightness=0 -> no POWER_ON. HAS_BRI always set. OFFSET_MODE bit 5.
        self.assertEqual(call["flags"], 0x04 | 0x20)

    def test_send_wled_control_unifies_flag_emission(self):
        controller = FakeController()
        gateway = FakeGateway()
        service = ControlService(controller, gateway)

        service.send_wled_control(
            targetDevice=controller.devices[0],
            params={
                "brightness": 200, "mode": 12,
                "arm_on_sync": True, "offset_mode": True,
            },
        )

        call = controller.transport.control_calls[0]
        # POWER_ON | ARM | HAS_BRI | OFFSET_MODE = 0x01 | 0x02 | 0x04 | 0x20 = 0x27
        self.assertEqual(call["flags"], 0x27)
        self.assertEqual(call["mode"], 12)
        self.assertEqual(call["brightness"], 200)

    def test_send_wled_preset_and_control_emit_same_flag_byte(self):
        # Core property of the unification: identical params -> identical
        # flag byte on the wire for both opcodes.
        controller = FakeController()
        gateway = FakeGateway()
        service = ControlService(controller, gateway)

        common = {
            "brightness": 100, "arm_on_sync": True,
            "force_tt0": True, "offset_mode": True,
        }
        service.send_wled_preset(
            targetDevice=controller.devices[0],
            params={**common, "presetId": 5},
        )
        service.send_wled_control(
            targetDevice=controller.devices[1],
            params={**common, "mode": 9},
        )

        self.assertEqual(
            controller.transport.preset_calls[0]["flags"],
            controller.transport.control_calls[0]["flags"],
        )

    def test_send_offset_explicit_to_group_uses_broadcast_recv3(self):
        controller = FakeController()
        gateway = FakeGateway()
        service = ControlService(controller, gateway)

        ok = service.send_offset(targetGroup=3, mode="explicit", offset_ms=250)
        self.assertTrue(ok)
        self.assertEqual(len(controller.transport.offset_calls), 1)
        call = controller.transport.offset_calls[0]
        self.assertEqual(call["recv3"], b"\xFF\xFF\xFF")
        self.assertEqual(call["group_id"], 3)
        self.assertEqual(call["mode"], "explicit")
        self.assertEqual(call["offset_ms"], 250)

    def test_send_offset_linear_broadcast_carries_formula_params(self):
        controller = FakeController()
        gateway = FakeGateway()
        service = ControlService(controller, gateway)

        ok = service.send_offset(targetGroup=255, mode="linear",
                                 base_ms=0, step_ms=100)
        self.assertTrue(ok)
        call = controller.transport.offset_calls[0]
        self.assertEqual(call["mode"], "linear")
        self.assertEqual(call["base_ms"], 0)
        self.assertEqual(call["step_ms"], 100)

    def test_send_offset_none_clears_pending_change(self):
        controller = FakeController()
        gateway = FakeGateway()
        service = ControlService(controller, gateway)

        ok = service.send_offset(targetGroup=255, mode="none")
        self.assertTrue(ok)
        call = controller.transport.offset_calls[0]
        self.assertEqual(call["mode"], "none")

    def test_send_offset_to_device_unicasts_via_mac_last3(self):
        controller = FakeController()
        gateway = FakeGateway()
        service = ControlService(controller, gateway)

        ok = service.send_offset(
            targetDevice=controller.devices[0], mode="explicit", offset_ms=100,
        )
        self.assertTrue(ok)
        call = controller.transport.offset_calls[0]
        # last 3 bytes of MAC AABBCCDDEEFF -> DD EE FF
        self.assertEqual(call["recv3"], b"\xDD\xEE\xFF")
        # device's groupId is echoed into the body for completeness (filter
        # doesn't strictly matter on unicast but mirrors broadcast behaviour).
        self.assertEqual(call["group_id"], 2)
        self.assertEqual(call["offset_ms"], 100)

    def test_send_offset_returns_false_when_no_target_provided(self):
        controller = FakeController()
        gateway = FakeGateway()
        service = ControlService(controller, gateway)

        ok = service.send_offset(mode="explicit", offset_ms=10)
        self.assertFalse(ok)
        self.assertEqual(controller.transport.offset_calls, [])

    def test_config_sync_and_stream_services_delegate_to_gateway(self):
        controller = FakeController()
        gateway = FakeGateway()
        config_service = ConfigService(controller, gateway)
        sync_service = SyncService(controller, gateway)
        stream_service = StreamService(controller, gateway)

        self.assertTrue(config_service.send_config(0x04, data0=1, recv3=b"\xAA\xBB\xCC"))
        config_service.apply_config_update(controller.devices[0], 0x04, 1)
        sync_service.send_sync(0x123456, 44)
        result = stream_service.send_stream(b"\x01\x02", groupId=2)

        self.assertEqual(controller.devices[0].configByte & 0x04, 0x04)
        self.assertEqual(len(gateway.config_calls), 1)
        self.assertEqual(len(gateway.sync_calls), 1)
        self.assertEqual(len(gateway.stream_calls), 1)
        self.assertEqual(result, {"expected": 1, "acked": 1})


if __name__ == "__main__":
    unittest.main()
