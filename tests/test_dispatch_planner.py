# ruff: noqa: S101
"""Unit tests for ``racelink/services/dispatch_planner.py``.

Pure-function tests that pin the planner's per-kind output for every
target shape and every offset_group strategy. The companion
``test_dispatch_parity.py`` then asserts that the runner emits — and
the estimator predicts — exactly what the planner says.
"""

from __future__ import annotations

import unittest

from racelink.services.dispatch_planner import (
    ActionDispatchPlan,
    plan_action_dispatch,
)
from racelink.services.scenes_service import (
    KIND_DELAY,
    KIND_OFFSET_GROUP,
    KIND_RL_PRESET,
    KIND_STARTBLOCK,
    KIND_SYNC,
    KIND_WLED_CONTROL,
    KIND_WLED_PRESET,
)


class _FakeDevice:
    def __init__(self, addr, group_id=4):
        self.addr = addr
        self.groupId = group_id


def _device_lookup_for(addr_to_groupid):
    """Build a closure mimicking ``controller.getDeviceFromAddress``."""
    table = {a.upper(): _FakeDevice(a, gid) for a, gid in addr_to_groupid.items()}
    def lookup(addr):
        return table.get(str(addr).upper())
    return lookup


def _rl_preset_lookup(presets):
    """Build a closure mimicking the runner's preset-store lookup."""
    by_key = {p["key"]: p for p in presets}
    by_id = {p["id"]: p for p in presets}
    def lookup(ref):
        if isinstance(ref, str) and ref.startswith("RL:"):
            return by_key.get(ref[3:])
        if isinstance(ref, int):
            return by_id.get(ref)
        if isinstance(ref, str):
            return by_id.get(int(ref)) if ref.isdigit() else by_key.get(ref)
        return None
    return lookup


class SyncAndDelayTests(unittest.TestCase):
    """Trivial kinds — ``sync`` emits one OPC_SYNC; ``delay`` emits
    no wire packets at all."""

    def test_sync_plans_one_opc_sync_with_trigger_armed_5b_body(self):
        plan = plan_action_dispatch({"kind": KIND_SYNC}, known_group_ids=[])
        self.assertEqual(plan.kind, KIND_SYNC)
        self.assertEqual(len(plan.ops), 1)
        op = plan.ops[0]
        self.assertEqual(op.opcode, "OPC_SYNC")
        self.assertEqual(op.sender, "send_sync")
        # Runner sets trigger_armed=True → 5 B body (4 B base + 1 B flags).
        self.assertEqual(op.body_bytes, 5)
        self.assertTrue(op.payload["trigger_armed"])

    def test_delay_plans_zero_ops_with_duration_detail(self):
        plan = plan_action_dispatch(
            {"kind": KIND_DELAY, "duration_ms": 750},
            known_group_ids=[],
        )
        self.assertEqual(plan.kind, KIND_DELAY)
        self.assertEqual(plan.ops, [])
        self.assertEqual(plan.detail["duration_ms"], 750.0)


class TopLevelTargetShapeTests(unittest.TestCase):
    """Every per-group effect kind × every target shape."""

    def test_broadcast_emits_one_op_with_target_group_255(self):
        plan = plan_action_dispatch(
            {
                "kind": KIND_WLED_CONTROL,
                "target": {"kind": "broadcast"},
                "params": {"mode": 5},
            },
            known_group_ids=[1, 2, 3],
        )
        self.assertEqual(len(plan.ops), 1)
        op = plan.ops[0]
        self.assertEqual(op.target_group, 255)
        self.assertEqual(op.sender, "send_wled_control")
        self.assertEqual(op.payload["targetGroup"], 255)
        self.assertNotIn("targetDevice", op.payload)

    def test_groups_len1_emits_one_op_to_that_group(self):
        plan = plan_action_dispatch(
            {
                "kind": KIND_WLED_PRESET,
                "target": {"kind": "groups", "value": [3]},
                "params": {"presetId": 7, "brightness": 200},
            },
            known_group_ids=[1, 2, 3],
        )
        self.assertEqual(len(plan.ops), 1)
        op = plan.ops[0]
        self.assertEqual(op.target_group, 3)
        self.assertEqual(op.opcode, "OPC_PRESET")
        self.assertEqual(op.sender, "send_wled_preset")

    def test_groups_lenN_emits_one_op_per_group(self):
        plan = plan_action_dispatch(
            {
                "kind": KIND_WLED_CONTROL,
                "target": {"kind": "groups", "value": [1, 3, 5]},
                "params": {"mode": 5},
            },
            known_group_ids=[1, 2, 3, 4, 5],
        )
        self.assertEqual(len(plan.ops), 3)
        self.assertEqual([op.target_group for op in plan.ops], [1, 3, 5])

    def test_device_target_resolves_via_lookup(self):
        plan = plan_action_dispatch(
            {
                "kind": KIND_WLED_CONTROL,
                "target": {"kind": "device", "value": "AABBCCDDEEFF"},
                "params": {"mode": 5},
            },
            known_group_ids=[1],
            device_lookup=_device_lookup_for({"AABBCCDDEEFF": 4}),
        )
        self.assertEqual(len(plan.ops), 1)
        op = plan.ops[0]
        # WireOp.target_group reflects the device's stored gid (the
        # "single-device pinned rule" — see broadcast-ruleset.md).
        self.assertEqual(op.target_group, 4)
        self.assertIn("targetDevice", op.payload)
        self.assertEqual(op.payload["targetDevice"].addr, "AABBCCDDEEFF")

    def test_device_target_unresolved_degrades(self):
        plan = plan_action_dispatch(
            {
                "kind": KIND_WLED_CONTROL,
                "target": {"kind": "device", "value": "DEADBEEFCAFE"},
                "params": {},
            },
            known_group_ids=[],
            device_lookup=lambda addr: None,  # never resolves
        )
        self.assertTrue(plan.degraded)
        self.assertEqual(plan.ops, [])
        self.assertEqual(plan.error, "target_not_found")

    def test_rl_preset_uses_lookup_to_size_full_body(self):
        preset = {
            "key": "fast_red", "id": 42,
            "params": {"mode": 5, "speed": 200, "intensity": 180,
                       "brightness": 220, "palette": 4,
                       "color1": [255, 0, 0]},
            "flags": {"arm_on_sync": False, "force_tt0": False,
                      "force_reapply": False, "offset_mode": False},
        }
        plan = plan_action_dispatch(
            {
                "kind": KIND_RL_PRESET,
                "target": {"kind": "broadcast"},
                "params": {"presetId": "fast_red"},
            },
            known_group_ids=[1, 2, 3],
            rl_preset_lookup=_rl_preset_lookup([preset]),
        )
        self.assertEqual(len(plan.ops), 1)
        # Full preset materialises → body > the bare 3-byte minimum.
        self.assertGreater(plan.ops[0].body_bytes, 3)
        # preset_key forwarded to detail.
        self.assertEqual(plan.detail.get("preset_key"), "fast_red")

    def test_rl_preset_unknown_ref_degrades(self):
        plan = plan_action_dispatch(
            {
                "kind": KIND_RL_PRESET,
                "target": {"kind": "broadcast"},
                "params": {"presetId": "ghost"},
            },
            known_group_ids=[],
            rl_preset_lookup=_rl_preset_lookup([]),
        )
        self.assertTrue(plan.degraded)
        self.assertIn("preset_not_found", plan.error or "")

    def test_startblock_marks_op_approximate(self):
        plan = plan_action_dispatch(
            {
                "kind": KIND_STARTBLOCK,
                "target": {"kind": "broadcast"},
                "params": {"fn_key": "startblock_control"},
            },
            known_group_ids=[1],
        )
        self.assertEqual(len(plan.ops), 1)
        op = plan.ops[0]
        self.assertEqual(op.sender, "send_startblock")
        self.assertTrue(op.detail.get("approximate"))


class OffsetGroupStrategyTests(unittest.TestCase):
    """Phase 1 strategies through the planner — Strategy A (broadcast
    formula), B (per-group EXPLICIT), C (broadcast formula + sparse
    NONE overrides)."""

    def _container(self, *, target, mode="linear", base=0, step=100, children=None):
        offset = {"mode": mode, "base_ms": base, "step_ms": step}
        if mode == "none":
            offset = {"mode": "none"}
        return {
            "kind": KIND_OFFSET_GROUP,
            "target": target,
            "offset": offset,
            "actions": children or [],
        }

    def test_strategy_a_broadcast_linear_with_one_child(self):
        plan = plan_action_dispatch(
            self._container(
                target={"kind": "broadcast"},
                children=[{
                    "kind": KIND_WLED_CONTROL,
                    "target": {"kind": "broadcast"},
                    "params": {"mode": 5},
                }],
            ),
            known_group_ids=[1, 2, 3, 4, 5],
        )
        # 1 OPC_OFFSET (broadcast formula) + 1 child = 2 ops.
        self.assertEqual(plan.detail["wire_path"], "A_broadcast_formula")
        self.assertEqual(len(plan.ops), 2)
        offset_ops = [op for op in plan.ops if op.detail.get("phase") == "offset"]
        child_ops = [op for op in plan.ops if op.detail.get("phase") == "child"]
        self.assertEqual(len(offset_ops), 1)
        self.assertEqual(len(child_ops), 1)
        self.assertEqual(offset_ops[0].target_group, 255)

    def test_user_7_of_10_sparse_linear_strategy_c(self):
        """The user's reproducer: 7-of-10 sparse linear offset_group
        with one broadcast child. Strategy C wins on packet count
        (1 broadcast formula + 3 NONE-overrides + 1 child = 5)."""
        plan = plan_action_dispatch(
            self._container(
                target={"kind": "groups", "value": [1, 2, 3, 4, 5, 6, 7]},
                children=[{
                    "kind": KIND_RL_PRESET,
                    "target": {"kind": "broadcast"},
                    "params": {"presetId": "p"},
                }],
            ),
            known_group_ids=list(range(1, 11)),
            rl_preset_lookup=_rl_preset_lookup([{
                "key": "p", "id": 1, "params": {"mode": 5},
                "flags": {"arm_on_sync": False, "force_tt0": False,
                          "force_reapply": False, "offset_mode": False},
            }]),
        )
        self.assertEqual(plan.detail["wire_path"], "C_formula_plus_overrides")
        self.assertEqual(len(plan.ops), 5)  # 1 broadcast + 3 NONE + 1 child

    def test_strategy_b_per_group_explicit(self):
        # 2-of-10 sparse → C cost = 1 + 8 = 9; B cost = 2. B wins.
        plan = plan_action_dispatch(
            self._container(
                target={"kind": "groups", "value": [1, 2]},
                children=[],
            ),
            known_group_ids=list(range(1, 11)),
        )
        self.assertEqual(plan.detail["wire_path"], "B_per_group_explicit")

    def test_force_offset_flag_propagates_to_child_params(self):
        """A formula-mode container forces ``offset_mode=True`` on
        every child packet — this is the firmware-side gate's
        F=1 + E=1 acceptance path. The planner mirrors the runner's
        ``_dispatch_offset_group_child`` exactly."""
        plan = plan_action_dispatch(
            self._container(
                target={"kind": "broadcast"},
                children=[{
                    "kind": KIND_WLED_CONTROL,
                    "target": {"kind": "broadcast"},
                    "params": {"mode": 5},
                }],
            ),
            known_group_ids=[1, 2, 3],
        )
        child_ops = [op for op in plan.ops if op.detail.get("phase") == "child"]
        self.assertEqual(len(child_ops), 1)
        self.assertTrue(child_ops[0].payload["params"].get("offset_mode"))

    def test_mode_none_clears_force_offset_flag(self):
        """``mode=none`` containers force ``offset_mode=False`` on
        children — the gate's F=0 + E=NONE always-accept path."""
        plan = plan_action_dispatch(
            self._container(
                target={"kind": "broadcast"},
                mode="none",
                children=[{
                    "kind": KIND_WLED_CONTROL,
                    "target": {"kind": "broadcast"},
                    "params": {"mode": 5},
                }],
            ),
            known_group_ids=[1, 2, 3],
        )
        child_ops = [op for op in plan.ops if op.detail.get("phase") == "child"]
        # offset_mode flag stripped (False is the implicit default).
        self.assertNotIn("offset_mode", child_ops[0].payload["params"])


class PlanShapeTests(unittest.TestCase):
    """The planner's output dataclass shape — packet_count,
    total_bytes, detail fields used by the runner / estimator."""

    def test_action_dispatch_plan_aggregates(self):
        plan = ActionDispatchPlan(
            kind="x",
            ops=[],
            detail={"wire_path": "A_broadcast_formula"},
        )
        self.assertEqual(plan.packet_count, 0)
        self.assertEqual(plan.total_bytes, 0)
        self.assertFalse(plan.degraded)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
