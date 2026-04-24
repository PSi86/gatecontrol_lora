"""Tests for atomic persistence behavior on RaceLink_Host (plan P1-5 / P2-7)."""

from __future__ import annotations

import json
import unittest
from typing import Any

from controller import RaceLink_Host
from racelink.domain import RL_Device, RL_DeviceGroup, state_scope


class FakeDb:
    def __init__(self, initial: dict[str, Any] | None = None):
        self._options: dict[str, Any] = dict(initial or {})
        self.sets: list[tuple[str, Any]] = []

    def option(self, key: str, default: Any = None) -> Any:
        return self._options.get(key, default)

    def option_set(self, key: str, value: Any) -> None:
        self._options[key] = value
        self.sets.append((key, value))


class FakeUi:
    def message_notify(self, msg: str) -> None:  # pragma: no cover - not used
        pass

    def broadcast_ui(self, panel: str) -> None:  # pragma: no cover - not used
        pass


class FakeRhApi:
    def __init__(self, db: FakeDb):
        self.db = db
        self.ui = FakeUi()

    def __call__(self, *_args, **_kwargs):  # pragma: no cover
        return ""


def _make_host(db: FakeDb) -> RaceLink_Host:
    return RaceLink_Host(FakeRhApi(db), "name", "label")


class ControllerPersistenceTests(unittest.TestCase):
    def test_save_to_db_writes_only_combined_key(self):
        db = FakeDb()
        host = _make_host(db)
        host.device_repository.replace_all([RL_Device("AABBCCDDEEFF", 10, "N1", groupId=2)])
        host.group_repository.replace_all(
            [
                RL_DeviceGroup("Group A", 0, 0),
                RL_DeviceGroup("All WLED Nodes", 1, 0),
            ]
        )

        host.save_to_db({})

        # Only the combined key should have been written.
        self.assertEqual(len(db.sets), 1)
        key, value = db.sets[0]
        self.assertEqual(key, "rl_state_v1")

        payload = json.loads(value)
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(len(payload["devices"]), 1)
        self.assertEqual(payload["devices"][0]["addr"], "AABBCCDDEEFF")
        self.assertEqual(len(payload["groups"]), 2)

    def test_load_from_db_prefers_combined_key(self):
        state = json.dumps(
            {
                "schema_version": 1,
                "devices": [
                    {
                        "addr": "001122334455",
                        "dev_type": 10,
                        "name": "Direct",
                        "groupId": 4,
                        "flags": 1,
                        "presetId": 3,
                        "brightness": 128,
                    }
                ],
                "groups": [
                    {"name": "G1", "static_group": 0, "dev_type": 0},
                    {"name": "All WLED Nodes", "static_group": 1, "dev_type": 0},
                ],
            }
        )
        db = FakeDb({"rl_state_v1": state})
        host = _make_host(db)

        host.load_from_db()

        devices = host.device_repository.list()
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0].addr, "001122334455")
        self.assertEqual(devices[0].groupId, 4)
        self.assertEqual(devices[0].brightness, 128)

        # Combined key is already current schema version -> no re-save.
        self.assertEqual(db.sets, [])

    def test_load_from_db_migrates_legacy_keys_and_saves_combined(self):
        legacy_devices = json.dumps(
            [
                {
                    "addr": "AA11BB22CC33",
                    "dev_type": 10,
                    "name": "Legacy",
                    "groupId": 1,
                    "flags": 1,
                    "presetId": 2,
                    "brightness": 64,
                }
            ]
        )
        legacy_groups = json.dumps(
            [
                {"name": "Legacy Group", "static_group": 0, "dev_type": 0},
                {"name": "All WLED Nodes", "static_group": 1, "dev_type": 0},
            ]
        )
        db = FakeDb(
            {
                "rl_device_config": legacy_devices,
                "rl_groups_config": legacy_groups,
            }
        )
        host = _make_host(db)

        host.load_from_db()

        # Combined key must now contain the migrated payload.
        written_keys = {key for key, _ in db.sets}
        self.assertIn("rl_state_v1", written_keys)
        combined = db.option("rl_state_v1", None)
        self.assertIsNotNone(combined)
        payload = json.loads(combined)
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["devices"][0]["addr"], "AA11BB22CC33")

        # Legacy keys are preserved for rollback safety.
        self.assertEqual(db.option("rl_device_config", None), legacy_devices)
        self.assertEqual(db.option("rl_groups_config", None), legacy_groups)

    def test_load_from_db_migrates_legacy_python_repr(self):
        """Plan P1-3: pre-JSON Python-repr values are salvaged once + re-saved."""
        # Old RotorHazard option written with ast.literal-compatible format.
        legacy_devices_repr = (
            "[{'addr': 'AA11BB22CC33', 'dev_type': 10, 'name': 'OldRepr', "
            "'groupId': 2, 'flags': 1, 'presetId': 2, 'brightness': 32}]"
        )
        legacy_groups_repr = (
            "[{'name': 'Repr Group', 'static_group': 0, 'dev_type': 0}, "
            "{'name': 'All WLED Nodes', 'static_group': 1, 'dev_type': 0}]"
        )
        db = FakeDb(
            {
                "rl_device_config": legacy_devices_repr,
                "rl_groups_config": legacy_groups_repr,
            }
        )
        host = _make_host(db)
        host.load_from_db()

        devices = host.device_repository.list()
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0].addr, "AA11BB22CC33")
        self.assertEqual(devices[0].groupId, 2)

        # Combined key was saved -- legacy payload is now bridged.
        combined = db.option("rl_state_v1", None)
        self.assertIsNotNone(combined)
        payload = json.loads(combined)
        self.assertEqual(payload["devices"][0]["name"], "OldRepr")

    def test_save_to_db_forwards_scopes_to_persistence_callback(self):
        db = FakeDb()
        host = _make_host(db)
        received: list = []
        host.on_persistence_changed = lambda scopes=None: received.append(scopes)

        host.save_to_db({}, scopes={state_scope.DEVICE_SPECIALS})

        self.assertEqual(len(received), 1)
        self.assertEqual(set(received[0]), {state_scope.DEVICE_SPECIALS})

    def test_save_to_db_default_scope_is_full(self):
        db = FakeDb()
        host = _make_host(db)
        received: list = []
        host.on_persistence_changed = lambda scopes=None: received.append(scopes)

        host.save_to_db({})

        self.assertEqual(len(received), 1)
        self.assertEqual(set(received[0]), {state_scope.FULL})

    def test_save_to_db_supports_legacy_no_arg_callback(self):
        """Plugins written before the scopes hook still receive a plain call."""
        db = FakeDb()
        host = _make_host(db)
        called = []

        def legacy_callback():
            called.append(True)

        host.on_persistence_changed = legacy_callback
        host.save_to_db({}, scopes={state_scope.GROUPS})

        self.assertEqual(called, [True])

    def test_load_from_db_fresh_install_seeds_from_backups(self):
        db = FakeDb()
        host = _make_host(db)
        host.load_from_db()

        # Fresh install should populate some defaults and write the combined key.
        written_keys = {key for key, _ in db.sets}
        self.assertIn("rl_state_v1", written_keys)
        self.assertTrue(any(
            getattr(g, "name", "").lower() == "all wled nodes"
            for g in host.group_repository.list()
        ))


if __name__ == "__main__":
    unittest.main()
