import logging
import unittest

from racelink.state.migrations import migrate_state
from racelink.state.persistence import (
    CURRENT_SCHEMA_VERSION,
    dump_records,
    dump_state,
    load_records,
    load_state,
)


class PersistenceTests(unittest.TestCase):
    def test_dump_records_serializes_objects_to_json(self):
        class Obj:
            def __init__(self):
                self.addr = "AABBCCDDEEFF"
                self.groupId = 3

        raw = dump_records([Obj()])

        self.assertEqual(raw, '[{"addr": "AABBCCDDEEFF", "groupId": 3}]')

    def test_load_records_reads_json(self):
        json_raw = '[{"addr":"AABBCCDDEEFF","groupId":1}]'
        self.assertEqual(load_records(json_raw), [{"addr": "AABBCCDDEEFF", "groupId": 1}])

    def test_load_records_rejects_python_repr_and_warns(self):
        # The legacy ast.literal_eval fallback was removed (plan P1-3). Python-repr
        # strings now fail decoding, log a WARNING, and fall back to default.
        legacy_raw = "[{'addr': '112233445566', 'groupId': 2}]"
        default = [{"addr": "FALLBACK", "groupId": 9}]
        with self.assertLogs("racelink.state.persistence", level="WARNING") as captured:
            result = load_records(legacy_raw, default=default, source="test_key")
        self.assertEqual(result, default)
        self.assertTrue(any("test_key" in msg for msg in captured.output))

    def test_load_records_falls_back_to_default_without_eval(self):
        default = [{"addr": "FFEEDDCCBBAA", "groupId": 9}]

        with self.assertLogs("racelink.state.persistence", level="WARNING"):
            self.assertEqual(load_records("not valid", default=default), default)
        self.assertEqual(load_records(None, default=default), default)


class CombinedStateTests(unittest.TestCase):
    def test_dump_and_load_state_roundtrip(self):
        devices = [{"addr": "AA", "groupId": 1}]
        groups = [{"name": "All", "static_group": 1, "dev_type": 0}]

        raw = dump_state(devices, groups)
        loaded_devices, loaded_groups, version = load_state(raw)

        self.assertEqual(loaded_devices, devices)
        self.assertEqual(loaded_groups, groups)
        self.assertEqual(version, CURRENT_SCHEMA_VERSION)

    def test_load_state_missing_returns_zero_version(self):
        devices, groups, version = load_state(None, default_devices=[], default_groups=[])
        self.assertEqual(devices, [])
        self.assertEqual(groups, [])
        self.assertEqual(version, 0)

    def test_load_state_malformed_returns_zero_and_warns(self):
        with self.assertLogs("racelink.state.persistence", level="WARNING"):
            devices, groups, version = load_state(
                "{not json",
                default_devices=[{"addr": "DEF"}],
                default_groups=[{"name": "DEF"}],
                source="rl_state_v1",
            )
        self.assertEqual(devices, [{"addr": "DEF"}])
        self.assertEqual(groups, [{"name": "DEF"}])
        self.assertEqual(version, 0)

    def test_load_state_accepts_dict_payload(self):
        payload = {
            "schema_version": 1,
            "devices": [{"addr": "CC"}],
            "groups": [{"name": "G"}],
        }
        devices, groups, version = load_state(payload)
        self.assertEqual(devices, [{"addr": "CC"}])
        self.assertEqual(groups, [{"name": "G"}])
        self.assertEqual(version, 1)

    def test_migrate_state_is_noop_for_current_version(self):
        devices = [{"addr": "AA"}]
        groups = [{"name": "G"}]
        out_dev, out_grp, out_ver = migrate_state(
            devices, groups, from_version=CURRENT_SCHEMA_VERSION
        )
        self.assertEqual(out_dev, devices)
        self.assertEqual(out_grp, groups)
        self.assertEqual(out_ver, CURRENT_SCHEMA_VERSION)


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    unittest.main()
