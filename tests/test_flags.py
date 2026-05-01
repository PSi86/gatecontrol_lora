"""Unit tests for the canonical RaceLink flag module.

Covers ``racelink.domain.flags`` -- the single source of truth for the
six protocol flag bits, their serialization to the wire byte used by
both OPC_PRESET (0x04) and OPC_CONTROL (0x08), and the loose-mapping
normalizer used by persistence + REST ingress paths.
"""

import unittest

from racelink.domain import (
    FLAG_BITS,
    RL_FLAG_ARM_ON_SYNC,
    RL_FLAG_FORCE_REAPPLY,
    RL_FLAG_FORCE_TT0,
    RL_FLAG_HAS_BRI,
    RL_FLAG_OFFSET_MODE,
    RL_FLAG_POWER_ON,
    USER_FLAG_KEYS,
    build_flags_byte,
    flags_from_mapping,
)


class FlagConstantsTests(unittest.TestCase):
    def test_constants_match_wire_positions(self):
        # Must match racelink_proto.h / racelink_wled.h bit layout exactly.
        self.assertEqual(RL_FLAG_POWER_ON, 0x01)
        self.assertEqual(RL_FLAG_ARM_ON_SYNC, 0x02)
        self.assertEqual(RL_FLAG_HAS_BRI, 0x04)
        self.assertEqual(RL_FLAG_FORCE_TT0, 0x08)
        self.assertEqual(RL_FLAG_FORCE_REAPPLY, 0x10)
        self.assertEqual(RL_FLAG_OFFSET_MODE, 0x20)

    def test_flag_bits_map_covers_all_six(self):
        self.assertEqual(set(FLAG_BITS.keys()), {
            "power_on", "arm_on_sync", "has_bri",
            "force_tt0", "force_reapply", "offset_mode",
        })
        self.assertEqual(FLAG_BITS["offset_mode"], 0x20)

    def test_user_flag_keys_excludes_derived_flags(self):
        # POWER_ON / HAS_BRI are derived from brightness at emit-time and
        # must not be persistable on an RL preset.
        self.assertNotIn("power_on", USER_FLAG_KEYS)
        self.assertNotIn("has_bri", USER_FLAG_KEYS)
        self.assertEqual(set(USER_FLAG_KEYS), {
            "arm_on_sync", "force_tt0", "force_reapply", "offset_mode",
        })


class BuildFlagsByteTests(unittest.TestCase):
    def test_empty_args_yields_zero(self):
        self.assertEqual(build_flags_byte(), 0x00)

    def test_each_bit_individually(self):
        self.assertEqual(build_flags_byte(power_on=True), 0x01)
        self.assertEqual(build_flags_byte(arm_on_sync=True), 0x02)
        self.assertEqual(build_flags_byte(has_bri=True), 0x04)
        self.assertEqual(build_flags_byte(force_tt0=True), 0x08)
        self.assertEqual(build_flags_byte(force_reapply=True), 0x10)
        self.assertEqual(build_flags_byte(offset_mode=True), 0x20)

    def test_all_six_bits(self):
        out = build_flags_byte(
            power_on=True, arm_on_sync=True, has_bri=True,
            force_tt0=True, force_reapply=True, offset_mode=True,
        )
        self.assertEqual(out, 0x3F)  # bits 0..5

    def test_typical_arm_on_sync_preset(self):
        # A preset that should arm + apply with explicit brightness at full.
        out = build_flags_byte(
            power_on=True, has_bri=True, arm_on_sync=True,
        )
        self.assertEqual(out, 0x07)

    def test_returns_uint8_range(self):
        self.assertEqual(build_flags_byte(power_on=True) & 0xFF, 0x01)
        self.assertLessEqual(
            build_flags_byte(power_on=True, arm_on_sync=True, has_bri=True,
                             force_tt0=True, force_reapply=True, offset_mode=True),
            0xFF,
        )


class FlagsFromMappingTests(unittest.TestCase):
    def test_none_input_defaults_all_false(self):
        self.assertEqual(flags_from_mapping(None), {
            "arm_on_sync": False, "force_tt0": False,
            "force_reapply": False, "offset_mode": False,
        })

    def test_empty_mapping_defaults_all_false(self):
        self.assertEqual(flags_from_mapping({}), {
            "arm_on_sync": False, "force_tt0": False,
            "force_reapply": False, "offset_mode": False,
        })

    def test_unknown_keys_are_ignored(self):
        out = flags_from_mapping({"power_on": True, "has_bri": True, "bogus": 1})
        # Derived flags are never user-persistable; must not leak through.
        self.assertNotIn("power_on", out)
        self.assertNotIn("has_bri", out)
        self.assertNotIn("bogus", out)

    def test_coerces_truthy_values_to_bool(self):
        out = flags_from_mapping({
            "arm_on_sync": 1, "force_tt0": "yes",
            "force_reapply": 0, "offset_mode": None,
        })
        self.assertIs(out["arm_on_sync"], True)
        self.assertIs(out["force_tt0"], True)
        self.assertIs(out["force_reapply"], False)
        self.assertIs(out["offset_mode"], False)


if __name__ == "__main__":
    unittest.main()
