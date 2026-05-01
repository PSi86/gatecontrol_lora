"""Tests for the host-side offset formula evaluator.

The C++ counterpart in ``RaceLink_WLED/racelink_wled.cpp``
(``computeOffsetMs``) must produce byte-identical results — these tests
lock the contract for both sides so a future change to either has to update
both. The cases here cover every mode plus the documented edge cases:
clamping to [0, 65535], signed step, modulo cycle=0 → 1, etc.
"""

from __future__ import annotations

import unittest

from racelink.domain.offset_formula import (
    OFFSET_MS_MAX,
    OFFSET_MS_MIN,
    evaluate_for_groups,
    evaluate_offset_ms,
)


class EvaluateOffsetMsTests(unittest.TestCase):
    def test_none_always_returns_zero(self):
        for gid in (0, 1, 100, 254):
            self.assertEqual(evaluate_offset_ms({"mode": "none"}, gid), 0)

    def test_explicit_clamps_to_uint16(self):
        self.assertEqual(evaluate_offset_ms({"mode": "explicit", "offset_ms": 0}, 7), 0)
        self.assertEqual(evaluate_offset_ms({"mode": "explicit", "offset_ms": 12345}, 7), 12345)
        self.assertEqual(evaluate_offset_ms({"mode": "explicit", "offset_ms": -10}, 7), OFFSET_MS_MIN)
        self.assertEqual(
            evaluate_offset_ms({"mode": "explicit", "offset_ms": 0x20000}, 7),
            OFFSET_MS_MAX,
        )

    def test_linear_uses_groupid_directly(self):
        spec = {"mode": "linear", "base_ms": 0, "step_ms": 100}
        self.assertEqual(evaluate_offset_ms(spec, 0), 0)
        self.assertEqual(evaluate_offset_ms(spec, 1), 100)
        self.assertEqual(evaluate_offset_ms(spec, 5), 500)

    def test_linear_with_base(self):
        spec = {"mode": "linear", "base_ms": 1000, "step_ms": 50}
        self.assertEqual(evaluate_offset_ms(spec, 0), 1000)
        self.assertEqual(evaluate_offset_ms(spec, 4), 1200)

    def test_linear_signed_step_clamps_to_zero(self):
        # Reverse cascade: base=200, step=-50 → group 0 → 200, group 4 → 0,
        # group 5 → -50 → clamps to 0.
        spec = {"mode": "linear", "base_ms": 200, "step_ms": -50}
        self.assertEqual(evaluate_offset_ms(spec, 0), 200)
        self.assertEqual(evaluate_offset_ms(spec, 4), 0)
        self.assertEqual(evaluate_offset_ms(spec, 5), 0)

    def test_linear_overflow_clamps_to_uint16_max(self):
        spec = {"mode": "linear", "base_ms": 0, "step_ms": 1000}
        self.assertEqual(evaluate_offset_ms(spec, 100), 65535)  # 100*1000 = 100000 → clamp

    def test_vshape_symmetry(self):
        spec = {"mode": "vshape", "base_ms": 0, "step_ms": 100, "center": 5}
        # Center fires first.
        self.assertEqual(evaluate_offset_ms(spec, 5), 0)
        # Symmetric: distance 1 either way → same offset.
        self.assertEqual(evaluate_offset_ms(spec, 4), evaluate_offset_ms(spec, 6))
        self.assertEqual(evaluate_offset_ms(spec, 4), 100)
        # Distance 3 → 300 ms.
        self.assertEqual(evaluate_offset_ms(spec, 8), 300)
        self.assertEqual(evaluate_offset_ms(spec, 2), 300)

    def test_modulo_repeats(self):
        spec = {"mode": "modulo", "base_ms": 0, "step_ms": 100, "cycle": 4}
        # Phase = groupId % 4 → 0, 1, 2, 3, 0, 1, ...
        self.assertEqual(evaluate_offset_ms(spec, 0), 0)
        self.assertEqual(evaluate_offset_ms(spec, 1), 100)
        self.assertEqual(evaluate_offset_ms(spec, 2), 200)
        self.assertEqual(evaluate_offset_ms(spec, 3), 300)
        self.assertEqual(evaluate_offset_ms(spec, 4), 0)
        self.assertEqual(evaluate_offset_ms(spec, 5), 100)

    def test_modulo_cycle_zero_treated_as_one(self):
        # Documented behaviour: cycle=0 collapses to cycle=1 → all groups get base_ms.
        spec = {"mode": "modulo", "base_ms": 50, "step_ms": 100, "cycle": 0}
        for gid in (0, 1, 5, 100):
            self.assertEqual(evaluate_offset_ms(spec, gid), 50)

    def test_unknown_mode_raises(self):
        with self.assertRaises(ValueError):
            evaluate_offset_ms({"mode": "log2"}, 0)


class EvaluateForGroupsTests(unittest.TestCase):
    def test_per_group_list_in_input_order(self):
        spec = {"mode": "linear", "base_ms": 0, "step_ms": 100}
        result = evaluate_for_groups(spec, [3, 1, 5])
        self.assertEqual(result, [
            {"id": 3, "offset_ms": 300},
            {"id": 1, "offset_ms": 100},
            {"id": 5, "offset_ms": 500},
        ])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
