"""Tests for SceneService — CRUD, schema validation, on_changed hook."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock

from racelink.services.scenes_service import (
    KIND_DELAY,
    KIND_OFFSET_GROUP,
    KIND_RL_PRESET,
    KIND_STARTBLOCK,
    KIND_SYNC,
    KIND_WLED_CONTROL,
    KIND_WLED_PRESET,
    MAX_ACTIONS_PER_SCENE,
    MAX_DELAY_MS,
    MAX_GROUPS_OFFSET_ENTRIES,
    MAX_OFFSET_GROUP_CHILDREN,
    OFFSET_MS_MAX,
    SCHEMA_VERSION,
    SceneService,
)


def _rl_preset_action(group_id=1, preset_slug="start_red", brightness=200, **flags):
    return {
        "kind": KIND_RL_PRESET,
        "target": {"kind": "group", "value": group_id},
        "params": {"presetId": preset_slug, "brightness": brightness},
        "flags_override": dict(flags),
    }


class SceneServiceCrudTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "scenes.json")
        self.svc = SceneService(storage_path=self.path)

    def test_empty_on_missing_file(self):
        self.assertEqual(self.svc.list(), [])
        self.assertFalse(os.path.exists(self.path))

    def test_create_persists_with_id_and_slug(self):
        scene = self.svc.create(
            label="Start Sequence",
            actions=[
                _rl_preset_action(group_id=1, arm_on_sync=True),
                {"kind": KIND_SYNC},
            ],
        )
        self.assertEqual(scene["id"], 0)
        self.assertEqual(scene["key"], "start_sequence")
        self.assertEqual(scene["label"], "Start Sequence")
        self.assertEqual(len(scene["actions"]), 2)
        self.assertEqual(scene["actions"][0]["kind"], KIND_RL_PRESET)
        self.assertEqual(scene["actions"][1]["kind"], KIND_SYNC)

        with open(self.path) as fh:
            data = json.load(fh)
        self.assertEqual(data["schema_version"], SCHEMA_VERSION)
        self.assertEqual(data["next_id"], 1)
        self.assertEqual(len(data["scenes"]), 1)

    def test_create_assigns_unique_keys_on_collision(self):
        a = self.svc.create(label="Show", actions=[])
        b = self.svc.create(label="Show", actions=[])
        self.assertEqual(a["key"], "show")
        self.assertEqual(b["key"], "show_2")

    def test_get_and_get_by_id(self):
        scene = self.svc.create(label="Demo", actions=[])
        self.assertEqual(self.svc.get("demo")["id"], scene["id"])
        self.assertEqual(self.svc.get_by_id(scene["id"])["key"], "demo")
        self.assertIsNone(self.svc.get("missing"))
        self.assertIsNone(self.svc.get_by_id(9999))

    def test_update_partial_label_and_actions(self):
        scene = self.svc.create(label="Start", actions=[])
        updated = self.svc.update("start", label="Start v2")
        self.assertEqual(updated["label"], "Start v2")
        # `updated` is bumped to current ISO-second; same wall-second as
        # `created` is acceptable on fast machines (matches RLPresetsService).
        self.assertGreaterEqual(updated["updated"], scene["created"])

        updated2 = self.svc.update(
            "start",
            actions=[_rl_preset_action(group_id=2)],
        )
        self.assertEqual(len(updated2["actions"]), 1)
        # label preserved when not passed
        self.assertEqual(updated2["label"], "Start v2")

    def test_update_returns_none_when_key_missing(self):
        self.assertIsNone(self.svc.update("nope", label="x"))

    def test_delete_returns_true_only_when_present(self):
        self.svc.create(label="Demo", actions=[])
        self.assertTrue(self.svc.delete("demo"))
        self.assertFalse(self.svc.delete("demo"))
        self.assertEqual(self.svc.list(), [])

    def test_duplicate_clones_actions(self):
        original = self.svc.create(
            label="Start",
            actions=[_rl_preset_action(group_id=1), {"kind": KIND_SYNC}],
        )
        dup = self.svc.duplicate("start")
        self.assertNotEqual(dup["id"], original["id"])
        self.assertEqual(dup["label"], "Start copy")
        self.assertEqual(len(dup["actions"]), 2)

    def test_ids_are_monotonic_across_delete(self):
        a = self.svc.create(label="A", actions=[])
        self.svc.delete("a")
        b = self.svc.create(label="B", actions=[])
        self.assertEqual(a["id"], 0)
        self.assertEqual(b["id"], 1)  # not recycled

    def test_create_default_stop_on_error_is_true(self):
        """Batch A (2026-04-28): default value of stop_on_error.

        The runner aborts a sequence on first failure under this default
        — the safer behaviour when a half-failed scene would otherwise
        burn air-time on packets that can't reach the intended state."""
        scene = self.svc.create(label="Default", actions=[])
        self.assertTrue(scene["stop_on_error"])
        # And persisted on disk.
        with open(self.path) as fh:
            data = json.load(fh)
        self.assertTrue(data["scenes"][0]["stop_on_error"])

    def test_create_explicit_stop_on_error_false_persists(self):
        scene = self.svc.create(label="Loose", actions=[], stop_on_error=False)
        self.assertFalse(scene["stop_on_error"])
        # Round-trips through reload.
        reloaded = SceneService(storage_path=self.path).get("loose")
        self.assertFalse(reloaded["stop_on_error"])

    def test_legacy_scene_without_field_loads_with_default_true(self):
        """Backward-compat: a scene file written before stop_on_error
        existed loads with the safer default. No migration needed."""
        legacy_payload = {
            "schema_version": 1,
            "next_id": 1,
            "scenes": [{
                "id": 0,
                "key": "old",
                "label": "Old",
                "created": "2026-01-01T00:00:00Z",
                "updated": "2026-01-01T00:00:00Z",
                "actions": [],
                # stop_on_error intentionally omitted
            }],
        }
        with open(self.path, "w") as fh:
            json.dump(legacy_payload, fh)
        svc = SceneService(storage_path=self.path)
        self.assertTrue(svc.get("old")["stop_on_error"])

    def test_update_can_toggle_stop_on_error(self):
        self.svc.create(label="Toggle", actions=[])
        updated = self.svc.update("toggle", stop_on_error=False)
        self.assertFalse(updated["stop_on_error"])
        # Survives reload.
        reloaded = SceneService(storage_path=self.path).get("toggle")
        self.assertFalse(reloaded["stop_on_error"])

    def test_update_preserves_stop_on_error_when_not_passed(self):
        self.svc.create(label="Keep", actions=[], stop_on_error=False)
        updated = self.svc.update("keep", label="Keep v2")
        self.assertFalse(updated["stop_on_error"])
        self.assertEqual(updated["label"], "Keep v2")

    def test_duplicate_carries_stop_on_error_value(self):
        self.svc.create(label="Source", actions=[], stop_on_error=False)
        dup = self.svc.duplicate("source")
        self.assertFalse(dup["stop_on_error"])

    def test_create_coerces_stop_on_error_string_forms(self):
        """The frontend round-trips ``stop_on_error`` as a JSON bool, but
        legacy / hand-edited scenes might carry stringy forms — handle
        them defensively."""
        a = self.svc.create(label="A", actions=[], stop_on_error="false")
        b = self.svc.create(label="B", actions=[], stop_on_error="0")
        c = self.svc.create(label="C", actions=[], stop_on_error="yes")
        self.assertFalse(a["stop_on_error"])
        self.assertFalse(b["stop_on_error"])
        self.assertTrue(c["stop_on_error"])


class SceneServiceValidationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "scenes.json")
        self.svc = SceneService(storage_path=self.path)

    def test_create_rejects_empty_label(self):
        with self.assertRaises(ValueError):
            self.svc.create(label="", actions=[])

    def test_create_rejects_too_many_actions(self):
        too_many = [{"kind": KIND_SYNC}] * (MAX_ACTIONS_PER_SCENE + 1)
        with self.assertRaises(ValueError):
            self.svc.create(label="Long", actions=too_many)

    def test_max_actions_exactly_allowed(self):
        scene = self.svc.create(
            label="Edge",
            actions=[{"kind": KIND_SYNC}] * MAX_ACTIONS_PER_SCENE,
        )
        self.assertEqual(len(scene["actions"]), MAX_ACTIONS_PER_SCENE)

    def test_invalid_kind_rejected(self):
        with self.assertRaises(ValueError):
            self.svc.create(label="Bad", actions=[{"kind": "unicorn"}])

    def test_delay_validates_duration(self):
        ok = self.svc.create(label="OK", actions=[{"kind": KIND_DELAY, "duration_ms": 0}])
        self.assertEqual(ok["actions"][0]["duration_ms"], 0)

        with self.assertRaises(ValueError):
            self.svc.create(label="Neg", actions=[{"kind": KIND_DELAY, "duration_ms": -1}])
        with self.assertRaises(ValueError):
            self.svc.create(label="Big", actions=[{"kind": KIND_DELAY, "duration_ms": MAX_DELAY_MS + 1}])

    def test_sync_rejects_extra_fields(self):
        with self.assertRaises(ValueError):
            self.svc.create(label="X", actions=[{"kind": KIND_SYNC, "target": {"kind": "group", "value": 1}}])

    def test_delay_rejects_target(self):
        with self.assertRaises(ValueError):
            self.svc.create(label="X", actions=[
                {"kind": KIND_DELAY, "duration_ms": 100, "target": {"kind": "group", "value": 1}}
            ])

    def test_target_kind_validated(self):
        with self.assertRaises(ValueError):
            self.svc.create(label="X", actions=[{
                "kind": KIND_RL_PRESET,
                "target": {"kind": "broadcast", "value": None},
                "params": {"presetId": "x"},
            }])

    def test_group_target_range_enforced(self):
        # 255 is reserved for broadcast; not a valid scene target
        with self.assertRaises(ValueError):
            self.svc.create(label="X", actions=[{
                "kind": KIND_RL_PRESET,
                "target": {"kind": "group", "value": 255},
                "params": {"presetId": "x"},
            }])
        # negative also rejected
        with self.assertRaises(ValueError):
            self.svc.create(label="X", actions=[{
                "kind": KIND_RL_PRESET,
                "target": {"kind": "group", "value": -1},
                "params": {"presetId": "x"},
            }])

    def test_device_target_must_be_12_hex(self):
        with self.assertRaises(ValueError):
            self.svc.create(label="X", actions=[{
                "kind": KIND_RL_PRESET,
                "target": {"kind": "device", "value": "ABCDEF"},  # 6 chars, not 12
                "params": {"presetId": "x"},
            }])
        # 12 hex passes (and uppercases)
        scene = self.svc.create(label="OK", actions=[{
            "kind": KIND_RL_PRESET,
            "target": {"kind": "device", "value": "aabbccddeeff"},
            "params": {"presetId": "x"},
        }])
        self.assertEqual(scene["actions"][0]["target"]["value"], "AABBCCDDEEFF")

    def test_flags_override_filtered_to_known_keys(self):
        scene = self.svc.create(label="X", actions=[{
            "kind": KIND_RL_PRESET,
            "target": {"kind": "group", "value": 1},
            "params": {"presetId": "x"},
            "flags_override": {"arm_on_sync": True, "unknown_flag": True, "offset_mode": False},
        }])
        flags = scene["actions"][0]["flags_override"]
        self.assertIn("arm_on_sync", flags)
        self.assertIn("offset_mode", flags)
        self.assertNotIn("unknown_flag", flags)

    # ---- offset_group container ---------------------------------------

    def _wled_control_child(self, target=None, params=None):
        return {
            "kind": KIND_WLED_CONTROL,
            "target": target or {"kind": "scope"},
            "params": params or {"presetId": 1, "brightness": 200},
        }

    def test_offset_group_explicit_canonicalises_and_sorts(self):
        scene = self.svc.create(label="Cascade", actions=[{
            "kind": KIND_OFFSET_GROUP,
            "groups": [5, 1, 3],
            "offset": {
                "mode": "explicit",
                "values": [
                    {"id": 5, "offset_ms": 250},
                    {"id": 1, "offset_ms": 0},
                    {"id": 3, "offset_ms": 100},
                ],
            },
            "actions": [self._wled_control_child()],
        }])
        action = scene["actions"][0]
        self.assertEqual(action["kind"], KIND_OFFSET_GROUP)
        # Group IDs sorted; values sorted by id for deterministic order.
        self.assertEqual(action["groups"], [1, 3, 5])
        self.assertEqual(action["offset"], {
            "mode": "explicit",
            "values": [
                {"id": 1, "offset_ms": 0},
                {"id": 3, "offset_ms": 100},
                {"id": 5, "offset_ms": 250},
            ],
        })
        self.assertEqual(action["actions"][0]["kind"], KIND_WLED_CONTROL)
        self.assertEqual(action["actions"][0]["target"], {"kind": "scope"})

    def test_offset_group_linear_all_groups_canonicalises(self):
        scene = self.svc.create(label="Cascade", actions=[{
            "kind": KIND_OFFSET_GROUP,
            "groups": "all",
            "offset": {"mode": "linear", "base_ms": 0, "step_ms": 100},
            "actions": [],
        }])
        action = scene["actions"][0]
        self.assertEqual(action["groups"], "all")
        self.assertEqual(action["offset"],
                         {"mode": "linear", "base_ms": 0, "step_ms": 100})
        self.assertEqual(action["actions"], [])

    def test_offset_group_vshape_carries_center(self):
        scene = self.svc.create(label="VS", actions=[{
            "kind": KIND_OFFSET_GROUP,
            "groups": "all",
            "offset": {"mode": "vshape", "base_ms": 0, "step_ms": 50, "center": 8},
            "actions": [],
        }])
        offset = scene["actions"][0]["offset"]
        self.assertEqual(offset, {"mode": "vshape", "base_ms": 0, "step_ms": 50, "center": 8})

    def test_offset_group_modulo_carries_cycle(self):
        scene = self.svc.create(label="MO", actions=[{
            "kind": KIND_OFFSET_GROUP,
            "groups": "all",
            "offset": {"mode": "modulo", "base_ms": 0, "step_ms": 100, "cycle": 4},
            "actions": [],
        }])
        offset = scene["actions"][0]["offset"]
        self.assertEqual(offset, {"mode": "modulo", "base_ms": 0, "step_ms": 100, "cycle": 4})

    def test_offset_group_explicit_requires_concrete_groups(self):
        with self.assertRaises(ValueError):
            self.svc.create(label="X", actions=[{
                "kind": KIND_OFFSET_GROUP,
                "groups": "all",
                "offset": {"mode": "explicit", "values": []},
                "actions": [],
            }])

    def test_offset_group_rejects_empty_groups_list(self):
        with self.assertRaises(ValueError):
            self.svc.create(label="X", actions=[{
                "kind": KIND_OFFSET_GROUP,
                "groups": [],
                "offset": {"mode": "linear", "base_ms": 0, "step_ms": 100},
                "actions": [],
            }])

    def test_offset_group_rejects_duplicate_ids(self):
        with self.assertRaises(ValueError):
            self.svc.create(label="X", actions=[{
                "kind": KIND_OFFSET_GROUP,
                "groups": [1, 1],
                "offset": {"mode": "linear", "base_ms": 0, "step_ms": 100},
                "actions": [],
            }])

    def test_offset_group_rejects_id_255(self):
        # 255 is the broadcast sentinel; "all" is the documented spelling.
        with self.assertRaises(ValueError):
            self.svc.create(label="X", actions=[{
                "kind": KIND_OFFSET_GROUP,
                "groups": [255],
                "offset": {"mode": "linear", "base_ms": 0, "step_ms": 100},
                "actions": [],
            }])

    def test_offset_group_caps_entry_count(self):
        too_many = list(range(MAX_GROUPS_OFFSET_ENTRIES + 1))
        with self.assertRaises(ValueError):
            self.svc.create(label="X", actions=[{
                "kind": KIND_OFFSET_GROUP,
                "groups": too_many,
                "offset": {"mode": "linear", "base_ms": 0, "step_ms": 100},
                "actions": [],
            }])

    def test_offset_group_rejects_unknown_mode(self):
        with self.assertRaises(ValueError):
            self.svc.create(label="X", actions=[{
                "kind": KIND_OFFSET_GROUP,
                "groups": "all",
                "offset": {"mode": "log2"},
                "actions": [],
            }])

    def test_offset_group_rejects_out_of_range_explicit_offset(self):
        with self.assertRaises(ValueError):
            self.svc.create(label="X", actions=[{
                "kind": KIND_OFFSET_GROUP,
                "groups": [1],
                "offset": {
                    "mode": "explicit",
                    "values": [{"id": 1, "offset_ms": OFFSET_MS_MAX + 1}],
                },
                "actions": [],
            }])

    # ---- offset_group children -----------------------------------------

    def test_offset_group_rejects_invalid_child_kinds(self):
        # Sync, delay, startblock, and another offset_group are all forbidden.
        for forbidden in (
            {"kind": KIND_SYNC},
            {"kind": KIND_DELAY, "duration_ms": 100},
            {"kind": KIND_STARTBLOCK,
             "target": {"kind": "scope"},
             "params": {"fn_key": "startblock_control"}},
            {"kind": KIND_OFFSET_GROUP,
             "groups": "all",
             "offset": {"mode": "linear", "base_ms": 0, "step_ms": 100},
             "actions": []},
        ):
            with self.assertRaises(ValueError):
                self.svc.create(label="X", actions=[{
                    "kind": KIND_OFFSET_GROUP,
                    "groups": "all",
                    "offset": {"mode": "linear", "base_ms": 0, "step_ms": 100},
                    "actions": [forbidden],
                }])

    def test_offset_group_caps_children_count(self):
        too_many = [self._wled_control_child() for _ in range(MAX_OFFSET_GROUP_CHILDREN + 1)]
        with self.assertRaises(ValueError):
            self.svc.create(label="X", actions=[{
                "kind": KIND_OFFSET_GROUP,
                "groups": "all",
                "offset": {"mode": "linear", "base_ms": 0, "step_ms": 100},
                "actions": too_many,
            }])

    def test_offset_group_child_target_scope_is_default(self):
        scene = self.svc.create(label="X", actions=[{
            "kind": KIND_OFFSET_GROUP,
            "groups": "all",
            "offset": {"mode": "linear", "base_ms": 0, "step_ms": 100},
            "actions": [
                {"kind": KIND_WLED_PRESET,
                 "target": {"kind": "scope"},
                 "params": {"presetId": 7, "brightness": 128}},
            ],
        }])
        child = scene["actions"][0]["actions"][0]
        self.assertEqual(child["target"], {"kind": "scope"})

    def test_offset_group_child_group_must_be_in_parent(self):
        # parent groups [1, 3] — child target group 2 is rejected.
        with self.assertRaises(ValueError):
            self.svc.create(label="X", actions=[{
                "kind": KIND_OFFSET_GROUP,
                "groups": [1, 3],
                "offset": {"mode": "linear", "base_ms": 0, "step_ms": 100},
                "actions": [{
                    "kind": KIND_WLED_CONTROL,
                    "target": {"kind": "group", "value": 2},
                    "params": {"presetId": 1},
                }],
            }])
        # ... but group 3 is fine.
        ok = self.svc.create(label="OK", actions=[{
            "kind": KIND_OFFSET_GROUP,
            "groups": [1, 3],
            "offset": {"mode": "linear", "base_ms": 0, "step_ms": 100},
            "actions": [{
                "kind": KIND_WLED_CONTROL,
                "target": {"kind": "group", "value": 3},
                "params": {"presetId": 1},
            }],
        }])
        self.assertEqual(ok["actions"][0]["actions"][0]["target"],
                         {"kind": "group", "value": 3})

    def test_offset_group_child_group_filter_skipped_when_all(self):
        # parent.groups == "all" → any group id passes the membership check.
        scene = self.svc.create(label="X", actions=[{
            "kind": KIND_OFFSET_GROUP,
            "groups": "all",
            "offset": {"mode": "linear", "base_ms": 0, "step_ms": 100},
            "actions": [{
                "kind": KIND_WLED_CONTROL,
                "target": {"kind": "group", "value": 42},
                "params": {"presetId": 1},
            }],
        }])
        self.assertEqual(scene["actions"][0]["actions"][0]["target"],
                         {"kind": "group", "value": 42})

    def test_offset_group_child_device_target_format(self):
        # MAC must be 12-char hex; case is normalised to upper.
        scene = self.svc.create(label="X", actions=[{
            "kind": KIND_OFFSET_GROUP,
            "groups": "all",
            "offset": {"mode": "linear", "base_ms": 0, "step_ms": 100},
            "actions": [{
                "kind": KIND_WLED_CONTROL,
                "target": {"kind": "device", "value": "aabbccddeeff"},
                "params": {"presetId": 1},
            }],
        }])
        self.assertEqual(scene["actions"][0]["actions"][0]["target"],
                         {"kind": "device", "value": "AABBCCDDEEFF"})
        with self.assertRaises(ValueError):
            self.svc.create(label="Y", actions=[{
                "kind": KIND_OFFSET_GROUP,
                "groups": "all",
                "offset": {"mode": "linear", "base_ms": 0, "step_ms": 100},
                "actions": [{
                    "kind": KIND_WLED_CONTROL,
                    "target": {"kind": "device", "value": "ABCD"},
                    "params": {"presetId": 1},
                }],
            }])

    def test_offset_group_top_level_only_no_nesting(self):
        # An offset_group at the top of a scene is fine, but nesting it
        # inside another offset_group must fail (covered by
        # test_offset_group_rejects_invalid_child_kinds).
        scene = self.svc.create(label="OK", actions=[{
            "kind": KIND_OFFSET_GROUP,
            "groups": "all",
            "offset": {"mode": "linear", "base_ms": 0, "step_ms": 100},
            "actions": [self._wled_control_child()],
        }])
        self.assertEqual(scene["actions"][0]["kind"], KIND_OFFSET_GROUP)

    def test_offset_group_rejects_stray_top_level_fields(self):
        for stray in ("target", "params", "flags_override", "duration_ms"):
            with self.assertRaises(ValueError):
                self.svc.create(label="X", actions=[{
                    "kind": KIND_OFFSET_GROUP,
                    "groups": "all",
                    "offset": {"mode": "linear", "base_ms": 0, "step_ms": 100},
                    "actions": [],
                    stray: "bogus",
                }])

    # ---- legacy migration: groups_offset target -> offset_group action ---

    def test_legacy_groups_offset_target_migrated_to_offset_group(self):
        # Pre-hierarchy shape: kind=rl_preset with target.kind=groups_offset.
        # The loader rewrites the whole action into an offset_group container
        # with a single child whose target is "scope".
        scene = self.svc.create(label="Legacy", actions=[{
            "kind": KIND_RL_PRESET,
            "target": {
                "kind": "groups_offset",
                "groups": "all",
                "offset": {"mode": "linear", "base_ms": 0, "step_ms": 100},
            },
            "params": {"presetId": "start_red"},
        }])
        action = scene["actions"][0]
        self.assertEqual(action["kind"], KIND_OFFSET_GROUP)
        self.assertEqual(action["groups"], "all")
        self.assertEqual(action["offset"],
                         {"mode": "linear", "base_ms": 0, "step_ms": 100})
        self.assertEqual(len(action["actions"]), 1)
        child = action["actions"][0]
        self.assertEqual(child["kind"], KIND_RL_PRESET)
        self.assertEqual(child["target"], {"kind": "scope"})
        self.assertEqual(child["params"], {"presetId": "start_red"})

    def test_legacy_groups_offset_pre_formula_shape_migrated(self):
        # The original {id, offset_ms} list (with optional ui_hints) also
        # round-trips into the new container form.
        scene = self.svc.create(label="OldOld", actions=[{
            "kind": KIND_WLED_CONTROL,
            "target": {
                "kind": "groups_offset",
                "groups": [
                    {"id": 1, "offset_ms": 0},
                    {"id": 3, "offset_ms": 100},
                ],
                "ui_hints": {"mode": "linear", "base_ms": 0, "step_ms": 100},
            },
            "params": {"presetId": 7},
        }])
        action = scene["actions"][0]
        self.assertEqual(action["kind"], KIND_OFFSET_GROUP)
        self.assertEqual(action["groups"], [1, 3])
        self.assertEqual(action["offset"],
                         {"mode": "linear", "base_ms": 0, "step_ms": 100})
        self.assertEqual(action["actions"][0]["kind"], KIND_WLED_CONTROL)

    def test_kind_with_target_requires_target(self):
        with self.assertRaises(ValueError):
            self.svc.create(label="X", actions=[{
                "kind": KIND_RL_PRESET,
                "params": {"presetId": "x"},
            }])


class SceneServiceOnChangedTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "scenes.json")
        self.svc = SceneService(storage_path=self.path)
        self.cb = MagicMock()
        self.svc.on_changed = self.cb

    def test_create_fires_on_changed(self):
        self.svc.create(label="A", actions=[])
        self.cb.assert_called_once()

    def test_update_fires_on_changed(self):
        self.svc.create(label="A", actions=[])
        self.cb.reset_mock()
        self.svc.update("a", label="A2")
        self.cb.assert_called_once()

    def test_update_missing_does_not_fire(self):
        self.cb.reset_mock()
        self.assertIsNone(self.svc.update("nope", label="x"))
        self.cb.assert_not_called()

    def test_delete_fires_on_changed(self):
        self.svc.create(label="A", actions=[])
        self.cb.reset_mock()
        self.svc.delete("a")
        self.cb.assert_called_once()

    def test_listener_exception_does_not_undo_write(self):
        self.svc.on_changed = MagicMock(side_effect=RuntimeError("listener boom"))
        scene = self.svc.create(label="A", actions=[])
        self.assertIsNotNone(self.svc.get("a"))
        self.assertEqual(scene["key"], "a")


class SceneServicePersistenceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "scenes.json")

    def test_round_trip_through_disk(self):
        svc1 = SceneService(storage_path=self.path)
        svc1.create(
            label="Start",
            actions=[
                _rl_preset_action(group_id=1, arm_on_sync=True, offset_mode=True),
                {"kind": KIND_SYNC},
                {"kind": KIND_DELAY, "duration_ms": 1500},
                {
                    "kind": KIND_WLED_PRESET,
                    "target": {"kind": "device", "value": "AABBCCDDEEFF"},
                    "params": {"presetId": 5, "brightness": 128},
                    "flags_override": {"force_reapply": True},
                },
            ],
        )

        svc2 = SceneService(storage_path=self.path)
        scenes = svc2.list()
        self.assertEqual(len(scenes), 1)
        actions = scenes[0]["actions"]
        self.assertEqual(actions[0]["flags_override"]["arm_on_sync"], True)
        self.assertEqual(actions[0]["flags_override"]["offset_mode"], True)
        self.assertEqual(actions[1]["kind"], KIND_SYNC)
        self.assertEqual(actions[2]["duration_ms"], 1500)
        self.assertEqual(actions[3]["target"]["value"], "AABBCCDDEEFF")

    def test_replace_all_assigns_fresh_ids(self):
        svc = SceneService(storage_path=self.path)
        svc.create(label="A", actions=[])
        svc.create(label="B", actions=[])
        svc.replace_all([
            {"label": "X", "actions": []},
            {"label": "Y", "actions": []},
        ])
        scenes = svc.list()
        self.assertEqual([s["label"] for s in scenes], ["X", "Y"])
        # New ids must come AFTER previously used ids (no recycling)
        self.assertGreaterEqual(min(s["id"] for s in scenes), 2)

    def test_corrupt_file_starts_empty(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("not a JSON document {{")
        svc = SceneService(storage_path=self.path)
        self.assertEqual(svc.list(), [])


class RenumberGroupReferencesTests(unittest.TestCase):
    """Group-deletion path: ``SceneService.renumber_group_references``
    rewrites every persisted scene's group references after the API
    deletes the group at index ``deleted_gid``.

    Shift contract:
      * value == deleted_gid → 0 (collapse to Unconfigured)
      * value >  deleted_gid → value - 1
      * value <  deleted_gid → unchanged
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "scenes.json")
        self.svc = SceneService(storage_path=self.path)

    def test_no_op_when_no_scene_references_deleted_group(self):
        self.svc.create(
            label="A", actions=[_rl_preset_action(group_id=5)],
        )
        changed = self.svc.renumber_group_references(deleted_gid=2)
        self.assertEqual(changed, 1)  # group 5 shifted to 4
        self.assertEqual(self.svc.list()[0]["actions"][0]["target"]["value"], 4)

    def test_top_level_target_collapses_when_id_matches(self):
        self.svc.create(label="A", actions=[_rl_preset_action(group_id=3)])
        changed = self.svc.renumber_group_references(deleted_gid=3)
        self.assertEqual(changed, 1)
        self.assertEqual(self.svc.list()[0]["actions"][0]["target"]["value"], 0)

    def test_lower_indexed_target_stays_unchanged(self):
        self.svc.create(label="A", actions=[_rl_preset_action(group_id=1)])
        changed = self.svc.renumber_group_references(deleted_gid=5)
        # Lower id is unchanged → no rewrite needed → changed count is 0.
        self.assertEqual(changed, 0)
        self.assertEqual(self.svc.list()[0]["actions"][0]["target"]["value"], 1)

    def test_offset_group_groups_list_shifts(self):
        self.svc.create(
            label="A",
            actions=[{
                "kind": KIND_OFFSET_GROUP,
                "groups": [1, 3, 5],
                "offset": {"mode": "linear", "base_ms": 0, "step_ms": 100},
                "actions": [],
            }],
        )
        changed = self.svc.renumber_group_references(deleted_gid=3)
        self.assertEqual(changed, 1)
        scene = self.svc.list()[0]
        # 1 stays, 3 collapses to 0, 5 shifts to 4. Stored order is
        # canonical (sorted) — the validator sorts the groups list.
        self.assertEqual(scene["actions"][0]["groups"], [0, 1, 4])

    def test_offset_group_groups_list_dedupes_after_collapse(self):
        """If ``deleted_gid == 1`` and the list is ``[0, 1, 2]``, the
        rewrite produces ``[0, 0, 1]``. Duplicate ``0`` is collapsed
        so the on-disk list stays canonical."""
        self.svc.create(
            label="A",
            actions=[{
                "kind": KIND_OFFSET_GROUP,
                "groups": [0, 1, 2],
                "offset": {"mode": "linear", "base_ms": 0, "step_ms": 100},
                "actions": [],
            }],
        )
        changed = self.svc.renumber_group_references(deleted_gid=1)
        self.assertEqual(changed, 1)
        self.assertEqual(self.svc.list()[0]["actions"][0]["groups"], [0, 1])

    def test_offset_group_all_passes_through(self):
        """``groups: 'all'`` is dynamic — it doesn't tie to specific
        ids and should pass unchanged. The action's children may
        still need rewriting though."""
        self.svc.create(
            label="A",
            actions=[{
                "kind": KIND_OFFSET_GROUP,
                "groups": "all",
                "offset": {"mode": "linear", "base_ms": 0, "step_ms": 100},
                "actions": [],
            }],
        )
        changed = self.svc.renumber_group_references(deleted_gid=2)
        self.assertEqual(changed, 0)
        self.assertEqual(self.svc.list()[0]["actions"][0]["groups"], "all")

    def test_offset_group_child_target_shifts(self):
        self.svc.create(
            label="A",
            actions=[{
                "kind": KIND_OFFSET_GROUP,
                "groups": [3, 5],
                "offset": {"mode": "linear", "base_ms": 0, "step_ms": 100},
                "actions": [
                    {
                        "kind": KIND_WLED_PRESET,
                        "target": {"kind": "group", "value": 5},
                        "params": {"presetId": 1, "brightness": 100},
                    },
                ],
            }],
        )
        changed = self.svc.renumber_group_references(deleted_gid=3)
        self.assertEqual(changed, 1)
        scene = self.svc.list()[0]
        # Both the parent groups list and the child target are
        # rewritten; the parent list is canonicalised to sorted.
        self.assertEqual(scene["actions"][0]["groups"], [0, 4])
        self.assertEqual(scene["actions"][0]["actions"][0]["target"]["value"], 4)

    def test_explicit_offset_values_shift_and_dedupe(self):
        self.svc.create(
            label="A",
            actions=[{
                "kind": KIND_OFFSET_GROUP,
                "groups": [3, 5],
                "offset": {
                    "mode": "explicit",
                    "values": [
                        {"id": 3, "offset_ms": 100},
                        {"id": 5, "offset_ms": 250},
                    ],
                },
                "actions": [],
            }],
        )
        changed = self.svc.renumber_group_references(deleted_gid=3)
        self.assertEqual(changed, 1)
        scene = self.svc.list()[0]
        # id=3 collapses to 0, id=5 shifts to 4.
        self.assertEqual(
            scene["actions"][0]["offset"]["values"],
            [{"id": 0, "offset_ms": 100}, {"id": 4, "offset_ms": 250}],
        )

    def test_multiple_scenes_only_changed_count_returned(self):
        # Scene A: references group 5 → will rewrite.
        self.svc.create(label="A", actions=[_rl_preset_action(group_id=5)])
        # Scene B: references group 1 (below deleted_gid=3) → no change.
        self.svc.create(label="B", actions=[_rl_preset_action(group_id=1)])
        changed = self.svc.renumber_group_references(deleted_gid=3)
        self.assertEqual(changed, 1)
        scenes = {s["label"]: s for s in self.svc.list()}
        self.assertEqual(scenes["A"]["actions"][0]["target"]["value"], 4)
        self.assertEqual(scenes["B"]["actions"][0]["target"]["value"], 1)

    def test_persistence_round_trips_renumbered_state(self):
        """A delete + reload cycle must show the renumbered values
        on disk, not just in memory."""
        self.svc.create(label="A", actions=[_rl_preset_action(group_id=5)])
        self.svc.renumber_group_references(deleted_gid=3)
        # Fresh service loads from disk.
        svc2 = SceneService(storage_path=self.path)
        self.assertEqual(svc2.list()[0]["actions"][0]["target"]["value"], 4)


if __name__ == "__main__":
    unittest.main()
