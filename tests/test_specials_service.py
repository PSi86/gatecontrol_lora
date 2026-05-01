import unittest

from racelink.domain import RL_Device, RL_Dev_Type
from racelink.services.specials_service import SpecialsService


class SpecialsServiceTests(unittest.TestCase):
    def setUp(self):
        rl_instance = type("RL", (), {"uiPresetList": [{"value": "01", "label": "Red"}]})()
        self.service = SpecialsService(rl_instance=rl_instance)
        self.startblock = RL_Device(
            "AABBCCDDEEFF",
            RL_Dev_Type.NODE_WLED_STARTBLOCK_REV3,
            "SB",
            caps=RL_Dev_Type.NODE_WLED_STARTBLOCK_REV3,
        )

    def test_resolve_option_finds_startblock_config(self):
        option = self.service.resolve_option(self.startblock, "startblock_slots")
        self.assertIsNotNone(option)
        self.assertEqual(option.get("option"), 0x8C)

    def test_resolve_action_and_coerce_params_for_startblock_control(self):
        fn_info, options_by_key = self.service.resolve_action(self.startblock, "startblock_control")
        params = self.service.coerce_action_params(fn_info, options_by_key, {"startblock_use_current_heat": 1})
        self.assertEqual(fn_info.get("comm"), "sendStartblockControl")
        self.assertEqual(params, {})

    def test_validate_option_value_enforces_bounds(self):
        option = self.service.resolve_option(self.startblock, "startblock_first_slot")
        with self.assertRaises(ValueError):
            self.service.validate_option_value(option, 99)

    def test_get_serialized_config_includes_ui_safe_specials(self):
        config = self.service.get_serialized_config()

        self.assertIn("STARTBLOCK", config)
        self.assertIn("WLED", config)
        self.assertEqual(
            config["WLED"]["functions"][0]["ui"]["presetId"]["options"],
            [{"value": "01", "label": "Red"}],
        )


if __name__ == "__main__":
    unittest.main()
