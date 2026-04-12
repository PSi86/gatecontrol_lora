import unittest

from racelink import racelink_proto_auto as RLPA
from racelink.protocol import addressing, codec, packets, rules


class ProtocolTests(unittest.TestCase):
    def test_protocol_rules_and_opcode_names(self):
        self.assertEqual(rules.response_policy(0x05), rules.RESP_ACK)
        self.assertEqual(rules.response_opcode(0x03), 0x03)
        self.assertEqual(rules.opcode_name(0x05), "CONFIG")
        self.assertEqual(rules.request_direction(0x07), rules.DIR_M2N)

    def test_protocol_packet_builders_and_addressing(self):
        self.assertEqual(packets.build_get_devices_body(2, 3), b"\x02\x03")
        self.assertEqual(packets.build_set_group_body(9), b"\x09")
        self.assertEqual(packets.build_control_body(1, 2, 3, 4), b"\x01\x02\x03\x04")
        self.assertEqual(packets.build_config_body(5, 1, 2, 3, 4), b"\x05\x01\x02\x03\x04")
        self.assertEqual(packets.build_sync_body(0x123456, 0x44), b"\x56\x34\x12\x44")
        self.assertEqual(packets.build_stream_body(0xA5, b"\x01\x02"), b"\xA5\x01\x02")
        self.assertEqual(addressing.to_hex_str("aa:bb:cc:dd:ee:ff"), "AABBCCDDEEFF")
        self.assertEqual(addressing.last3_hex("aa:bb:cc:dd:ee:ff"), "DDEEFF")

    def test_generated_struct_sizes_match_manual_packet_builders(self):
        self.assertEqual(len(packets.build_get_devices_body(1, 2)), RLPA.SZ_P_GetDevices)
        self.assertEqual(len(packets.build_set_group_body(3)), RLPA.SZ_P_SetGroup)
        self.assertEqual(len(packets.build_control_body(1, 2, 3, 4)), RLPA.SZ_P_Control)
        self.assertEqual(len(packets.build_config_body(5, 1, 2, 3, 4)), RLPA.SZ_P_Config)
        self.assertEqual(len(packets.build_sync_body(0x123456, 0x44)), RLPA.SZ_P_Sync)
        self.assertEqual(len(packets.build_stream_body(0xA5, b"\x01\x02\x03\x04\x05\x06\x07\x08")), RLPA.SZ_P_Stream)
        self.assertEqual(RLPA.SZ_P_IdentifyReply, 9)
        self.assertEqual(RLPA.SZ_P_StatusReply, 8)
        self.assertEqual(RLPA.SZ_P_Ack, 3)

    def test_generated_struct_fields_match_header_contract_used_by_python(self):
        self.assertEqual(
            RLPA.STRUCT_FIELDS["P_GetDevices"],
            [("groupId", "uint8_t", 1), ("flags", "uint8_t", 1)],
        )
        self.assertEqual(
            RLPA.STRUCT_FIELDS["P_Control"],
            [("groupId", "uint8_t", 1), ("flags", "uint8_t", 1), ("presetId", "uint8_t", 1), ("brightness", "uint8_t", 1)],
        )
        self.assertEqual(
            RLPA.STRUCT_FIELDS["P_Sync"],
            [("ts24_0", "uint8_t", 1), ("ts24_1", "uint8_t", 1), ("ts24_2", "uint8_t", 1), ("brightness", "uint8_t", 1)],
        )
        self.assertEqual(
            RLPA.STRUCT_FIELDS["P_IdentifyReply"],
            [("fw", "uint8_t", 1), ("caps", "uint8_t", 1), ("groupId", "uint8_t", 1), ("mac6", "uint8_t", 6)],
        )
        self.assertEqual(
            RLPA.STRUCT_FIELDS["P_StatusReply"],
            [
                ("flags", "uint8_t", 1),
                ("configByte", "uint8_t", 1),
                ("presetId", "uint8_t", 1),
                ("brightness", "uint8_t", 1),
                ("vbat_mV", "uint16_t", 1),
                ("rssi", "int8_t", 1),
                ("snr", "int8_t", 1),
            ],
        )

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

    def test_protocol_codec_parses_identify_reply_using_generated_size(self):
        identify_body = b"\x04\x21\x09" + bytes.fromhex("AABBCCDDEEFF")
        self.assertEqual(len(identify_body), RLPA.SZ_P_IdentifyReply)
        identify_payload = bytes.fromhex("AABBCC11223300") + identify_body + b"\x00\x00\x00"
        identify_event = codec.parse_reply_event(0x01, identify_payload, timestamp=3.0, host_rssi=-42, host_snr=6, rx_windows=1)

        self.assertEqual(identify_event["reply"], "IDENTIFY_REPLY")
        self.assertEqual(identify_event["version"], 0x04)
        self.assertEqual(identify_event["caps"], 0x21)
        self.assertEqual(identify_event["groupId"], 0x09)
        self.assertEqual(identify_event["mac6"], bytes.fromhex("AABBCCDDEEFF"))


if __name__ == "__main__":
    unittest.main()
