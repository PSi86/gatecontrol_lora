"""A12 — Unit tests for the WLED effect metadata parser in gen_wled_metadata.py.

Covers the parsing of representative ``_data_FX_MODE_*`` PROGMEM strings taken
directly from wled00/FX.cpp, plus the integration points that expose slot info
to the WebUI (``_normalize_select_options``).
"""

import unittest

from gen_wled_metadata import parse_fx_metadata, parse_palette_color_rule
from racelink.domain.specials import _normalize_select_options, serialize_rl_preset_editor_schema
from racelink.domain.wled_palette_color_rules import WLED_PALETTE_COLOR_RULES


class ParseFxMetadataTests(unittest.TestCase):
    def test_solid_uses_default_color_slots(self):
        """D3 regression: effects without an '@' in their metadata (``Solid``,
        ``Oscillate``) must still surface color 1/2/3 + palette in the editor —
        WLED uses those controls by default."""
        name, slots = parse_fx_metadata("Solid")
        self.assertEqual(name, "Solid")
        # Sliders/toggles stay unused (no explicit labels).
        for field in (
            "speed", "intensity", "custom1", "custom2", "custom3",
            "check1", "check2", "check3",
        ):
            self.assertFalse(slots[field]["used"], field)
            self.assertIsNone(slots[field]["label"], field)
        # Colors + palette default to used.
        for field in ("color1", "color2", "color3", "palette"):
            self.assertTrue(slots[field]["used"], field)
            self.assertIsNone(slots[field]["label"], field)

    def test_oscillate_also_uses_default_color_slots(self):
        """Second ``@``-less effect in the WLED source; same default coverage."""
        name, slots = parse_fx_metadata("Oscillate")
        self.assertEqual(name, "Oscillate")
        self.assertTrue(slots["color1"]["used"])
        self.assertTrue(slots["color2"]["used"])
        self.assertTrue(slots["color3"]["used"])
        self.assertTrue(slots["palette"]["used"])
        self.assertFalse(slots["speed"]["used"])
        self.assertFalse(slots["check1"]["used"])

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

    def test_deterministic_flag_forwarded_when_present(self):
        # The WLED effect mode generator uses this path to ship the
        # deterministic marker end-to-end. The presence and bool-coercion
        # are pinned so a future refactor can't quietly drop the field.
        raw = [{"value": "2", "label": "Breathe", "deterministic": True}]
        out = _normalize_select_options(raw)
        self.assertIs(out[0]["deterministic"], True)

    def test_deterministic_flag_absent_when_not_provided(self):
        raw = [{"value": "01", "label": "Red"}]
        out = _normalize_select_options(raw)
        self.assertNotIn("deterministic", out[0])


class WledDeterministicTaggingTests(unittest.TestCase):
    """The WLED effect-mode select must mark the audited deterministic
    subset (see racelink.domain.wled_deterministic) with the
    ``deterministic: True`` flag and sort those entries to the top so the
    operator picks offset-mode-safe effects first.
    """

    def test_deterministic_id_set_matches_analysis(self):
        # Pin the audited set so a future refactor / merge can't quietly
        # add or remove an ID without an explicit code review. Update
        # this list together with WLED_DETERMINISTIC_EFFECT_IDS when the
        # analysis doc adds a verified effect.
        from racelink.domain.wled_deterministic import (
            WLED_DETERMINISTIC_EFFECT_IDS,
        )
        expected = {0, 1, 2, 3, 6, 8, 9, 10, 11, 12, 15, 16, 23, 35, 52,
                    65, 83, 84, 115}
        self.assertEqual(set(WLED_DETERMINISTIC_EFFECT_IDS), expected)
        self.assertEqual(len(WLED_DETERMINISTIC_EFFECT_IDS), 19)

    def test_is_deterministic_handles_str_int_and_garbage(self):
        from racelink.domain.wled_deterministic import is_deterministic
        self.assertTrue(is_deterministic(2))
        self.assertTrue(is_deterministic("2"))
        self.assertFalse(is_deterministic(7))      # Dynamic — RNG
        self.assertFalse(is_deterministic("64"))   # Juggle — beat pitfall
        self.assertFalse(is_deterministic(None))
        self.assertFalse(is_deterministic("abc"))

    def test_effect_mode_options_tags_breathe_and_blink_deterministic(self):
        from racelink.domain.specials import wled_effect_mode_options
        options = wled_effect_mode_options()
        by_value = {o["value"]: o for o in options}
        # Breathe (2) is the textbook deterministic offset demo.
        self.assertTrue(by_value["2"]["deterministic"])
        self.assertEqual(by_value["2"]["label"], "Breathe")
        # Dynamic (7) is RNG-driven.
        self.assertFalse(by_value["7"]["deterministic"])

    def test_effect_mode_options_sort_deterministic_first(self):
        from racelink.domain.specials import wled_effect_mode_options
        options = wled_effect_mode_options()
        first_non_det_idx = next(
            (i for i, o in enumerate(options) if not o["deterministic"]),
            len(options),
        )
        # All entries before first_non_det_idx must be deterministic.
        for idx, o in enumerate(options[:first_non_det_idx]):
            self.assertTrue(
                o["deterministic"],
                f"option at index {idx} (value={o['value']}, label={o['label']!r}) "
                "should be deterministic but isn't",
            )
        # All entries from first_non_det_idx onward must be non-deterministic.
        for idx, o in enumerate(options[first_non_det_idx:],
                                start=first_non_det_idx):
            self.assertFalse(
                o["deterministic"],
                f"option at index {idx} (value={o['value']}, label={o['label']!r}) "
                "should be non-deterministic but isn't",
            )
        # Sanity: there ARE both groups.
        self.assertGreater(first_non_det_idx, 0)
        self.assertLess(first_non_det_idx, len(options))

    def test_effect_mode_options_preserves_intra_group_order(self):
        # Within the deterministic group, the original WLED order must
        # be preserved (stable sort). Pin the first three IDs from the
        # audited set: 0 (Solid), 1 (Blink), 2 (Breathe). Same check
        # for the non-deterministic tail using IDs known to be the
        # earliest non-deterministic ones in WLED's effect table:
        # 4 (Wipe Random), 5 (Random Colors), 7 (Dynamic).
        from racelink.domain.specials import wled_effect_mode_options
        options = wled_effect_mode_options()
        det_values = [o["value"] for o in options if o["deterministic"]]
        non_det_values = [o["value"] for o in options if not o["deterministic"]]
        self.assertEqual(det_values[:3], ["0", "1", "2"])
        self.assertEqual(non_det_values[:3], ["4", "5", "7"])


class CoerceRespectsAbsentVarsTests(unittest.TestCase):
    """Regression guard: A12 changes coerce_action_params to omit missing vars."""

    def test_missing_vars_are_omitted(self):
        from racelink.services.specials_service import SpecialsService

        svc = SpecialsService(rl_instance=type("RL", (), {"uiPresetList": []})())
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


class ParsePaletteColorRuleTests(unittest.TestCase):
    """Verify ``parse_palette_color_rule`` extracts the palette->color-slot
    rule from WLED's ``updateSelectedPalette()`` and that the generated
    module + schema endpoint surface it unchanged."""

    _STOCK_FN = (
        "function updateSelectedPalette(s)\n"
        "{\n"
        "\tlet cd = gId('csl').children;\n"
        "\tif (s > 1 && s < 6) {\n"
        "\t\tcd[0].classList.remove('hide');\n"
        "\t\tif (s > 2) cd[1].classList.remove('hide');\n"
        "\t\tif (s > 3) cd[2].classList.remove('hide');\n"
        "\t} else {\n"
        "\t\tfor (let i of cd) if (i.dataset.hide == '1') i.classList.add('hide');\n"
        "\t}\n"
        "}\n"
    )

    def test_extracts_stock_thresholds(self):
        rule = parse_palette_color_rule(self._STOCK_FN)
        self.assertEqual(rule["force_slot_min_palette"], [2, 3, 4])
        self.assertEqual(rule["max_palette_id"], 5)

    def test_generated_module_matches_stock_thresholds(self):
        # Pin: the bundled WLED checkout currently produces these values. If a
        # firmware bump shifts them, this assertion fires alongside the
        # parser's own RuntimeError on shape changes -- we want both signals.
        self.assertEqual(WLED_PALETTE_COLOR_RULES["force_slot_min_palette"], [2, 3, 4])
        self.assertEqual(WLED_PALETTE_COLOR_RULES["max_palette_id"], 5)

    def test_schema_serialisation_includes_rule(self):
        schema = serialize_rl_preset_editor_schema()
        self.assertIn("palette_color_rules", schema)
        self.assertEqual(
            schema["palette_color_rules"]["force_slot_min_palette"],
            WLED_PALETTE_COLOR_RULES["force_slot_min_palette"],
        )
        self.assertEqual(
            schema["palette_color_rules"]["max_palette_id"],
            WLED_PALETTE_COLOR_RULES["max_palette_id"],
        )

    def test_missing_function_raises(self):
        with self.assertRaises(RuntimeError):
            parse_palette_color_rule("// no updateSelectedPalette here\n")

    def test_changed_outer_shape_raises(self):
        # Drop the outer 'if (s > LO && s < HI)' guard -- generator must stop.
        broken = self._STOCK_FN.replace("if (s > 1 && s < 6)", "if (s == 99)")
        with self.assertRaises(RuntimeError):
            parse_palette_color_rule(broken)

    def test_changed_slot1_shape_raises(self):
        broken = self._STOCK_FN.replace(
            "if (s > 2) cd[1].classList.remove('hide');", ""
        )
        with self.assertRaises(RuntimeError):
            parse_palette_color_rule(broken)


if __name__ == "__main__":
    unittest.main()
