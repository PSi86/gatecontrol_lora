"""A12 — Unit tests for the WLED effect metadata parser in gen_wled_metadata.py.

Covers the parsing of representative ``_data_FX_MODE_*`` PROGMEM strings taken
directly from wled00/FX.cpp, plus the integration points that expose slot info
to the WebUI (``_normalize_select_options``).
"""

import unittest

from gen_wled_metadata import parse_fx_metadata
from racelink.domain.specials import _normalize_select_options


class ParseFxMetadataTests(unittest.TestCase):
    def test_solid_has_no_slot_usage(self):
        name, slots = parse_fx_metadata("Solid")
        self.assertEqual(name, "Solid")
        for field in (
            "speed", "intensity", "custom1", "custom2", "custom3",
            "check1", "check2", "check3",
            "color1", "color2", "color3", "palette",
        ):
            self.assertFalse(slots[field]["used"], field)
            self.assertIsNone(slots[field]["label"], field)

    def test_blink_uses_sx_ix_col1_col2_palette(self):
        # From FX.cpp: "Blink@!,Duty cycle;!,!;!;01"
        name, slots = parse_fx_metadata("Blink@!,Duty cycle;!,!;!;01")
        self.assertEqual(name, "Blink")
        self.assertTrue(slots["speed"]["used"])
        self.assertIsNone(slots["speed"]["label"])  # "!" = default label
        self.assertTrue(slots["intensity"]["used"])
        self.assertEqual(slots["intensity"]["label"], "Duty cycle")
        for unused in ("custom1", "custom2", "custom3", "check1", "check2", "check3", "color3"):
            self.assertFalse(slots[unused]["used"], unused)
        self.assertTrue(slots["color1"]["used"])
        self.assertTrue(slots["color2"]["used"])
        self.assertTrue(slots["palette"]["used"])

    def test_scan_has_toggle_on_o2_slot(self):
        # From FX.cpp: "Scan@!,# of dots,,,,,Overlay;!,!,!;!"
        name, slots = parse_fx_metadata("Scan@!,# of dots,,,,,Overlay;!,!,!;!")
        self.assertEqual(name, "Scan")
        self.assertTrue(slots["intensity"]["used"])
        self.assertEqual(slots["intensity"]["label"], "# of dots")
        # positions 2..4 (c1, c2, c3) are empty
        self.assertFalse(slots["custom1"]["used"])
        self.assertFalse(slots["custom2"]["used"])
        self.assertFalse(slots["custom3"]["used"])
        # o1 empty, o2 = "Overlay", o3 not present
        self.assertFalse(slots["check1"]["used"])
        self.assertTrue(slots["check2"]["used"])
        self.assertEqual(slots["check2"]["label"], "Overlay")
        self.assertFalse(slots["check3"]["used"])
        # all three colors used + palette
        self.assertTrue(slots["color1"]["used"])
        self.assertTrue(slots["color2"]["used"])
        self.assertTrue(slots["color3"]["used"])
        self.assertTrue(slots["palette"]["used"])

    def test_palette_effect_has_three_toggles(self):
        # From FX.cpp: "Palette@Shift,Size,Rotation,,,Animate Shift,Animate Rotation,Anamorphic;;!;12;ix=112,c1=0,o1=1,o2=0,o3=1"
        raw = (
            "Palette@Shift,Size,Rotation,,,Animate Shift,Animate Rotation,Anamorphic"
            ";;!;12;ix=112,c1=0,o1=1,o2=0,o3=1"
        )
        name, slots = parse_fx_metadata(raw)
        self.assertEqual(name, "Palette")
        self.assertEqual(slots["speed"]["label"], "Shift")
        self.assertEqual(slots["intensity"]["label"], "Size")
        self.assertEqual(slots["custom1"]["label"], "Rotation")
        self.assertFalse(slots["custom2"]["used"])
        self.assertFalse(slots["custom3"]["used"])
        self.assertEqual(slots["check1"]["label"], "Animate Shift")
        self.assertEqual(slots["check2"]["label"], "Animate Rotation")
        self.assertEqual(slots["check3"]["label"], "Anamorphic")
        # Group 2 is empty -> no colors used.
        self.assertFalse(slots["color1"]["used"])
        self.assertFalse(slots["color2"]["used"])
        self.assertFalse(slots["color3"]["used"])
        self.assertTrue(slots["palette"]["used"])

    def test_fireworks_has_empty_speed_token(self):
        # From FX.cpp: "Fireworks@,Frequency;!,!;!;12;ix=192,pal=11"
        name, slots = parse_fx_metadata("Fireworks@,Frequency;!,!;!;12;ix=192,pal=11")
        self.assertEqual(name, "Fireworks")
        self.assertFalse(slots["speed"]["used"])  # empty token 0
        self.assertEqual(slots["intensity"]["label"], "Frequency")
        self.assertTrue(slots["color1"]["used"])
        self.assertTrue(slots["color2"]["used"])
        self.assertFalse(slots["color3"]["used"])
        self.assertTrue(slots["palette"]["used"])

    def test_meteor_has_mixed_toggles(self):
        # From FX.cpp: "Meteor@!,Trail,,,,Gradient,,Smooth;;!;1"
        name, slots = parse_fx_metadata("Meteor@!,Trail,,,,Gradient,,Smooth;;!;1")
        self.assertEqual(name, "Meteor")
        self.assertTrue(slots["speed"]["used"])
        self.assertEqual(slots["intensity"]["label"], "Trail")
        # o1="Gradient", o2="" (unused), o3="Smooth"
        self.assertEqual(slots["check1"]["label"], "Gradient")
        self.assertFalse(slots["check2"]["used"])
        self.assertEqual(slots["check3"]["label"], "Smooth")
        self.assertTrue(slots["palette"]["used"])


class NormalizeSelectOptionsTests(unittest.TestCase):
    def test_slots_are_forwarded_when_present(self):
        raw = [
            {"value": "1", "label": "Blink", "slots": {"speed": {"used": True}}},
        ]
        out = _normalize_select_options(raw)
        self.assertEqual(out[0]["value"], "1")
        self.assertEqual(out[0]["label"], "Blink")
        self.assertEqual(out[0]["slots"], {"speed": {"used": True}})

    def test_slots_absent_when_option_has_none(self):
        raw = [{"value": "01", "label": "Red"}]
        out = _normalize_select_options(raw)
        self.assertEqual(out, [{"value": "01", "label": "Red"}])
        self.assertNotIn("slots", out[0])

    def test_slots_non_dict_is_dropped(self):
        raw = [{"value": "1", "label": "Blink", "slots": "oops"}]
        out = _normalize_select_options(raw)
        self.assertNotIn("slots", out[0])


class CoerceRespectsAbsentVarsTests(unittest.TestCase):
    """Regression guard: A12 changes coerce_action_params to omit missing vars."""

    def test_missing_vars_are_omitted(self):
        from racelink.services.specials_service import SpecialsService

        svc = SpecialsService(rl_instance=type("RL", (), {"uiEffectList": []})())
        fn_info = {
            "vars": ["mode", "speed", "color1", "check1"],
            "ui": {
                "mode": {"widget": "select"},
                "speed": {"widget": "slider", "min": 0, "max": 255},
                "color1": {"widget": "color"},
                "check1": {"widget": "toggle"},
            },
        }
        coerced = svc.coerce_action_params(fn_info, {}, {"mode": 5, "color1": "#ff0000"})
        # Only explicitly-supplied vars are present; others are omitted for the
        # service layer to skip entirely (no fieldMask/extMask bit).
        self.assertEqual(set(coerced.keys()), {"mode", "color1"})
        self.assertEqual(coerced["mode"], 5)
        self.assertEqual(coerced["color1"], (0xFF, 0x00, 0x00))


if __name__ == "__main__":
    unittest.main()
