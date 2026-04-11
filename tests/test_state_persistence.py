import unittest

from racelink.state.persistence import dump_records, load_records


class PersistenceTests(unittest.TestCase):
    def test_dump_records_serializes_objects_to_json(self):
        class Obj:
            def __init__(self):
                self.addr = "AABBCCDDEEFF"
                self.groupId = 3

        raw = dump_records([Obj()])

        self.assertEqual(raw, '[{"addr": "AABBCCDDEEFF", "groupId": 3}]')

    def test_load_records_reads_json_and_legacy_literal(self):
        json_raw = '[{"addr":"AABBCCDDEEFF","groupId":1}]'
        legacy_raw = "[{'addr': '112233445566', 'groupId': 2}]"

        self.assertEqual(load_records(json_raw), [{"addr": "AABBCCDDEEFF", "groupId": 1}])
        self.assertEqual(load_records(legacy_raw), [{"addr": "112233445566", "groupId": 2}])

    def test_load_records_falls_back_to_default_without_eval(self):
        default = [{"addr": "FFEEDDCCBBAA", "groupId": 9}]

        self.assertEqual(load_records("not valid", default=default), default)
        self.assertEqual(load_records(None, default=default), default)


if __name__ == "__main__":
    unittest.main()
