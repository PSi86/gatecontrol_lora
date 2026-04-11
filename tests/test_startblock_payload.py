import unittest

from racelink.services import build_startblock_payload_v1


class StartblockPayloadTests(unittest.TestCase):
    def test_startblock_payload_encodes_header_and_name(self):
        payload = build_startblock_payload_v1(slot=3, channel_label="R1", pilot_name="Pilot 42")

        self.assertEqual(payload[0], 0x01)
        self.assertEqual(payload[1], 3)
        self.assertEqual(payload[2:4], b"R1")
        self.assertEqual(payload[4], len(payload[5:]))
        self.assertEqual(payload[5:], b"PILOT 42")

    def test_startblock_payload_normalizes_and_truncates_channel(self):
        payload = build_startblock_payload_v1(slot=255, channel_label="abc", pilot_name="pilot")

        self.assertEqual(payload[1], 255)
        self.assertEqual(payload[2:4], b"AB")
        self.assertEqual(payload[5:], b"PILOT")


if __name__ == "__main__":
    unittest.main()
