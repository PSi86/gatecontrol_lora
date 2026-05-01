"""Phase C.N5 (updated for Phase D rename) — tests for ``send_rl_preset_by_id``.

Covers the RL-preset apply path that the RotorHazard plugin uses: resolve
a preset by its stable int id, merge any brightness override +
preset-stored flags, and dispatch to ``send_wled_control`` (OPC_CONTROL).

Post-Phase-D naming reference:
- OPC_PRESET  (0x04) → transport.send_preset(), service.send_wled_preset()
- OPC_CONTROL (0x08) → transport.send_control(), service.send_wled_control()
"""

import os
import tempfile
import unittest

from racelink.services.control_service import ControlService
from racelink.services.rl_presets_service import RLPresetsService


class _FakeTransport:
    """Captures transport-layer calls for both packet types."""

    def __init__(self):
        self.preset_calls = []   # OPC_PRESET (4 B fixed)
        self.control_calls = []  # OPC_CONTROL (variable length)

    def send_preset(self, **kwargs):
        self.preset_calls.append(kwargs)

    def send_control(self, **kwargs):
        self.control_calls.append(kwargs)


class _FakeController:
    def __init__(self, rl_presets_service=None):
        self.transport = _FakeTransport()
        self.device_repository = type("Repo", (), {"list": staticmethod(lambda: [])})()
        self.rl_presets_service = rl_presets_service


class _Dev:
    def __init__(self, addr="AABBCCDDEEFF", group_id=7):
        self.addr = addr
        self.groupId = group_id
        self.flags = 0
        self.presetId = 0
        self.brightness = 0


class SendRlPresetByIdTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.rl_svc = RLPresetsService(storage_path=os.path.join(self._tmp.name, "rl.json"))
        self.controller = _FakeController(rl_presets_service=self.rl_svc)
        self.service = ControlService(self.controller, None)

    def test_apply_sends_control_with_preset_params(self):
        p = self.rl_svc.create(
            label="Breathe Red",
            params={"mode": 2, "speed": 200, "color1": [255, 0, 0], "brightness": 255},
        )
        ok = self.service.send_rl_preset_by_id(
            p["id"], targetDevice=_Dev(), brightness_override=None,
        )
        self.assertTrue(ok)
        # No classical preset frame; variable-length control frame instead.
        self.assertEqual(self.controller.transport.preset_calls, [])
        self.assertEqual(len(self.controller.transport.control_calls), 1)
        call = self.controller.transport.control_calls[0]
        self.assertEqual(call["mode"], 2)
        self.assertEqual(call["speed"], 200)
        self.assertEqual(call["color1"], (255, 0, 0))
        self.assertEqual(call["brightness"], 255)

    def test_brightness_override_wins(self):
        p = self.rl_svc.create(
            label="Red",
            params={"mode": 1, "color1": [200, 0, 0], "brightness": 255},
        )
        ok = self.service.send_rl_preset_by_id(
            p["id"], targetDevice=_Dev(), brightness_override=50,
        )
        self.assertTrue(ok)
        call = self.controller.transport.control_calls[0]
        self.assertEqual(call["brightness"], 50)

    def test_preset_flags_translate_to_control_flag_bits(self):
        p = self.rl_svc.create(
            label="Armed",
            params={"mode": 3, "brightness": 100},
            flags={"arm_on_sync": True, "force_reapply": True},
        )
        ok = self.service.send_rl_preset_by_id(p["id"], targetDevice=_Dev())
        self.assertTrue(ok)
        flags = self.controller.transport.control_calls[0]["flags"]
        self.assertTrue(flags & 0x01)  # POWER_ON (bri>0)
        self.assertTrue(flags & 0x02)  # ARM_ON_SYNC
        self.assertTrue(flags & 0x04)  # HAS_BRI
        self.assertTrue(flags & 0x10)  # FORCE_REAPPLY

    def test_all_four_user_flags_survive_roundtrip(self):
        # Regression for Part 1 of the flag-unification work: send_rl_preset_by_id
        # used to forward only three flags (arm_on_sync, force_tt0, force_reapply).
        # offset_mode is now plumbed end-to-end.
        p = self.rl_svc.create(
            label="Staggered Arm",
            params={"mode": 5, "brightness": 80},
            flags={
                "arm_on_sync": True, "force_tt0": True,
                "force_reapply": True, "offset_mode": True,
            },
        )
        ok = self.service.send_rl_preset_by_id(p["id"], targetDevice=_Dev())
        self.assertTrue(ok)
        flags = self.controller.transport.control_calls[0]["flags"]
        # POWER_ON | ARM | HAS_BRI | FORCE_TT0 | FORCE_REAPPLY | OFFSET_MODE
        self.assertEqual(flags, 0x3F)

    def test_group_target(self):
        p = self.rl_svc.create(label="Green", params={"mode": 22, "brightness": 180})
        ok = self.service.send_rl_preset_by_id(p["id"], targetGroup=3)
        self.assertTrue(ok)
        call = self.controller.transport.control_calls[0]
        self.assertEqual(call["group_id"], 3)
        self.assertEqual(call["recv3"], b"\xFF\xFF\xFF")

    def test_unknown_id_returns_false_without_send(self):
        ok = self.service.send_rl_preset_by_id(42, targetDevice=_Dev())
        self.assertFalse(ok)
        self.assertEqual(self.controller.transport.preset_calls, [])
        self.assertEqual(self.controller.transport.control_calls, [])

    def test_invalid_id_type_returns_false(self):
        ok = self.service.send_rl_preset_by_id("not-an-int", targetDevice=_Dev())
        self.assertFalse(ok)

    def test_missing_rl_service_returns_false(self):
        controller = _FakeController(rl_presets_service=None)
        svc = ControlService(controller, None)
        ok = svc.send_rl_preset_by_id(0, targetDevice=_Dev())
        self.assertFalse(ok)
        self.assertEqual(controller.transport.preset_calls, [])
        self.assertEqual(controller.transport.control_calls, [])

    def test_numeric_string_id_is_accepted(self):
        p = self.rl_svc.create(label="Alpha", params={"mode": 1, "brightness": 128})
        ok = self.service.send_rl_preset_by_id(str(p["id"]), targetDevice=_Dev())
        self.assertTrue(ok)


class ControllerWledControlRoutesViaRlPresetIdTests(unittest.TestCase):
    """Phase D.2: ``RaceLink_Host.sendWledControl`` (the Specials entry point
    for "WLED Control") resolves an RL preset by id and dispatches
    ``send_rl_preset_by_id``. Exercised through the actual controller class
    so the full Specials → Controller → Service path is covered."""

    def setUp(self):
        # We instantiate the real ControlService (logic under test) but hand
        # it a minimal fake controller that exposes just the attributes the
        # service touches: transport + rl_presets_service.
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.rl_svc = RLPresetsService(storage_path=os.path.join(self._tmp.name, "rl.json"))
        self.controller = _FakeController(rl_presets_service=self.rl_svc)
        self.service = ControlService(self.controller, None)

    def test_preset_picker_params_resolve_and_send(self):
        p = self.rl_svc.create(label="Cyan", params={"mode": 12, "color1": [0, 200, 200]})
        # Imitate what RaceLink_Host.sendWledControl does: delegate.
        ok = self.service.send_rl_preset_by_id(
            int("0"),  # str-id from select option would be coerced int() upstream
            targetDevice=_Dev(),
            brightness_override=128,
        )
        self.assertTrue(ok)
        self.assertEqual(len(self.controller.transport.control_calls), 1)
        self.assertEqual(self.controller.transport.control_calls[0]["mode"], 12)
        self.assertEqual(self.controller.transport.control_calls[0]["brightness"], 128)
        # Also assert the preset id round-trip.
        self.assertEqual(p["id"], 0)


class SendWledPresetIsIntOnlyTests(unittest.TestCase):
    """Regression: ``send_wled_preset`` (the classical preset path, OPC_PRESET)
    accepts only numeric ids. RL presets go through ``send_rl_preset_by_id``."""

    def setUp(self):
        self.controller = _FakeController()
        self.service = ControlService(self.controller, None)

    def test_int_preset_routes_to_preset(self):
        ok = self.service.send_wled_preset(
            targetDevice=_Dev(),
            params={"presetId": 5, "brightness": 200},
        )
        self.assertTrue(ok)
        self.assertEqual(len(self.controller.transport.preset_calls), 1)
        self.assertEqual(self.controller.transport.control_calls, [])

    def test_numeric_string_still_works(self):
        ok = self.service.send_wled_preset(
            targetDevice=_Dev(),
            params={"presetId": "7", "brightness": 0},
        )
        self.assertTrue(ok)
        self.assertEqual(self.controller.transport.preset_calls[0]["preset_id"], 7)

    def test_non_numeric_preset_raises(self):
        with self.assertRaises(ValueError):
            self.service.send_wled_preset(
                targetDevice=_Dev(),
                params={"presetId": "breathe_red", "brightness": 128},
            )


if __name__ == "__main__":
    unittest.main()
