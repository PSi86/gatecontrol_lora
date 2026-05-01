# ruff: noqa: S101
"""Parity tests — the **structural guarantee** that the cost
estimator's prediction and the scene runner's wire emission are
always in lockstep.

Every test here:

1. Runs an action through ``plan_action_dispatch`` to get the
   reference plan.
2. Calls ``estimate_action`` on the same input and asserts
   ``cost.packets == plan.packet_count``.
3. Runs the action through ``SceneRunnerService`` with a
   recording control_service / sync_service / startblock sender
   and asserts the recorded send sequence matches the plan
   one-for-one (length, sender method name, target group / device).

If any of these three diverge for ANY action shape, the contract
is violated. The user's "make sure the estimator really uses the
same code as the scene execution" requirement is enforced by the
fact that all three paths share ``plan_action_dispatch``.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from typing import Any, Dict, List

from racelink.services.dispatch_planner import plan_action_dispatch
from racelink.services.scene_cost_estimator import estimate_action
from racelink.services.scenes_service import (
    KIND_OFFSET_GROUP,
    KIND_RL_PRESET,
    KIND_STARTBLOCK,
    KIND_SYNC,
    KIND_WLED_CONTROL,
    KIND_WLED_PRESET,
    SceneService,
)
from racelink.services.scene_runner_service import SceneRunnerService


# ---- recording fakes --------------------------------------------------


class _FakeDevice:
    def __init__(self, addr, group_id=4):
        self.addr = addr
        self.groupId = group_id


class _FakeController:
    """Stub controller with a tiny device repository."""
    def __init__(self, devices=None):
        self._devices = {d.addr.upper(): d for d in (devices or [])}
        # device_repository.list() shape — used by runner's
        # _known_group_ids() and the parity device_lookup.
        self.device_repository = type("Repo", (), {
            "list": lambda self_: list(self._devices.values()),
        })()
        self.device_repository._devices = self._devices  # for closure
        self.startblock_calls: List[Dict[str, Any]] = []

    def getDeviceFromAddress(self, addr):
        if not addr:
            return None
        return self._devices.get(str(addr).upper())

    def sendStartblockControl(self, *, targetDevice=None, targetGroup=None, params=None):
        self.startblock_calls.append({
            "sender": "send_startblock",
            "targetDevice": getattr(targetDevice, "addr", None),
            "targetGroup": targetGroup,
            "params": dict(params or {}),
        })
        return True


class _RecordingControlService:
    """Captures every send_*; returns truthy so all_ok stays True."""
    def __init__(self):
        self.calls: List[Dict[str, Any]] = []

    def send_offset(self, *, targetDevice=None, targetGroup=None, mode="none", **mode_params):
        self.calls.append({
            "sender": "send_offset",
            "targetDevice": getattr(targetDevice, "addr", None),
            "targetGroup": targetGroup,
            "mode": mode,
            "params": dict(mode_params),
        })
        return True

    def send_wled_control(self, *, targetDevice=None, targetGroup=None, params=None):
        self.calls.append({
            "sender": "send_wled_control",
            "targetDevice": getattr(targetDevice, "addr", None),
            "targetGroup": targetGroup,
            "params": dict(params or {}),
        })
        return True

    def send_wled_preset(self, *, targetDevice=None, targetGroup=None, params=None):
        self.calls.append({
            "sender": "send_wled_preset",
            "targetDevice": getattr(targetDevice, "addr", None),
            "targetGroup": targetGroup,
            "params": dict(params or {}),
        })
        return True


class _RecordingSyncService:
    def __init__(self):
        self.calls: List[Dict[str, Any]] = []

    def send_sync(self, ts24, brightness, recv3=b"\xFF\xFF\xFF", *, trigger_armed=False):
        self.calls.append({
            "sender": "send_sync",
            "ts24": ts24, "brightness": brightness,
            "trigger_armed": trigger_armed,
        })


class _StubRlPresets:
    def __init__(self, presets):
        self._by_key = {p["key"]: p for p in presets}
        self._by_id = {p["id"]: p for p in presets}

    def get(self, key):
        return dict(self._by_key[key]) if key in self._by_key else None

    def get_by_id(self, pid):
        return dict(self._by_id[pid]) if pid in self._by_id else None


def _rl_preset_lookup(presets):
    """Same closure shape the planner expects, for estimator parity."""
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


# ---- harness ---------------------------------------------------------


def _run_action(action, *, devices=None, presets=None):
    """Save the action as a one-action scene, run it via SceneRunner,
    return (control_calls, sync_calls, startblock_calls)."""
    tmp = tempfile.TemporaryDirectory()
    try:
        scenes = SceneService(storage_path=os.path.join(tmp.name, "scenes.json"))
        scenes.create(label="Parity", actions=[action])
        controller = _FakeController(devices=devices or [])
        ctrl = _RecordingControlService()
        sync = _RecordingSyncService()
        rl_presets = _StubRlPresets(presets or [])
        runner = SceneRunnerService(
            controller=controller,
            scenes_service=scenes,
            control_service=ctrl,
            sync_service=sync,
            rl_presets_service=rl_presets,
            sleep=lambda s: None,
            clock_ms=lambda: 0,
        )
        result = runner.run("parity")
    finally:
        tmp.cleanup()
    return result, ctrl.calls + sync.calls + controller.startblock_calls


def _planner_inputs(*, devices=None, presets=None):
    """Match the inputs ``_run_action`` would provide to the planner."""
    devices = devices or []
    known_group_ids = sorted({d.groupId for d in devices
                              if isinstance(getattr(d, "groupId", None), int)})

    def device_lookup(addr):
        for d in devices:
            if d.addr.upper() == str(addr).upper():
                return d
        return None

    return {
        "known_group_ids": known_group_ids,
        "rl_preset_lookup": _rl_preset_lookup(presets or []),
        "device_lookup": device_lookup,
    }


def _assert_parity(test_case, action, *, devices=None, presets=None):
    """Run the action through (planner, estimator, runner). Assert all
    three agree on packet count and per-op (sender, target_group)."""
    inputs = _planner_inputs(devices=devices, presets=presets)
    plan = plan_action_dispatch(action, **inputs)
    cost = estimate_action(action, **inputs)
    _, calls = _run_action(action, devices=devices, presets=presets)

    test_case.assertEqual(
        cost.packets, plan.packet_count,
        f"estimator/planner divergence: {cost.packets} vs {plan.packet_count}",
    )
    test_case.assertEqual(
        len(calls), plan.packet_count,
        f"runner/planner divergence: {len(calls)} vs {plan.packet_count}",
    )

    # Per-op (sender, addressing) — the on-wire shape parity. Derive
    # the addressing tuple from the kwargs the runner actually
    # consumes, not from ``WireOp.target_group`` (which exposes the
    # device's pinned gid for device targets). Both forms describe
    # the same wire packet.
    def _addressing_for_plan(op):
        if op.sender == "send_sync":
            return ("sync",)
        if "targetDevice" in op.payload:
            return ("device", op.payload["targetDevice"].addr.upper())
        return ("group", op.payload.get("targetGroup", op.target_group))

    def _addressing_for_call(c):
        if c.get("targetDevice"):
            return ("device", str(c["targetDevice"]).upper())
        if c["sender"] == "send_sync":
            # OPC_SYNC has no group concept on the wire — addressing
            # is recv3=FFFFFF baked into the gateway path.
            return ("sync",)
        return ("group", c.get("targetGroup"))

    plan_seq = [(op.sender, _addressing_for_plan(op)) for op in plan.ops]
    runner_seq = [(c["sender"], _addressing_for_call(c)) for c in calls]
    test_case.assertEqual(
        runner_seq, plan_seq,
        f"runner/planner per-op divergence:\n  plan:   {plan_seq}\n  runner: {runner_seq}",
    )


# ---- parity tests ----------------------------------------------------


class TopLevelKindParityTests(unittest.TestCase):
    """Every top-level effect kind through every target shape."""

    def test_sync_parity(self):
        _assert_parity(self, {"kind": KIND_SYNC})

    def test_wled_preset_broadcast_parity(self):
        _assert_parity(self, {
            "kind": KIND_WLED_PRESET,
            "target": {"kind": "broadcast"},
            "params": {"presetId": 7, "brightness": 200},
        })

    def test_wled_preset_groups_len1_parity(self):
        _assert_parity(self, {
            "kind": KIND_WLED_PRESET,
            "target": {"kind": "groups", "value": [3]},
            "params": {"presetId": 7, "brightness": 200},
        })

    def test_wled_preset_groups_lenN_parity(self):
        _assert_parity(self, {
            "kind": KIND_WLED_PRESET,
            "target": {"kind": "groups", "value": [1, 3, 5]},
            "params": {"presetId": 7, "brightness": 200},
        })

    def test_wled_control_with_full_params_parity(self):
        _assert_parity(self, {
            "kind": KIND_WLED_CONTROL,
            "target": {"kind": "groups", "value": [1, 2]},
            "params": {"mode": 5, "speed": 200, "brightness": 128,
                       "color1": [255, 0, 0]},
        })

    def test_rl_preset_with_lookup_parity(self):
        preset = {
            "key": "fast_red", "id": 42,
            "params": {"mode": 5, "speed": 200, "intensity": 180,
                       "brightness": 220, "palette": 4,
                       "color1": [255, 0, 0]},
            "flags": {"arm_on_sync": True, "force_tt0": False,
                      "force_reapply": False, "offset_mode": False},
        }
        _assert_parity(self, {
            "kind": KIND_RL_PRESET,
            "target": {"kind": "broadcast"},
            "params": {"presetId": 42},
            "flags_override": {"arm_on_sync": True},
        }, presets=[preset])

    def test_device_target_parity(self):
        device = _FakeDevice("AABBCCDDEEFF", group_id=4)
        _assert_parity(self, {
            "kind": KIND_WLED_CONTROL,
            "target": {"kind": "device", "value": "AABBCCDDEEFF"},
            "params": {"mode": 5},
        }, devices=[device])

    def test_startblock_parity(self):
        _assert_parity(self, {
            "kind": KIND_STARTBLOCK,
            "target": {"kind": "broadcast"},
            "params": {"fn_key": "startblock_control"},
        })


class OffsetGroupParityTests(unittest.TestCase):
    """Strategy A / B / C across multiple target shapes — the
    user's-reproducer-class bugs all live here."""

    def _devices_for_groups(self, gids):
        """One device per group id."""
        return [_FakeDevice(f"AA{gid:010X}"[:12], group_id=gid) for gid in gids]

    def test_strategy_a_broadcast_linear_one_child_parity(self):
        _assert_parity(self, {
            "kind": KIND_OFFSET_GROUP,
            "target": {"kind": "broadcast"},
            "offset": {"mode": "linear", "base_ms": 0, "step_ms": 100},
            "actions": [{
                "kind": KIND_WLED_CONTROL,
                "target": {"kind": "broadcast"},
                "params": {"mode": 5},
            }],
        }, devices=self._devices_for_groups([1, 2, 3, 4, 5]))

    def test_user_7_of_10_sparse_linear_strategy_c_parity(self):
        """The user's reproducer scene. After Phase A bug fix +
        structural sync, estimator and runner both emit 5 packets:
        1 broadcast OPC_OFFSET (linear formula) + 3 NONE-overrides
        for the 3 non-participants + 1 broadcast child."""
        preset = {
            "key": "p", "id": 1,
            "params": {"mode": 5},
            "flags": {"arm_on_sync": False, "force_tt0": False,
                      "force_reapply": False, "offset_mode": False},
        }
        _assert_parity(self, {
            "kind": KIND_OFFSET_GROUP,
            "target": {"kind": "groups", "value": [1, 2, 3, 4, 5, 6, 7]},
            "offset": {"mode": "linear", "base_ms": 0, "step_ms": 100},
            "actions": [{
                "kind": KIND_RL_PRESET,
                "target": {"kind": "broadcast"},
                "params": {"presetId": 1},
            }],
        },
            devices=self._devices_for_groups(list(range(1, 11))),
            presets=[preset])

    def test_strategy_b_majority_sparse_parity(self):
        """2-of-10 sparse → Strategy B (per-group EXPLICIT) wins."""
        _assert_parity(self, {
            "kind": KIND_OFFSET_GROUP,
            "target": {"kind": "groups", "value": [1, 2]},
            "offset": {"mode": "linear", "base_ms": 0, "step_ms": 100},
            "actions": [],
        }, devices=self._devices_for_groups(list(range(1, 11))))

    def test_offset_group_with_groups_child_parity(self):
        """Container = broadcast (Strategy A); child = groups[2]
        means 2 child packets (group-scoped broadcast). 1 OFFSET +
        2 child = 3 total."""
        _assert_parity(self, {
            "kind": KIND_OFFSET_GROUP,
            "target": {"kind": "broadcast"},
            "offset": {"mode": "linear", "base_ms": 0, "step_ms": 100},
            "actions": [{
                "kind": KIND_WLED_CONTROL,
                "target": {"kind": "groups", "value": [1, 3]},
                "params": {"mode": 5},
            }],
        }, devices=self._devices_for_groups([1, 2, 3, 4, 5]))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
