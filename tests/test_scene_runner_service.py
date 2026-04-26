"""Tests for SceneRunnerService — sequential dispatch per action kind."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import MagicMock

from racelink.services.scenes_service import (
    KIND_DELAY,
    KIND_RL_PRESET,
    KIND_STARTBLOCK,
    KIND_SYNC,
    KIND_WLED_CONTROL,
    KIND_WLED_PRESET,
    SceneService,
)
from racelink.services.scene_runner_service import SceneRunnerService


class _FakeDevice:
    def __init__(self, addr, group_id=1):
        self.addr = addr
        self.groupId = group_id


class _FakeController:
    """Stub controller exposing the surface the runner needs."""

    def __init__(self, devices=None):
        self._devices = {d.addr.upper(): d for d in (devices or [])}
        self.startblock_calls = []

    def getDeviceFromAddress(self, addr):
        if not addr:
            return None
        return self._devices.get(str(addr).upper())

    def sendStartblockControl(self, *, targetDevice=None, targetGroup=None, params=None):
        self.startblock_calls.append({
            "targetDevice": getattr(targetDevice, "addr", None),
            "targetGroup": targetGroup,
            "params": dict(params or {}),
        })
        return True


class _RecordingControlService:
    """Captures every send_* call so tests can assert on routing + flags."""

    def __init__(self, *, fail_kinds=(), fail_offsets_for=()):
        self.preset_calls = []
        self.control_calls = []
        self.offset_calls = []
        self._fail_kinds = set(fail_kinds)
        # set of group ids whose send_offset should return False
        self._fail_offsets_for = set(int(g) for g in fail_offsets_for)

    def send_wled_preset(self, *, targetDevice=None, targetGroup=None, params=None):
        self.preset_calls.append({
            "targetDevice": getattr(targetDevice, "addr", None),
            "targetGroup": targetGroup,
            "params": dict(params or {}),
        })
        return "wled_preset" not in self._fail_kinds

    def send_wled_control(self, *, targetDevice=None, targetGroup=None, params=None):
        self.control_calls.append({
            "targetDevice": getattr(targetDevice, "addr", None),
            "targetGroup": targetGroup,
            "params": dict(params or {}),
        })
        return "wled_control" not in self._fail_kinds

    def send_offset(self, *, targetDevice=None, targetGroup=None, mode="none", **mode_params):
        self.offset_calls.append({
            "targetDevice": getattr(targetDevice, "addr", None),
            "targetGroup": targetGroup,
            "mode": mode,
            "params": dict(mode_params),
        })
        if targetGroup is not None and int(targetGroup) in self._fail_offsets_for:
            return False
        return True


class _RecordingSyncService:
    def __init__(self):
        self.sync_calls = []

    def send_sync(self, ts24, brightness, recv3=b"\xFF\xFF\xFF"):
        self.sync_calls.append({"ts24": ts24, "brightness": brightness, "recv3": recv3})


class _StubRlPresets:
    def __init__(self, presets):
        self._by_key = {p["key"]: p for p in presets}
        self._by_id = {p["id"]: p for p in presets}

    def get(self, key):
        return dict(self._by_key[key]) if key in self._by_key else None

    def get_by_id(self, pid):
        return dict(self._by_id[pid]) if pid in self._by_id else None


def _make_runner(*, scenes, control_service=None, sync_service=None,
                 rl_presets=None, devices=None, fake_clock=None, fake_sleep=None):
    controller = _FakeController(devices=devices)
    return SceneRunnerService(
        controller=controller,
        scenes_service=scenes,
        control_service=control_service or _RecordingControlService(),
        sync_service=sync_service or _RecordingSyncService(),
        rl_presets_service=rl_presets,
        sleep=fake_sleep or (lambda s: None),
        clock_ms=fake_clock or (lambda: 0),
    ), controller


class _SceneFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.scenes = SceneService(storage_path=os.path.join(self._tmp.name, "scenes.json"))


class SceneRunnerDispatchTests(_SceneFixture):
    def test_unknown_scene_returns_error(self):
        runner, _ = _make_runner(scenes=self.scenes)
        result = runner.run("nope")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "scene_not_found")
        self.assertEqual(result.actions, [])

    def test_rl_preset_dispatches_via_send_wled_control(self):
        rl = _StubRlPresets([{
            "id": 7,
            "key": "start_red",
            "label": "Start Red",
            "params": {"mode": 1, "brightness": 50, "color1": [255, 0, 0]},
            "flags": {"arm_on_sync": False, "force_tt0": False, "force_reapply": False, "offset_mode": False},
        }])
        self.scenes.create(label="Run", actions=[{
            "kind": KIND_RL_PRESET,
            "target": {"kind": "group", "value": 3},
            "params": {"presetId": "start_red", "brightness": 200},
            "flags_override": {"arm_on_sync": True, "offset_mode": True},
        }])

        ctrl = _RecordingControlService()
        runner, _ = _make_runner(scenes=self.scenes, control_service=ctrl, rl_presets=rl)
        result = runner.run("run")

        self.assertTrue(result.ok)
        self.assertEqual(len(ctrl.control_calls), 1)
        call = ctrl.control_calls[0]
        self.assertEqual(call["targetGroup"], 3)
        # action's brightness override beats the persisted preset brightness
        self.assertEqual(call["params"]["brightness"], 200)
        # mode from persisted preset comes through
        self.assertEqual(call["params"]["mode"], 1)
        # flag override merged into params (only True flags propagate)
        self.assertTrue(call["params"]["arm_on_sync"])
        self.assertTrue(call["params"]["offset_mode"])
        # explicitly-False persisted flag stays absent
        self.assertNotIn("force_tt0", call["params"])

    def test_rl_preset_override_wins_over_persisted_true_flag(self):
        """If the preset persists arm_on_sync=True but the action overrides
        it to False, the runner must drop it."""
        rl = _StubRlPresets([{
            "id": 1, "key": "always_armed", "label": "x",
            "params": {"mode": 1},
            "flags": {"arm_on_sync": True, "force_tt0": False, "force_reapply": False, "offset_mode": False},
        }])
        self.scenes.create(label="R", actions=[{
            "kind": KIND_RL_PRESET,
            "target": {"kind": "group", "value": 1},
            "params": {"presetId": "always_armed"},
            "flags_override": {"arm_on_sync": False},
        }])

        ctrl = _RecordingControlService()
        runner, _ = _make_runner(scenes=self.scenes, control_service=ctrl, rl_presets=rl)
        runner.run("r")
        self.assertNotIn("arm_on_sync", ctrl.control_calls[0]["params"])

    def test_rl_preset_persisted_flag_used_when_no_override(self):
        rl = _StubRlPresets([{
            "id": 1, "key": "armed", "label": "x",
            "params": {"mode": 1},
            "flags": {"arm_on_sync": True, "force_tt0": False, "force_reapply": False, "offset_mode": False},
        }])
        self.scenes.create(label="R", actions=[{
            "kind": KIND_RL_PRESET,
            "target": {"kind": "group", "value": 1},
            "params": {"presetId": "armed"},
            "flags_override": {},
        }])
        ctrl = _RecordingControlService()
        runner, _ = _make_runner(scenes=self.scenes, control_service=ctrl, rl_presets=rl)
        runner.run("r")
        self.assertTrue(ctrl.control_calls[0]["params"]["arm_on_sync"])

    def test_rl_preset_unknown_preset_records_error(self):
        rl = _StubRlPresets([])
        self.scenes.create(label="R", actions=[{
            "kind": KIND_RL_PRESET,
            "target": {"kind": "group", "value": 1},
            "params": {"presetId": "missing"},
        }])
        ctrl = _RecordingControlService()
        runner, _ = _make_runner(scenes=self.scenes, control_service=ctrl, rl_presets=rl)
        result = runner.run("r")
        self.assertFalse(result.ok)
        self.assertFalse(result.actions[0].ok)
        self.assertIn("preset_not_found", result.actions[0].error)
        self.assertEqual(ctrl.control_calls, [])

    def test_device_target_resolved_to_object(self):
        rl = _StubRlPresets([{
            "id": 1, "key": "p", "label": "x",
            "params": {"mode": 2}, "flags": {"arm_on_sync": False, "force_tt0": False, "force_reapply": False, "offset_mode": False},
        }])
        self.scenes.create(label="R", actions=[{
            "kind": KIND_RL_PRESET,
            "target": {"kind": "device", "value": "AABBCCDDEEFF"},
            "params": {"presetId": "p"},
        }])
        ctrl = _RecordingControlService()
        device = _FakeDevice("AABBCCDDEEFF")
        runner, _ = _make_runner(
            scenes=self.scenes, control_service=ctrl, rl_presets=rl, devices=[device],
        )
        runner.run("r")
        self.assertEqual(ctrl.control_calls[0]["targetDevice"], "AABBCCDDEEFF")
        self.assertIsNone(ctrl.control_calls[0]["targetGroup"])

    def test_device_target_not_found_marks_action_degraded(self):
        rl = _StubRlPresets([{
            "id": 1, "key": "p", "label": "x",
            "params": {}, "flags": {"arm_on_sync": False, "force_tt0": False, "force_reapply": False, "offset_mode": False},
        }])
        self.scenes.create(label="R", actions=[{
            "kind": KIND_RL_PRESET,
            "target": {"kind": "device", "value": "DEADBEEFCAFE"},
            "params": {"presetId": "p"},
        }])
        ctrl = _RecordingControlService()
        runner, _ = _make_runner(scenes=self.scenes, control_service=ctrl, rl_presets=rl, devices=[])
        result = runner.run("r")
        self.assertFalse(result.ok)
        self.assertTrue(result.actions[0].degraded)
        self.assertEqual(result.actions[0].error, "target_not_found")
        self.assertEqual(ctrl.control_calls, [])

    def test_wled_preset_dispatches_via_send_wled_preset(self):
        self.scenes.create(label="W", actions=[{
            "kind": KIND_WLED_PRESET,
            "target": {"kind": "group", "value": 2},
            "params": {"presetId": 5, "brightness": 128},
            "flags_override": {"force_reapply": True},
        }])
        ctrl = _RecordingControlService()
        runner, _ = _make_runner(scenes=self.scenes, control_service=ctrl)
        runner.run("w")
        self.assertEqual(len(ctrl.preset_calls), 1)
        call = ctrl.preset_calls[0]
        self.assertEqual(call["targetGroup"], 2)
        self.assertEqual(call["params"]["presetId"], 5)
        self.assertTrue(call["params"]["force_reapply"])

    def test_wled_control_dispatches_via_send_wled_control(self):
        self.scenes.create(label="C", actions=[{
            "kind": KIND_WLED_CONTROL,
            "target": {"kind": "group", "value": 4},
            "params": {"mode": 9, "brightness": 200},
            "flags_override": {},
        }])
        ctrl = _RecordingControlService()
        runner, _ = _make_runner(scenes=self.scenes, control_service=ctrl)
        runner.run("c")
        self.assertEqual(len(ctrl.control_calls), 1)
        self.assertEqual(ctrl.control_calls[0]["params"]["mode"], 9)

    def test_startblock_dispatches_via_controller_method(self):
        self.scenes.create(label="S", actions=[{
            "kind": KIND_STARTBLOCK,
            "target": {"kind": "group", "value": 1},
            "params": {"fn_key": "startblock_control"},
        }])
        ctrl = _RecordingControlService()
        runner, controller = _make_runner(scenes=self.scenes, control_service=ctrl)
        result = runner.run("s")
        self.assertTrue(result.ok)
        self.assertEqual(len(controller.startblock_calls), 1)
        self.assertEqual(controller.startblock_calls[0]["params"]["fn_key"], "startblock_control")

    def test_sync_action_emits_one_broadcast(self):
        self.scenes.create(label="Sy", actions=[{"kind": KIND_SYNC}])
        sync = _RecordingSyncService()
        runner, _ = _make_runner(scenes=self.scenes, sync_service=sync, fake_clock=lambda: 12345678)
        result = runner.run("sy")
        self.assertTrue(result.ok)
        self.assertEqual(len(sync.sync_calls), 1)
        # ts24 = lower 24 bits of clock_ms()
        self.assertEqual(sync.sync_calls[0]["ts24"], 12345678 & 0xFFFFFF)
        self.assertEqual(sync.sync_calls[0]["brightness"], 0)

    def test_delay_action_blocks_via_sleep_helper(self):
        self.scenes.create(label="D", actions=[{"kind": KIND_DELAY, "duration_ms": 750}])
        sleeps = []
        runner, _ = _make_runner(scenes=self.scenes, fake_sleep=sleeps.append)
        result = runner.run("d")
        self.assertTrue(result.ok)
        self.assertEqual(sleeps, [0.75])
        self.assertEqual(result.actions[0].detail["requested_ms"], 750)

    def test_sequential_order_preserved_across_kinds(self):
        rl = _StubRlPresets([{
            "id": 1, "key": "p", "label": "x", "params": {},
            "flags": {"arm_on_sync": False, "force_tt0": False, "force_reapply": False, "offset_mode": False},
        }])
        self.scenes.create(label="Mix", actions=[
            {"kind": KIND_RL_PRESET, "target": {"kind": "group", "value": 1},
             "params": {"presetId": "p"}, "flags_override": {"arm_on_sync": True}},
            {"kind": KIND_RL_PRESET, "target": {"kind": "group", "value": 2},
             "params": {"presetId": "p"}, "flags_override": {"arm_on_sync": True}},
            {"kind": KIND_SYNC},
            {"kind": KIND_DELAY, "duration_ms": 100},
            {"kind": KIND_WLED_PRESET, "target": {"kind": "group", "value": 9},
             "params": {"presetId": 11, "brightness": 50}},
        ])
        ctrl = _RecordingControlService()
        sync = _RecordingSyncService()
        sleeps = []
        runner, _ = _make_runner(scenes=self.scenes, control_service=ctrl, sync_service=sync,
                                 rl_presets=rl, fake_sleep=sleeps.append)
        result = runner.run("mix")
        self.assertTrue(result.ok)
        self.assertEqual(len(result.actions), 5)
        self.assertEqual(len(ctrl.control_calls), 2)
        self.assertEqual(len(sync.sync_calls), 1)
        self.assertEqual(sleeps, [0.1])
        self.assertEqual(len(ctrl.preset_calls), 1)
        # Order check via per-action result kinds
        self.assertEqual([a.kind for a in result.actions],
                         [KIND_RL_PRESET, KIND_RL_PRESET, KIND_SYNC, KIND_DELAY, KIND_WLED_PRESET])

    def test_failed_action_does_not_abort_subsequent_actions(self):
        # Make wled_control fail; preceding wled_preset and following sync still run.
        self.scenes.create(label="X", actions=[
            {"kind": KIND_WLED_PRESET, "target": {"kind": "group", "value": 1},
             "params": {"presetId": 1, "brightness": 50}},
            {"kind": KIND_WLED_CONTROL, "target": {"kind": "group", "value": 2},
             "params": {"mode": 5}},
            {"kind": KIND_SYNC},
        ])
        ctrl = _RecordingControlService(fail_kinds={"wled_control"})
        sync = _RecordingSyncService()
        runner, _ = _make_runner(scenes=self.scenes, control_service=ctrl, sync_service=sync)
        result = runner.run("x")
        self.assertFalse(result.ok)
        self.assertTrue(result.actions[0].ok)
        self.assertFalse(result.actions[1].ok)
        self.assertTrue(result.actions[2].ok)
        # All three were attempted
        self.assertEqual(len(ctrl.preset_calls), 1)
        self.assertEqual(len(ctrl.control_calls), 1)
        self.assertEqual(len(sync.sync_calls), 1)


class SceneRunnerLookupTests(_SceneFixture):
    def test_rl_preset_resolves_stable_key_format(self):
        rl = _StubRlPresets([{
            "id": 3, "key": "start_red", "label": "x", "params": {},
            "flags": {"arm_on_sync": False, "force_tt0": False, "force_reapply": False, "offset_mode": False},
        }])
        self.scenes.create(label="R", actions=[{
            "kind": KIND_RL_PRESET,
            "target": {"kind": "group", "value": 1},
            "params": {"presetId": "RL:start_red"},
        }])
        ctrl = _RecordingControlService()
        runner, _ = _make_runner(scenes=self.scenes, control_service=ctrl, rl_presets=rl)
        result = runner.run("r")
        self.assertTrue(result.ok)
        self.assertEqual(len(ctrl.control_calls), 1)

    def test_rl_preset_resolves_integer_id(self):
        rl = _StubRlPresets([{
            "id": 42, "key": "p", "label": "x", "params": {},
            "flags": {"arm_on_sync": False, "force_tt0": False, "force_reapply": False, "offset_mode": False},
        }])
        self.scenes.create(label="R", actions=[{
            "kind": KIND_RL_PRESET,
            "target": {"kind": "group", "value": 1},
            "params": {"presetId": 42},
        }])
        ctrl = _RecordingControlService()
        runner, _ = _make_runner(scenes=self.scenes, control_service=ctrl, rl_presets=rl)
        result = runner.run("r")
        self.assertTrue(result.ok)


class SceneRunnerProgressCallbackTests(_SceneFixture):
    """R7a: per-action progress events fire before each action runs and again
    on completion with the terminal status. The callback is purely additive
    — the SceneRunResult is unchanged with or without it."""

    def test_progress_cb_fires_running_then_terminal_per_action(self):
        self.scenes.create(label="P", actions=[
            {"kind": KIND_WLED_PRESET, "target": {"kind": "group", "value": 1},
             "params": {"presetId": 1, "brightness": 50}},
            {"kind": KIND_SYNC},
            {"kind": KIND_DELAY, "duration_ms": 0},
        ])
        ctrl = _RecordingControlService()
        events = []
        runner, _ = _make_runner(scenes=self.scenes, control_service=ctrl)
        runner.run("p", progress_cb=events.append)

        # Two events per action (running + terminal) in strict order.
        statuses = [(e["index"], e["status"]) for e in events]
        self.assertEqual(statuses, [
            (0, "running"), (0, "ok"),
            (1, "running"), (1, "ok"),
            (2, "running"), (2, "ok"),
        ])
        # scene_key + kind round-trip.
        self.assertTrue(all(e["scene_key"] == "p" for e in events))
        self.assertEqual([e["kind"] for e in events], [
            KIND_WLED_PRESET, KIND_WLED_PRESET,
            KIND_SYNC, KIND_SYNC,
            KIND_DELAY, KIND_DELAY,
        ])
        # Terminal events carry duration_ms; running events do not.
        self.assertNotIn("duration_ms", events[0])
        self.assertIn("duration_ms", events[1])

    def test_progress_cb_terminal_status_reflects_failures_and_degraded(self):
        # action 0: ok wled_preset
        # action 1: error (forced fail)
        # action 2: degraded (device target not in fixture)
        rl = _StubRlPresets([{
            "id": 1, "key": "p", "label": "x", "params": {},
            "flags": {"arm_on_sync": False, "force_tt0": False, "force_reapply": False, "offset_mode": False},
        }])
        self.scenes.create(label="MixErr", actions=[
            {"kind": KIND_WLED_PRESET, "target": {"kind": "group", "value": 1},
             "params": {"presetId": 1, "brightness": 50}},
            {"kind": KIND_WLED_CONTROL, "target": {"kind": "group", "value": 2},
             "params": {"mode": 5}},
            {"kind": KIND_RL_PRESET, "target": {"kind": "device", "value": "AABBCCDDEEFF"},
             "params": {"presetId": "p"}},
        ])
        ctrl = _RecordingControlService(fail_kinds={"wled_control"})
        events = []
        runner, _ = _make_runner(scenes=self.scenes, control_service=ctrl, rl_presets=rl, devices=[])
        runner.run("mixerr", progress_cb=events.append)
        terminals = [e for e in events if e["status"] != "running"]
        self.assertEqual([t["status"] for t in terminals], ["ok", "error", "degraded"])

    def test_progress_cb_exception_does_not_break_run(self):
        self.scenes.create(label="X", actions=[
            {"kind": KIND_SYNC}, {"kind": KIND_SYNC}, {"kind": KIND_SYNC},
        ])
        sync = _RecordingSyncService()
        calls = []

        def boom(payload):
            calls.append(payload["index"])
            if payload["status"] == "running" and payload["index"] == 1:
                raise RuntimeError("listener crash")

        runner, _ = _make_runner(scenes=self.scenes, sync_service=sync)
        result = runner.run("x", progress_cb=boom)
        # All three sync packets still went out — exception did not abort.
        self.assertEqual(len(sync.sync_calls), 3)
        self.assertTrue(result.ok)
        # Callback was invoked for every transition (3 actions × 2 events = 6).
        self.assertEqual(len(calls), 6)

    def test_run_without_progress_cb_unchanged(self):
        """Sanity: omitting progress_cb is the documented baseline behaviour."""
        self.scenes.create(label="B", actions=[{"kind": KIND_SYNC}])
        sync = _RecordingSyncService()
        runner, _ = _make_runner(scenes=self.scenes, sync_service=sync)
        result = runner.run("b")
        self.assertTrue(result.ok)
        self.assertEqual(len(sync.sync_calls), 1)


class OffsetGroupContainerTests(_SceneFixture):
    """Container dispatch for the ``offset_group`` action kind.

    The runner plans the OPC_OFFSET sequence via the optimizer, then
    dispatches each child action with OFFSET_MODE forced on. No OPC_SYNC
    is auto-emitted — scenes use an explicit ``sync`` action.
    """

    def _preset(self, **flags):
        flag_dict = {"arm_on_sync": False, "force_tt0": False, "force_reapply": False, "offset_mode": False}
        flag_dict.update(flags)
        return _StubRlPresets([{
            "id": 1,
            "key": "cascade_red",
            "label": "Cascade Red",
            "params": {"mode": 1, "color1": [255, 0, 0]},
            "flags": flag_dict,
        }])

    def _container(self, *, groups, offset, children):
        return {
            "kind": "offset_group",
            "groups": groups,
            "offset": offset,
            "actions": children,
        }

    def _wled_control_child(self, target=None, params=None):
        return {
            "kind": "wled_control",
            "target": target or {"kind": "scope"},
            "params": params or {"mode": 5, "brightness": 200},
        }

    def test_offset_group_all_groups_linear_uses_broadcast_formula(self):
        """The big win: one broadcast OPC_OFFSET (mode=linear) configures
        every device. Plus one broadcast OPC_CONTROL per child."""
        self.scenes.create(label="LinearAll", actions=[
            self._container(
                groups="all",
                offset={"mode": "linear", "base_ms": 0, "step_ms": 100},
                children=[self._wled_control_child()],
            ),
        ])
        ctrl = _RecordingControlService()
        runner, _ = _make_runner(scenes=self.scenes, control_service=ctrl)
        result = runner.run("linearall")

        self.assertTrue(result.ok)
        # ONE broadcast OPC_OFFSET with formula params.
        self.assertEqual(len(ctrl.offset_calls), 1)
        op = ctrl.offset_calls[0]
        self.assertEqual(op["targetGroup"], 255)
        self.assertEqual(op["mode"], "linear")
        self.assertEqual(op["params"], {"base_ms": 0, "step_ms": 100})
        # Child fires as broadcast control with offset_mode forced on.
        self.assertEqual(len(ctrl.control_calls), 1)
        self.assertEqual(ctrl.control_calls[0]["targetGroup"], 255)
        self.assertTrue(ctrl.control_calls[0]["params"]["offset_mode"])
        # Detail records the optimizer strategy.
        detail = result.actions[0].detail
        self.assertEqual(detail["wire_path"], "A_broadcast_formula")
        self.assertEqual(detail["offset_mode"], "linear")
        self.assertEqual(detail["offset_packet_count"], 1)
        self.assertEqual(len(detail["children"]), 1)
        self.assertTrue(detail["children"][0]["ok"])

    def test_offset_group_sparse_explicit_per_group_then_broadcast_control(self):
        self.scenes.create(label="Cascade", actions=[
            self._container(
                groups=[1, 3, 5],
                offset={
                    "mode": "explicit",
                    "values": [
                        {"id": 1, "offset_ms": 0},
                        {"id": 3, "offset_ms": 100},
                        {"id": 5, "offset_ms": 250},
                    ],
                },
                children=[self._wled_control_child()],
            ),
        ])
        ctrl = _RecordingControlService()
        runner, _ = _make_runner(scenes=self.scenes, control_service=ctrl)
        result = runner.run("cascade")

        self.assertTrue(result.ok)
        # 3 OPC_OFFSET (mode=explicit), one per participating group.
        self.assertEqual(
            [(c["targetGroup"], c["mode"], c["params"].get("offset_ms")) for c in ctrl.offset_calls],
            [(1, "explicit", 0), (3, "explicit", 100), (5, "explicit", 250)],
        )
        # ONE broadcast control via the acceptance gate.
        self.assertEqual(len(ctrl.control_calls), 1)
        self.assertEqual(ctrl.control_calls[0]["targetGroup"], 255)
        detail = result.actions[0].detail
        self.assertEqual(detail["wire_path"], "B_per_group_explicit")

    def test_offset_group_vshape_all_groups_broadcast_formula(self):
        self.scenes.create(label="V", actions=[
            self._container(
                groups="all",
                offset={"mode": "vshape", "base_ms": 0, "step_ms": 50, "center": 8},
                children=[self._wled_control_child()],
            ),
        ])
        ctrl = _RecordingControlService()
        runner, _ = _make_runner(scenes=self.scenes, control_service=ctrl)
        runner.run("v")
        self.assertEqual(len(ctrl.offset_calls), 1)
        self.assertEqual(ctrl.offset_calls[0]["mode"], "vshape")
        self.assertEqual(ctrl.offset_calls[0]["params"],
                         {"base_ms": 0, "step_ms": 50, "center": 8})

    def test_offset_group_sparse_linear_evaluates_host_side(self):
        """Sparse selection with a formula → host evaluates per group and
        emits N OPC_OFFSET (mode=explicit) packets. Optimizer prefers B
        when there are no known devices to compute Strategy C against."""
        self.scenes.create(label="LinearSparse", actions=[
            self._container(
                groups=[1, 5],
                offset={"mode": "linear", "base_ms": 0, "step_ms": 100},
                children=[self._wled_control_child()],
            ),
        ])
        ctrl = _RecordingControlService()
        runner, _ = _make_runner(scenes=self.scenes, control_service=ctrl)
        runner.run("linearsparse")
        self.assertEqual(
            [(c["targetGroup"], c["mode"], c["params"].get("offset_ms")) for c in ctrl.offset_calls],
            [(1, "explicit", 100), (5, "explicit", 500)],
        )

    def test_offset_group_forces_offset_mode_on_children(self):
        """OFFSET_MODE flag is forced on regardless of child flags_override
        — the wire-level acceptance gate depends on it being present."""
        self.scenes.create(label="X", actions=[
            self._container(
                groups="all",
                offset={"mode": "linear", "base_ms": 0, "step_ms": 100},
                children=[{
                    "kind": "wled_control",
                    "target": {"kind": "scope"},
                    "params": {"mode": 5},
                    "flags_override": {"offset_mode": False, "arm_on_sync": True},
                }],
            ),
        ])
        ctrl = _RecordingControlService()
        runner, _ = _make_runner(scenes=self.scenes, control_service=ctrl)
        runner.run("x")
        self.assertEqual(len(ctrl.control_calls), 1)
        # offset_mode is forced True even though the override said False.
        self.assertTrue(ctrl.control_calls[0]["params"]["offset_mode"])
        # arm_on_sync override passes through.
        self.assertTrue(ctrl.control_calls[0]["params"]["arm_on_sync"])

    def test_offset_group_child_target_group_unicasts(self):
        """A child with target.kind=group sends a unicast OPC_CONTROL to
        that group instead of broadcasting."""
        self.scenes.create(label="X", actions=[
            self._container(
                groups=[1, 3],
                offset={"mode": "linear", "base_ms": 0, "step_ms": 100},
                children=[{
                    "kind": "wled_control",
                    "target": {"kind": "group", "value": 3},
                    "params": {"mode": 5},
                }],
            ),
        ])
        ctrl = _RecordingControlService()
        runner, _ = _make_runner(scenes=self.scenes, control_service=ctrl)
        runner.run("x")
        self.assertEqual(len(ctrl.control_calls), 1)
        self.assertEqual(ctrl.control_calls[0]["targetGroup"], 3)

    def test_offset_group_wled_preset_child_routes_via_send_wled_preset(self):
        self.scenes.create(label="X", actions=[
            self._container(
                groups="all",
                offset={"mode": "linear", "base_ms": 0, "step_ms": 100},
                children=[{
                    "kind": "wled_preset",
                    "target": {"kind": "scope"},
                    "params": {"presetId": 7, "brightness": 128},
                }],
            ),
        ])
        ctrl = _RecordingControlService()
        runner, _ = _make_runner(scenes=self.scenes, control_service=ctrl)
        runner.run("x")
        self.assertEqual(ctrl.control_calls, [])
        self.assertEqual(len(ctrl.preset_calls), 1)
        self.assertEqual(ctrl.preset_calls[0]["targetGroup"], 255)

    def test_offset_group_rl_preset_child_resolves_persisted_preset(self):
        rl = self._preset()
        self.scenes.create(label="X", actions=[
            self._container(
                groups="all",
                offset={"mode": "linear", "base_ms": 0, "step_ms": 100},
                children=[{
                    "kind": "rl_preset",
                    "target": {"kind": "scope"},
                    "params": {"presetId": "cascade_red", "brightness": 200},
                }],
            ),
        ])
        ctrl = _RecordingControlService()
        runner, _ = _make_runner(scenes=self.scenes, control_service=ctrl, rl_presets=rl)
        runner.run("x")
        # rl_preset goes through send_wled_control with the resolved preset's params.
        self.assertEqual(len(ctrl.control_calls), 1)
        call = ctrl.control_calls[0]
        self.assertEqual(call["params"]["mode"], 1)            # from preset
        self.assertEqual(call["params"]["brightness"], 200)    # action override
        self.assertTrue(call["params"]["offset_mode"])

    def test_offset_group_multiple_children_share_one_offset_setup(self):
        """A container with N children emits ONE OPC_OFFSET phase shared
        across all children — the bandwidth win the hierarchy enables."""
        self.scenes.create(label="Multi", actions=[
            self._container(
                groups="all",
                offset={"mode": "linear", "base_ms": 0, "step_ms": 100},
                children=[
                    {"kind": "wled_control",
                     "target": {"kind": "scope"},
                     "params": {"mode": 1}},
                    {"kind": "wled_preset",
                     "target": {"kind": "scope"},
                     "params": {"presetId": 7, "brightness": 128}},
                    {"kind": "wled_control",
                     "target": {"kind": "scope"},
                     "params": {"mode": 9}},
                ],
            ),
        ])
        ctrl = _RecordingControlService()
        runner, _ = _make_runner(scenes=self.scenes, control_service=ctrl)
        runner.run("multi")
        # ONE offset packet shared across 3 children.
        self.assertEqual(len(ctrl.offset_calls), 1)
        # Each child fires its own control / preset.
        self.assertEqual(len(ctrl.control_calls), 2)
        self.assertEqual(len(ctrl.preset_calls), 1)

    def test_offset_send_failure_marks_action_failed_but_runs_children(self):
        """If one OPC_OFFSET fails, the action is partial-failure. Children
        still dispatch — the failed group will be in NORMAL state on the
        device and rejects the broadcast control via the acceptance gate.
        """
        self.scenes.create(label="C", actions=[
            self._container(
                groups=[1, 2],
                offset={
                    "mode": "explicit",
                    "values": [
                        {"id": 1, "offset_ms": 0},
                        {"id": 2, "offset_ms": 100},
                    ],
                },
                children=[self._wled_control_child()],
            ),
        ])
        ctrl = _RecordingControlService(fail_offsets_for=[2])
        runner, _ = _make_runner(scenes=self.scenes, control_service=ctrl)
        result = runner.run("c")
        self.assertFalse(result.ok)
        self.assertEqual([c["targetGroup"] for c in ctrl.offset_calls], [1, 2])
        # Child broadcast still fires.
        self.assertEqual(len(ctrl.control_calls), 1)
        # Detail records the failure on the offset_packets map.
        detail = result.actions[0].detail
        self.assertFalse(detail["offset_packets"]["2"]["ok"])
        self.assertTrue(detail["offset_packets"]["1"]["ok"])

    def test_offset_group_linear_params_round_trip_through_save_reload(self):
        self.scenes.create(label="LinearAll", actions=[
            self._container(
                groups="all",
                offset={"mode": "linear", "base_ms": 500, "step_ms": 200},
                children=[self._wled_control_child()],
            ),
        ])
        reloaded = SceneService(storage_path=self.scenes.path)
        scene = reloaded.get("linearall")
        self.assertIsNotNone(scene)
        action = scene["actions"][0]
        self.assertEqual(action["kind"], "offset_group")
        self.assertEqual(action["groups"], "all")
        self.assertEqual(action["offset"],
                         {"mode": "linear", "base_ms": 500, "step_ms": 200})

        ctrl = _RecordingControlService()
        runner, _ = _make_runner(scenes=reloaded, control_service=ctrl)
        runner.run("linearall")
        self.assertEqual(len(ctrl.offset_calls), 1)
        self.assertEqual(ctrl.offset_calls[0]["mode"], "linear")
        self.assertEqual(ctrl.offset_calls[0]["params"],
                         {"base_ms": 500, "step_ms": 200})

    def test_offset_group_does_not_auto_emit_sync(self):
        self.scenes.create(label="C", actions=[
            self._container(
                groups="all",
                offset={"mode": "linear", "base_ms": 0, "step_ms": 100},
                children=[self._wled_control_child()],
            ),
        ])
        sync = _RecordingSyncService()
        ctrl = _RecordingControlService()
        runner, _ = _make_runner(scenes=self.scenes, control_service=ctrl, sync_service=sync)
        runner.run("c")
        self.assertEqual(sync.sync_calls, [])

    def test_legacy_groups_offset_target_action_runs_via_migration(self):
        """A legacy ``rl_preset`` action with ``target.kind=groups_offset``
        is migrated to an offset_group container on load and dispatches
        through the new path."""
        rl = self._preset()
        self.scenes.create(label="L", actions=[{
            "kind": KIND_RL_PRESET,
            "target": {
                "kind": "groups_offset",
                "groups": "all",
                "offset": {"mode": "linear", "base_ms": 0, "step_ms": 100},
            },
            "params": {"presetId": "cascade_red"},
        }])
        ctrl = _RecordingControlService()
        runner, _ = _make_runner(scenes=self.scenes, control_service=ctrl, rl_presets=rl)
        result = runner.run("l")
        self.assertTrue(result.ok)
        # Migrated to offset_group container with one rl_preset child →
        # one broadcast OPC_OFFSET + one OPC_CONTROL via send_wled_control.
        self.assertEqual(len(ctrl.offset_calls), 1)
        self.assertEqual(ctrl.offset_calls[0]["mode"], "linear")
        self.assertEqual(len(ctrl.control_calls), 1)


if __name__ == "__main__":
    unittest.main()
