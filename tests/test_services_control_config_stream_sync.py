import unittest

from racelink.domain import RL_Device
from racelink.services import ConfigService, ControlService, StreamService, SyncService


class FakeTransport:
    def __init__(self):
        self.control_calls = []
        self.sync_calls = []

    def send_control(self, **kwargs):
        self.control_calls.append(kwargs)


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

        service.send_device_control(controller.devices[0], flags=5, preset_id=7, brightness=80)
        service.send_group_control(2, 1, 9, 33)

        self.assertEqual(controller.transport.control_calls[0]["group_id"], 2)
        self.assertEqual(controller.devices[0].flags, 1)
        self.assertEqual(controller.devices[0].presetId, 9)
        self.assertEqual(controller.devices[1].brightness, 33)

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
        self.assertEqual(StreamService.build_ctrl(True, False, 3), 0x83)


if __name__ == "__main__":
    unittest.main()
