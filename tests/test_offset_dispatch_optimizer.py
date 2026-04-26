"""Tests for ``offset_dispatch_optimizer.plan_offset_setup``.

The optimizer picks the wire path with the smallest packet count for a
given offset_group container. These tests pin the strategy choices for
the documented edge cases and ensure the per-strategy output stays in
sync with the on-wire builder (body lengths come from
``build_offset_body``).
"""

from __future__ import annotations

import unittest

from racelink.services.offset_dispatch_optimizer import (
    OptimizerPlan,
    plan_offset_setup,
)


class StrategyAllGroupsTests(unittest.TestCase):
    """``groups == "all"`` chooses Strategy A whenever the mode supports it."""

    def test_all_groups_linear_is_single_broadcast(self):
        plan = plan_offset_setup(
            participant_groups="all",
            offset={"mode": "linear", "base_ms": 0, "step_ms": 100},
            known_group_ids=[1, 2, 3, 4, 5],
        )
        self.assertEqual(plan.strategy, "A_broadcast_formula")
        self.assertEqual(plan.packet_count, 1)
        op = plan.ops[0]
        self.assertEqual(op.target_group, 255)
        self.assertEqual(op.payload, {"mode": "linear", "base_ms": 0, "step_ms": 100})

    def test_all_groups_vshape_is_single_broadcast(self):
        plan = plan_offset_setup(
            participant_groups="all",
            offset={"mode": "vshape", "base_ms": 0, "step_ms": 50, "center": 5},
            known_group_ids=list(range(10)),
        )
        self.assertEqual(plan.strategy, "A_broadcast_formula")
        self.assertEqual(plan.packet_count, 1)
        self.assertEqual(plan.ops[0].payload["center"], 5)

    def test_all_groups_modulo_is_single_broadcast(self):
        plan = plan_offset_setup(
            participant_groups="all",
            offset={"mode": "modulo", "base_ms": 0, "step_ms": 100, "cycle": 4},
            known_group_ids=list(range(20)),
        )
        self.assertEqual(plan.strategy, "A_broadcast_formula")
        self.assertEqual(plan.ops[0].payload["cycle"], 4)

    def test_all_groups_none_is_single_broadcast_clear(self):
        plan = plan_offset_setup(
            participant_groups="all",
            offset={"mode": "none"},
            known_group_ids=list(range(10)),
        )
        self.assertEqual(plan.strategy, "A_broadcast_formula")
        self.assertEqual(plan.ops[0].payload, {"mode": "none"})

    def test_all_groups_with_no_known_ids_still_broadcasts(self):
        # Strategy A works even without device discovery — the gateway
        # forwards the broadcast and any device on air picks it up.
        plan = plan_offset_setup(
            participant_groups="all",
            offset={"mode": "linear", "base_ms": 0, "step_ms": 100},
            known_group_ids=[],
        )
        self.assertEqual(plan.strategy, "A_broadcast_formula")


class StrategySparseTests(unittest.TestCase):
    """Sparse selections fall back to per-group EXPLICIT or pick C when
    cheaper."""

    def test_sparse_explicit_emits_per_group_with_persisted_values(self):
        plan = plan_offset_setup(
            participant_groups=[1, 3, 5],
            offset={
                "mode": "explicit",
                "values": [
                    {"id": 1, "offset_ms": 0},
                    {"id": 3, "offset_ms": 100},
                    {"id": 5, "offset_ms": 250},
                ],
            },
            known_group_ids=[1, 2, 3, 4, 5],
        )
        self.assertEqual(plan.strategy, "B_per_group_explicit")
        self.assertEqual(plan.packet_count, 3)
        self.assertEqual(
            [(op.target_group, op.payload["mode"], op.payload["offset_ms"]) for op in plan.ops],
            [(1, "explicit", 0), (3, "explicit", 100), (5, "explicit", 250)],
        )

    def test_sparse_linear_evaluates_host_side_per_group_when_b_wins(self):
        # 2 participants, 4 non-participants in the universe.
        # B: 2 EXPLICIT pkts (4 B each = 8 B). C: 1 LINEAR (6 B) + 4 NONE (2 B
        # each = 8 B) = 5 pkts / 14 B. B wins on packets.
        plan = plan_offset_setup(
            participant_groups=[1, 5],
            offset={"mode": "linear", "base_ms": 0, "step_ms": 100},
            known_group_ids=[1, 2, 3, 4, 5, 6],
        )
        self.assertEqual(plan.strategy, "B_per_group_explicit")
        self.assertEqual(
            [(op.target_group, op.payload["offset_ms"]) for op in plan.ops],
            [(1, 100), (5, 500)],
        )

    def test_sparse_strategy_c_wins_when_non_participants_few(self):
        # 5 participants, only 1 non-participant → Strategy C costs 1 + 1
        # = 2 packets vs Strategy B's 5. C wins.
        plan = plan_offset_setup(
            participant_groups=[1, 2, 3, 4, 5],
            offset={"mode": "linear", "base_ms": 0, "step_ms": 100},
            known_group_ids=[1, 2, 3, 4, 5, 6],
        )
        self.assertEqual(plan.strategy, "C_formula_plus_overrides")
        self.assertEqual(plan.packet_count, 2)
        # First op: broadcast formula
        self.assertEqual(plan.ops[0].target_group, 255)
        self.assertEqual(plan.ops[0].payload["mode"], "linear")
        # Second op: NONE override for the non-participant
        self.assertEqual(plan.ops[1].target_group, 6)
        self.assertEqual(plan.ops[1].payload, {"mode": "none"})

    def test_sparse_strategy_b_wins_when_majority_does_not_participate(self):
        # 2 participants, 8 non-participants → B = 2, C = 1 + 8 = 9 → B wins.
        plan = plan_offset_setup(
            participant_groups=[1, 2],
            offset={"mode": "linear", "base_ms": 0, "step_ms": 100},
            known_group_ids=list(range(10)),
        )
        self.assertEqual(plan.strategy, "B_per_group_explicit")
        self.assertEqual(plan.packet_count, 2)

    def test_sparse_none_mode_emits_per_group_clear(self):
        # Sparse + mode=none: deactivate exactly the listed groups.
        # B mode is overridden so each entry sends a NONE clear (not EXPLICIT 0).
        plan = plan_offset_setup(
            participant_groups=[1, 3],
            offset={"mode": "none"},
            known_group_ids=[1, 2, 3, 4, 5],
        )
        self.assertEqual(plan.strategy, "B_per_group_explicit")
        self.assertEqual(plan.packet_count, 2)
        for op in plan.ops:
            self.assertEqual(op.payload, {"mode": "none"})

    def test_sparse_without_known_devices_still_emits_explicit(self):
        # Even without a device list, the host emits per-group EXPLICIT for
        # the configured groups. (Strategy C requires device discovery to
        # know who the non-participants are.)
        plan = plan_offset_setup(
            participant_groups=[1, 3],
            offset={"mode": "linear", "base_ms": 0, "step_ms": 100},
            known_group_ids=[],
        )
        self.assertEqual(plan.strategy, "B_per_group_explicit")
        self.assertEqual(plan.packet_count, 2)


class TieBreakTests(unittest.TestCase):
    def test_packet_tie_picks_smaller_bytes(self):
        # B and C tied on packets (3 each). C uses one broadcast LINEAR
        # body (6 B) + one NONE body (2 B) = 8 B; B uses three EXPLICIT
        # bodies (4 B each) = 12 B. Wait — that means C is *smaller*
        # bytes; let's verify the comparator picks C.
        plan = plan_offset_setup(
            participant_groups=[1, 2, 3],
            offset={"mode": "linear", "base_ms": 0, "step_ms": 100},
            known_group_ids=[1, 2, 3, 4, 5],  # 2 non-participants
        )
        # B: 3 pkts × 4 B  = 12 B
        # C: 1 LINEAR (6 B) + 2 NONE (2 B each) = 10 B
        # → both at 3 pkts, C is smaller bytes, C wins.
        self.assertEqual(plan.strategy, "C_formula_plus_overrides")


class ResultShapeTests(unittest.TestCase):
    def test_optimizer_plan_exposes_packets_and_bytes(self):
        plan = plan_offset_setup(
            participant_groups="all",
            offset={"mode": "linear", "base_ms": 0, "step_ms": 100},
            known_group_ids=[],
        )
        self.assertIsInstance(plan, OptimizerPlan)
        self.assertEqual(plan.packet_count, 1)
        # body_bytes mirrors the on-wire builder: LINEAR body = 1 (groupId)
        # + 1 (mode) + 2 (int16 base) + 2 (int16 step) = 6 B
        self.assertEqual(plan.total_bytes, 6)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
