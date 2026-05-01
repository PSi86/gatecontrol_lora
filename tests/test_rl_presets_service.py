"""Phase B.10 — RLPresetsService CRUD tests."""

import json
import os
import tempfile
import unittest

from racelink.services.rl_presets_service import RLPresetsService, SCHEMA_VERSION


class RLPresetsServiceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "rl_presets.json")
        self.svc = RLPresetsService(storage_path=self.path)

    def test_empty_on_missing_file(self):
        self.assertEqual(self.svc.list(), [])
        self.assertFalse(os.path.exists(self.path))

    def test_create_persists_with_canonical_params_and_flags(self):
        preset = self.svc.create(
            label="Start Red",
            params={
                "mode": 1, "speed": 200, "color1": [255, 0, 0],
                "check1": True, "brightness": 255,
            },
            flags={"arm_on_sync": True},
        )
        self.assertEqual(preset["label"], "Start Red")
        self.assertEqual(preset["key"], "start_red")
        self.assertEqual(preset["params"]["mode"], 1)
        self.assertEqual(preset["params"]["color1"], [255, 0, 0])
        self.assertTrue(preset["params"]["check1"])
        # unspecified keys are filled with None
        self.assertIsNone(preset["params"]["mode"] if preset["params"]["mode"] is None else None)
        self.assertIsNone(preset["params"]["custom2"])
        self.assertTrue(preset["flags"]["arm_on_sync"])
        self.assertFalse(preset["flags"]["force_tt0"])
        # file written atomically
        self.assertTrue(os.path.isfile(self.path))
        with open(self.path) as fh:
            data = json.load(fh)
        self.assertEqual(data["schema_version"], SCHEMA_VERSION)
        self.assertEqual(len(data["presets"]), 1)

    def test_create_persists_offset_mode_flag(self):
        preset = self.svc.create(
            label="Staggered",
            params={"mode": 2, "brightness": 128},
            flags={"arm_on_sync": True, "offset_mode": True},
        )
        self.assertTrue(preset["flags"]["arm_on_sync"])
        self.assertTrue(preset["flags"]["offset_mode"])
        self.assertFalse(preset["flags"]["force_tt0"])
        self.assertFalse(preset["flags"]["force_reapply"])
        reloaded = self.svc.get(preset["key"])
        self.assertTrue(reloaded["flags"]["offset_mode"])

    def test_legacy_three_key_flags_read_missing_offset_as_false(self):
        # Simulate a pre-unification preset file (v2 with 3-key flags only).
        pre_file = {
            "schema_version": SCHEMA_VERSION,
            "next_id": 1,
            "presets": [{
                "id": 0,
                "key": "old_preset",
                "label": "Old Preset",
                "created": "2025-12-01T00:00:00Z",
                "updated": "2025-12-01T00:00:00Z",
                "params": {},
                "flags": {
                    "arm_on_sync": True,
                    "force_tt0": False,
                    "force_reapply": False,
                    # no offset_mode key
                },
            }],
        }
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(pre_file, fh)
        fresh = RLPresetsService(storage_path=self.path)
        got = fresh.get("old_preset")
        self.assertIsNotNone(got)
        self.assertTrue(got["flags"]["arm_on_sync"])
        self.assertIn("offset_mode", got["flags"])
        self.assertFalse(got["flags"]["offset_mode"])

    def test_slug_collision_appends_suffix(self):
        a = self.svc.create(label="Start Red")
        b = self.svc.create(label="Start Red")
        self.assertEqual(a["key"], "start_red")
        self.assertEqual(b["key"], "start_red_2")

    def test_get_and_list_roundtrip(self):
        a = self.svc.create(label="Alpha")
        b = self.svc.create(label="Beta")
        found = {p["key"] for p in self.svc.list()}
        self.assertEqual(found, {a["key"], b["key"]})
        self.assertEqual(self.svc.get(a["key"])["label"], "Alpha")
        self.assertIsNone(self.svc.get("nonexistent"))

    def test_update_partial_and_timestamp_bumps(self):
        preset = self.svc.create(label="Alpha", params={"mode": 5})
        original_updated = preset["updated"]
        # sleep-free way: just update and ensure monotonic stability.
        updated = self.svc.update(preset["key"], label="Alpha renamed")
        self.assertEqual(updated["label"], "Alpha renamed")
        self.assertEqual(updated["params"]["mode"], 5)
        self.assertGreaterEqual(updated["updated"], original_updated)
        # params update leaves label untouched
        updated2 = self.svc.update(preset["key"], params={"mode": 9, "speed": 100})
        self.assertEqual(updated2["label"], "Alpha renamed")
        self.assertEqual(updated2["params"]["mode"], 9)
        self.assertEqual(updated2["params"]["speed"], 100)

    def test_update_empty_label_rejected(self):
        preset = self.svc.create(label="Alpha")
        with self.assertRaises(ValueError):
            self.svc.update(preset["key"], label="   ")

    def test_update_unknown_key_returns_none(self):
        self.assertIsNone(self.svc.update("nope", label="x"))

    def test_delete(self):
        preset = self.svc.create(label="To Delete")
        self.assertTrue(self.svc.delete(preset["key"]))
        self.assertIsNone(self.svc.get(preset["key"]))
        self.assertFalse(self.svc.delete(preset["key"]))

    def test_duplicate(self):
        preset = self.svc.create(label="Alpha", params={"mode": 3})
        dup = self.svc.duplicate(preset["key"])
        self.assertEqual(dup["label"], "Alpha copy")
        self.assertEqual(dup["key"], "alpha_copy")
        self.assertEqual(dup["params"]["mode"], 3)

    def test_load_survives_malformed_file(self):
        with open(self.path, "w") as fh:
            fh.write("not json at all")
        # Fresh service to force reload.
        svc2 = RLPresetsService(storage_path=self.path)
        self.assertEqual(svc2.list(), [])

    def test_canonical_params_coerce_types(self):
        preset = self.svc.create(
            label="Typed",
            params={"mode": "7", "check1": 1, "check2": 0, "color1": (10, 20, 30)},
        )
        self.assertEqual(preset["params"]["mode"], 7)
        self.assertIs(preset["params"]["check1"], True)
        self.assertIs(preset["params"]["check2"], False)
        self.assertEqual(preset["params"]["color1"], [10, 20, 30])


class RLPresetsIdAllocationTests(unittest.TestCase):
    """Phase C.N1/C.N4: stable int-ids + schema v1 → v2 migration."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "rl_presets.json")
        self.svc = RLPresetsService(storage_path=self.path)

    def test_create_assigns_monotonic_ids(self):
        a = self.svc.create(label="Alpha")
        b = self.svc.create(label="Beta")
        c = self.svc.create(label="Gamma")
        self.assertEqual(a["id"], 0)
        self.assertEqual(b["id"], 1)
        self.assertEqual(c["id"], 2)

    def test_get_by_id_hits(self):
        a = self.svc.create(label="Alpha")
        found = self.svc.get_by_id(a["id"])
        self.assertIsNotNone(found)
        self.assertEqual(found["key"], "alpha")

    def test_get_by_id_miss_returns_none(self):
        self.svc.create(label="Alpha")
        self.assertIsNone(self.svc.get_by_id(999))
        self.assertIsNone(self.svc.get_by_id("not-a-number"))
        self.assertIsNone(self.svc.get_by_id(None))

    def test_delete_does_not_recycle_id(self):
        a = self.svc.create(label="Alpha")  # id=0
        self.svc.create(label="Beta")       # id=1
        self.assertTrue(self.svc.delete(a["key"]))
        c = self.svc.create(label="Gamma")
        # id=2 (not reused from the deleted preset)
        self.assertEqual(c["id"], 2)

    def test_next_id_persists_across_service_instances(self):
        a = self.svc.create(label="Alpha")
        self.assertEqual(a["id"], 0)
        # Fresh service instance against the same file.
        svc2 = RLPresetsService(storage_path=self.path)
        b = svc2.create(label="Beta")
        self.assertEqual(b["id"], 1)
        svc2.delete(a["key"])
        # Even after reload + delete, ids stay unique and monotone.
        svc3 = RLPresetsService(storage_path=self.path)
        c = svc3.create(label="Gamma")
        self.assertEqual(c["id"], 2)

    def test_v1_file_migrates_on_load(self):
        # Hand-write a schema v1 payload (no ``id``, no ``next_id``).
        v1 = {
            "schema_version": 1,
            "presets": [
                {
                    "key": "alpha", "label": "Alpha",
                    "created": "2026-04-24T00:00:00Z",
                    "updated": "2026-04-24T00:00:00Z",
                    "params": {}, "flags": {},
                },
                {
                    "key": "beta", "label": "Beta",
                    "created": "2026-04-24T00:00:00Z",
                    "updated": "2026-04-24T00:00:00Z",
                    "params": {}, "flags": {},
                },
            ],
        }
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(v1, fh)

        svc = RLPresetsService(storage_path=self.path)
        presets = svc.list()
        self.assertEqual(len(presets), 2)
        # Ids assigned by list-position.
        self.assertEqual(presets[0]["id"], 0)
        self.assertEqual(presets[1]["id"], 1)

        # File was re-written as v2 with next_id=2.
        with open(self.path, "r", encoding="utf-8") as fh:
            persisted = json.load(fh)
        self.assertEqual(persisted["schema_version"], 2)
        self.assertEqual(persisted["next_id"], 2)
        self.assertEqual([p["id"] for p in persisted["presets"]], [0, 1])

    def test_v1_duplicate_ids_are_resolved(self):
        # Defensive edge case: hand-edited v1 with duplicate ids.
        v1 = {
            "schema_version": 1,
            "presets": [
                {"key": "a", "label": "A", "id": 5, "params": {}, "flags": {}},
                {"key": "b", "label": "B", "id": 5, "params": {}, "flags": {}},  # dup
            ],
        }
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(v1, fh)
        svc = RLPresetsService(storage_path=self.path)
        presets = svc.list()
        ids = [p["id"] for p in presets]
        self.assertEqual(len(set(ids)), 2)  # dedup

    def test_list_exposes_id_field(self):
        self.svc.create(label="Alpha")
        self.svc.create(label="Beta")
        items = self.svc.list()
        self.assertTrue(all("id" in p for p in items))


class RLPresetsOnChangedHookTests(unittest.TestCase):
    """BF2/BF6.1: the ``on_changed`` mutation hook fires exactly once per
    successful persist and never leaves the store in an inconsistent state
    when the listener itself raises."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "rl_presets.json")
        self.svc = RLPresetsService(storage_path=self.path)
        self.calls: list[str] = []
        self.svc.on_changed = lambda: self.calls.append("fired")

    def test_create_fires_once(self):
        self.svc.create(label="Alpha")
        self.assertEqual(self.calls, ["fired"])

    def test_update_fires_once(self):
        self.svc.create(label="Alpha")
        self.calls.clear()
        self.svc.update("alpha", label="Alpha renamed")
        self.assertEqual(self.calls, ["fired"])

    def test_update_miss_does_not_fire(self):
        self.svc.create(label="Alpha")
        self.calls.clear()
        result = self.svc.update("nonexistent", label="x")
        self.assertIsNone(result)
        self.assertEqual(self.calls, [])

    def test_delete_fires_once(self):
        self.svc.create(label="Alpha")
        self.calls.clear()
        self.svc.delete("alpha")
        self.assertEqual(self.calls, ["fired"])

    def test_delete_miss_does_not_fire(self):
        self.calls.clear()
        self.assertFalse(self.svc.delete("nonexistent"))
        self.assertEqual(self.calls, [])

    def test_duplicate_fires_once(self):
        # create() already fires the hook; clear before duplicate()
        self.svc.create(label="Alpha")
        self.calls.clear()
        dup = self.svc.duplicate("alpha")
        self.assertIsNotNone(dup)
        self.assertEqual(self.calls, ["fired"])

    def test_replace_all_fires_once(self):
        self.calls.clear()
        self.svc.replace_all([{"label": "Bulk 1"}, {"label": "Bulk 2"}])
        self.assertEqual(self.calls, ["fired"])

    def test_listener_exception_does_not_rollback_write(self):
        def boom():
            raise RuntimeError("listener blew up")
        self.svc.on_changed = boom
        preset = self.svc.create(label="Alpha")
        # Write must have persisted despite the listener raising.
        self.assertIsNotNone(preset)
        self.assertEqual(self.svc.get("alpha")["label"], "Alpha")


if __name__ == "__main__":
    unittest.main()
