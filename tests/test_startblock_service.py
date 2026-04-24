import unittest
from types import SimpleNamespace

from racelink.domain import RL_Device, RL_Dev_Type
from racelink.services.startblock_service import StartblockService


class FakeStreamService:
    def __init__(self):
        self.calls = []

    def send_stream(self, payload, groupId=None, device=None):
        self.calls.append(
            {
                "payload": payload,
                "groupId": groupId,
                "device": device,
            }
        )
        return {"expected": 1, "acked": 1}


class FakeRepo:
    def __init__(self, items):
        self._items = items

    def list(self):
        return self._items


class FakeSource:
    def get_current_heat_slot_list(self):
        return [
            (0, "Alpha", "R1"),
            (1, "Bravo", "R2"),
            (2, "Charlie", "R3"),
        ]


class FakeController:
    def __init__(self, devices=None):
        self._host_api = SimpleNamespace(event_source=FakeSource())
        self.saved = []
        self.send_config_calls = []
        self._devices = devices or []

    @property
    def device_repository(self):
        return FakeRepo(self._devices)

    def _require_transport(self, _context):
        return True

    def sendConfig(self, **kwargs):
        self.send_config_calls.append(kwargs)
        return True

    def save_to_db(self, args, scopes=None):
        self.saved.append((args, scopes))


class StartblockServiceTests(unittest.TestCase):
    def test_send_startblock_config_updates_specials_and_persists(self):
        device = RL_Device(
            "AABBCCDDEEFF",
            RL_Dev_Type.NODE_WLED_STARTBLOCK_REV3,
            "SB",
            caps=RL_Dev_Type.NODE_WLED_STARTBLOCK_REV3,
        )
        controller = FakeController([device])
        stream_service = FakeStreamService()
        service = StartblockService(controller, stream_service)

        result = service.send_startblock_config(
            target_device=device,
            params={"startblock_slots": 3, "startblock_first_slot": 2},
        )

        self.assertTrue(result)
        self.assertEqual(len(controller.send_config_calls), 2)
        self.assertEqual(device.specials["startblock_slots"], 3)
        self.assertEqual(device.specials["startblock_first_slot"], 2)
        from racelink.domain import state_scope

        self.assertEqual(len(controller.saved), 1)
        args, scopes = controller.saved[0]
        self.assertEqual(args, {"manual": True})
        self.assertEqual(set(scopes or set()), {state_scope.DEVICE_SPECIALS})

    def test_send_startblock_control_in_group_mode_sends_all_slots(self):
        controller = FakeController()
        stream_service = FakeStreamService()
        service = StartblockService(controller, stream_service)

        result = service.send_startblock_control(target_group=7)

        self.assertEqual(result["mode"], "group")
        self.assertEqual(result["groupId"], 7)
        self.assertEqual(len(stream_service.calls), 8)
        self.assertTrue(all(call["groupId"] == 7 for call in stream_service.calls))
        self.assertEqual(stream_service.calls[0]["payload"][5:], b"ALPHA")
        self.assertEqual(stream_service.calls[1]["payload"][5:], b"BRAVO")

    def test_send_startblock_control_maps_slots_to_matching_devices(self):
        dev_a = RL_Device(
            "AABBCCDDEEFF",
            RL_Dev_Type.NODE_WLED_STARTBLOCK_REV3,
            "SB-A",
            caps=RL_Dev_Type.NODE_WLED_STARTBLOCK_REV3,
        )
        dev_b = RL_Device(
            "001122334455",
            RL_Dev_Type.NODE_WLED_STARTBLOCK_REV3,
            "SB-B",
            caps=RL_Dev_Type.NODE_WLED_STARTBLOCK_REV3,
        )
        dev_a.specials["startblock_slots"] = 2
        dev_a.specials["startblock_first_slot"] = 1
        dev_b.specials["startblock_slots"] = 2
        dev_b.specials["startblock_first_slot"] = 3

        controller = FakeController([dev_a, dev_b])
        stream_service = FakeStreamService()
        service = StartblockService(controller, stream_service)

        result = service.send_startblock_control()

        self.assertEqual(result["mode"], "unicast")
        self.assertEqual(len(stream_service.calls), 4)
        self.assertIs(stream_service.calls[0]["device"], dev_a)
        self.assertIs(stream_service.calls[1]["device"], dev_a)
        self.assertIs(stream_service.calls[2]["device"], dev_b)
        self.assertIs(stream_service.calls[3]["device"], dev_b)
        self.assertEqual(result["devices"][0]["first"], 1)
        self.assertEqual(result["devices"][1]["first"], 3)

    def test_normalize_slot_list_accepts_tuples_and_dicts(self):
        controller = FakeController()
        stream_service = FakeStreamService()
        service = StartblockService(controller, stream_service)

        result = service.normalize_slot_list(
            [
                (1, "Pilot A", "R1"),
                {"slot": 2, "callsign": "Pilot B", "racechannel": "R2"},
            ]
        )

        self.assertEqual(
            result,
            [
                (1, "Pilot A", "R1"),
                (2, "Pilot B", "R2"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
