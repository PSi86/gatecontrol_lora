import unittest

from racelink.protocol import addressing, codec, packets, rules


class ProtocolTests(unittest.TestCase):
    def test_protocol_rules_and_opcode_names(self):
        self.assertEqual(rules.response_policy(0x05), rules.RESP_ACK)
        self.assertEqual(rules.response_opcode(0x03), 0x03)
        self.assertEqual(rules.opcode_name(0x05), "CONFIG")
        self.assertEqual(rules.request_direction(0x07), rules.DIR_M2N)

    def test_protocol_packet_builders_and_addressing(self):
        self.assertEqual(packets.build_config_body(5, 1, 2, 3, 4), b"\x05\x01\x02\x03\x04")
        self.assertEqual(packets.build_sync_body(0x123456, 0x44), b"\x56\x34\x12\x44")
        self.assertEqual(packets.build_stream_body(0xA5, b"\x01\x02"), b"\xA5\x01\x02")
        self.assertEqual(addressing.to_hex_str("aa:bb:cc:dd:ee:ff"), "AABBCCDDEEFF")
        self.assertEqual(addressing.last3_hex("aa:bb:cc:dd:ee:ff"), "DDEEFF")

    def test_protocol_codec_parses_ack_and_status_reply(self):
        ack_payload = bytes.fromhex("AABBCC11223300") + bytes([0x05, 0x00, 0x09]) + b"\x00\x00\x00"
        ack_event = codec.parse_reply_event(0x7E, ack_payload, timestamp=1.0, host_rssi=-50, host_snr=7, rx_windows=1)

        self.assertEqual(ack_event["reply"], "ACK")
        self.assertEqual(ack_event["ack_of"], 0x05)
        self.assertEqual(ack_event["ack_status"], 0)
        self.assertEqual(ack_event["ack_seq"], 0x09)

        status_body = b"\x11\x22\x33\x44\x20\x03\xF6\x04"
        status_payload = bytes.fromhex("AABBCC11223300") + status_body + b"\x00\x00\x00"
        status_event = codec.parse_reply_event(0x03, status_payload, timestamp=2.0, host_rssi=-45, host_snr=5, rx_windows=1)

        self.assertEqual(status_event["reply"], "STATUS_REPLY")
        self.assertEqual(status_event["flags"], 0x11)
        self.assertEqual(status_event["configByte"], 0x22)
        self.assertEqual(status_event["presetId"], 0x33)
        self.assertEqual(status_event["brightness"], 0x44)


if __name__ == "__main__":
    unittest.main()
