import unittest

from racelink import racelink_proto_auto as RLPA
from racelink.protocol import addressing, codec, packets, rules
from racelink.protocol.packets import (
    RL_CTRL_E_COLOR1,
    RL_CTRL_E_COLOR2,
    RL_CTRL_E_COLOR3,
    RL_CTRL_E_PALETTE,
    RL_CTRL_F_BRIGHTNESS,
    RL_CTRL_F_CUSTOM1,
    RL_CTRL_F_CUSTOM2,
    RL_CTRL_F_CUSTOM3_CHECKS,
    RL_CTRL_F_EXT,
    RL_CTRL_F_INTENSITY,
    RL_CTRL_F_MODE,
    RL_CTRL_F_SPEED,
    build_control_body,
)


def _parse_control_adv(body: bytes) -> dict:
    """Reference parser mirroring the layout documented in racelink_proto.h.

    Used only by tests to validate round-trip of build_control_body.
    """
    i = 0
    group_id = body[i]; i += 1
    flags = body[i]; i += 1
    field_mask = body[i]; i += 1
    out: dict = {"groupId": group_id, "flags": flags, "fieldMask": field_mask}

    if field_mask & RL_CTRL_F_BRIGHTNESS:
        out["brightness"] = body[i]; i += 1
    if field_mask & RL_CTRL_F_MODE:
        out["mode"] = body[i]; i += 1
    if field_mask & RL_CTRL_F_SPEED:
        out["speed"] = body[i]; i += 1
    if field_mask & RL_CTRL_F_INTENSITY:
        out["intensity"] = body[i]; i += 1
    if field_mask & RL_CTRL_F_CUSTOM1:
        out["custom1"] = body[i]; i += 1
    if field_mask & RL_CTRL_F_CUSTOM2:
        out["custom2"] = body[i]; i += 1
    if field_mask & RL_CTRL_F_CUSTOM3_CHECKS:
        packed = body[i]; i += 1
        out["custom3"] = packed & 0x1F
        out["check1"] = bool(packed & 0x20)
        out["check2"] = bool(packed & 0x40)
        out["check3"] = bool(packed & 0x80)

    if field_mask & RL_CTRL_F_EXT:
        ext_mask = body[i]; i += 1
        out["extMask"] = ext_mask
        if ext_mask & RL_CTRL_E_PALETTE:
            out["palette"] = body[i]; i += 1
        for key, bit in (("color1", RL_CTRL_E_COLOR1), ("color2", RL_CTRL_E_COLOR2), ("color3", RL_CTRL_E_COLOR3)):
            if ext_mask & bit:
                out[key] = (body[i], body[i + 1], body[i + 2])
                i += 3

    assert i == len(body), f"trailing bytes after parse: {i} != {len(body)}"
    return out


class ProtocolTests(unittest.TestCase):
    def test_protocol_rules_and_opcode_names(self):
        self.assertEqual(rules.response_policy(0x05), rules.RESP_ACK)
        self.assertEqual(rules.response_opcode(0x03), 0x03)
        self.assertEqual(rules.opcode_name(0x05), "CONFIG")
        self.assertEqual(rules.request_direction(0x07), rules.DIR_M2N)

    def test_protocol_packet_builders_and_addressing(self):
        self.assertEqual(packets.build_get_devices_body(2, 3), b"\x02\x03")
        self.assertEqual(packets.build_set_group_body(9), b"\x09")
        self.assertEqual(packets.build_preset_body(1, 2, 3, 4), b"\x01\x02\x03\x04")
        self.assertEqual(packets.build_config_body(5, 1, 2, 3, 4), b"\x05\x01\x02\x03\x04")
        # Default SYNC body is the legacy 4 B clock-tick form (no flags
        # byte): autosync uses this so devices skip pending arm-on-sync
        # materialisation. The deliberate-fire path passes the flags kwarg.
        self.assertEqual(packets.build_sync_body(0x123456, 0x44), b"\x56\x34\x12\x44")
        # flags=0 explicitly is identical to default (no trailing byte).
        self.assertEqual(packets.build_sync_body(0x123456, 0x44, flags=0), b"\x56\x34\x12\x44")
        # flags=SYNC_FLAG_TRIGGER_ARMED appends the flag byte at offset 4.
        self.assertEqual(
            packets.build_sync_body(0x123456, 0x44, flags=packets.SYNC_FLAG_TRIGGER_ARMED),
            b"\x56\x34\x12\x44\x01",
        )
        # OPC_OFFSET is now variable-length tagged-union. Common header is
        # groupId(1) + mode(1); per-mode payload follows.
        # NONE: 2 B (header only).
        self.assertEqual(packets.build_offset_body(7, "none"), b"\x07\x00")
        # EXPLICIT: 4 B (uint16 LE offset_ms). String mode also accepted.
        self.assertEqual(packets.build_offset_body(7, "explicit", offset_ms=0x1234), b"\x07\x01\x34\x12")
        self.assertEqual(packets.build_offset_body(255, "explicit", offset_ms=0xFFFF), b"\xFF\x01\xFF\xFF")
        # LINEAR: 6 B (int16 base, int16 step). Negative step round-trips.
        self.assertEqual(packets.build_offset_body(255, "linear", base_ms=0, step_ms=100),
                         b"\xFF\x02\x00\x00\x64\x00")
        self.assertEqual(packets.build_offset_body(255, "linear", base_ms=500, step_ms=-200),
                         b"\xFF\x02\xF4\x01\x38\xFF")
        # VSHAPE: 7 B (int16 base, int16 step, uint8 center).
        self.assertEqual(packets.build_offset_body(255, "vshape", base_ms=0, step_ms=50, center=8),
                         b"\xFF\x03\x00\x00\x32\x00\x08")
        # MODULO: 7 B (int16 base, int16 step, uint8 cycle).
        self.assertEqual(packets.build_offset_body(255, "modulo", base_ms=0, step_ms=100, cycle=4),
                         b"\xFF\x04\x00\x00\x64\x00\x04")
        # Builder clamps out-of-range offset values to keep on-wire bytes valid.
        self.assertEqual(packets.build_offset_body(0, "explicit", offset_ms=-1), b"\x00\x01\x00\x00")
        self.assertEqual(packets.build_offset_body(0, "explicit", offset_ms=0x10000), b"\x00\x01\xFF\xFF")
        # parse_offset_body round-trips every mode.
        for body in [
            packets.build_offset_body(255, "none"),
            packets.build_offset_body(7, "explicit", offset_ms=400),
            packets.build_offset_body(255, "linear", base_ms=0, step_ms=100),
            packets.build_offset_body(255, "vshape", base_ms=0, step_ms=50, center=5),
            packets.build_offset_body(255, "modulo", base_ms=10, step_ms=20, cycle=3),
        ]:
            decoded = packets.parse_offset_body(body)
            self.assertIn("mode", decoded)
            self.assertIn("group_id", decoded)
        self.assertEqual(addressing.to_hex_str("aa:bb:cc:dd:ee:ff"), "AABBCCDDEEFF")
        self.assertEqual(addressing.last3_hex("aa:bb:cc:dd:ee:ff"), "DDEEFF")

    def test_generated_struct_sizes_match_manual_packet_builders(self):
        self.assertEqual(len(packets.build_get_devices_body(1, 2)), RLPA.SZ_P_GetDevices)
        self.assertEqual(len(packets.build_set_group_body(3)), RLPA.SZ_P_SetGroup)
        self.assertEqual(len(packets.build_preset_body(1, 2, 3, 4)), RLPA.SZ_P_Preset)
        self.assertEqual(len(packets.build_config_body(5, 1, 2, 3, 4)), RLPA.SZ_P_Config)
        # P_Sync is now variable-length (4 B legacy / 5 B with flags). The
        # auto-generated SZ_P_Sync reflects the full struct (5 B). The
        # default emit is 4 B for back-compat with the autosync clock-tick
        # form; emitting with the trigger flag matches the full struct size.
        self.assertEqual(len(packets.build_sync_body(0x123456, 0x44)), 4)
        self.assertEqual(
            len(packets.build_sync_body(0x123456, 0x44, flags=packets.SYNC_FLAG_TRIGGER_ARMED)),
            RLPA.SZ_P_Sync,
        )
        self.assertEqual(RLPA.SZ_P_IdentifyReply, 9)
        self.assertEqual(RLPA.SZ_P_StatusReply, 8)
        self.assertEqual(RLPA.SZ_P_Ack, 3)
        self.assertEqual(RLPA.OPC_OFFSET, 0x09)
        # OPC_OFFSET is now variable-length; no fixed SZ_P_Offset constant.
        # The mode enum constants are exposed instead.
        self.assertEqual(RLPA.OFFSET_MODE_NONE,     0x00)
        self.assertEqual(RLPA.OFFSET_MODE_EXPLICIT, 0x01)
        self.assertEqual(RLPA.OFFSET_MODE_LINEAR,   0x02)
        self.assertEqual(RLPA.OFFSET_MODE_VSHAPE,   0x03)
        self.assertEqual(RLPA.OFFSET_MODE_MODULO,   0x04)

    def test_generated_struct_fields_match_header_contract_used_by_python(self):
        self.assertEqual(
            RLPA.STRUCT_FIELDS["P_GetDevices"],
            [("groupId", "uint8_t", 1), ("flags", "uint8_t", 1)],
        )
        self.assertEqual(
            RLPA.STRUCT_FIELDS["P_Preset"],
            [("groupId", "uint8_t", 1), ("flags", "uint8_t", 1), ("presetId", "uint8_t", 1), ("brightness", "uint8_t", 1)],
        )
        self.assertEqual(
            RLPA.STRUCT_FIELDS["P_Sync"],
            [
                ("ts24_0", "uint8_t", 1),
                ("ts24_1", "uint8_t", 1),
                ("ts24_2", "uint8_t", 1),
                ("brightness", "uint8_t", 1),
                ("flags", "uint8_t", 1),
            ],
        )
        # P_Offset is no longer a fixed packed struct (variable-length wire
        # body now). The auto-generator drops it from STRUCT_FIELDS; verify
        # this is the case so a regression that re-adds a fixed shape would
        # surface here.
        self.assertNotIn("P_Offset", RLPA.STRUCT_FIELDS)
        self.assertEqual(
            RLPA.STRUCT_FIELDS["P_IdentifyReply"],
            [("fw", "uint8_t", 1), ("caps", "uint8_t", 1), ("groupId", "uint8_t", 1), ("mac6", "uint8_t", 6)],
        )
        self.assertEqual(
            RLPA.STRUCT_FIELDS["P_StatusReply"],
            [
                ("flags", "uint8_t", 1),
                ("configByte", "uint8_t", 1),
                ("effectId", "uint8_t", 1),
                ("brightness", "uint8_t", 1),
                ("vbat_mV", "uint16_t", 1),
                ("rssi", "int8_t", 1),
                ("snr", "int8_t", 1),
            ],
        )

    def test_protocol_codec_parses_ack_and_status_reply(self):
        ack_payload = bytes.fromhex("AABBCC11223300") + bytes([0x05, 0x00, 0x09]) + b"\x00\x00\x00"
        ack_event = codec.parse_reply_event(0x7E, ack_payload, timestamp=1.0, host_rssi=-50, host_snr=7)

        self.assertEqual(ack_event["reply"], "ACK")
        self.assertEqual(ack_event["ack_of"], 0x05)
        self.assertEqual(ack_event["ack_status"], 0)
        self.assertEqual(ack_event["ack_seq"], 0x09)

        status_body = b"\x11\x22\x33\x44\x20\x03\xF6\x04"
        status_payload = bytes.fromhex("AABBCC11223300") + status_body + b"\x00\x00\x00"
        status_event = codec.parse_reply_event(0x03, status_payload, timestamp=2.0, host_rssi=-45, host_snr=5)

        self.assertEqual(status_event["reply"], "STATUS_REPLY")
        self.assertEqual(status_event["flags"], 0x11)
        self.assertEqual(status_event["configByte"], 0x22)
        self.assertEqual(status_event["effectId"], 0x33)
        self.assertEqual(status_event["brightness"], 0x44)

    def test_protocol_codec_parses_identify_reply_using_generated_size(self):
        identify_body = b"\x04\x21\x09" + bytes.fromhex("AABBCCDDEEFF")
        self.assertEqual(len(identify_body), RLPA.SZ_P_IdentifyReply)
        identify_payload = bytes.fromhex("AABBCC11223300") + identify_body + b"\x00\x00\x00"
        identify_event = codec.parse_reply_event(0x01, identify_payload, timestamp=3.0, host_rssi=-42, host_snr=6)

        self.assertEqual(identify_event["reply"], "IDENTIFY_REPLY")
        self.assertEqual(identify_event["version"], 0x04)
        self.assertEqual(identify_event["caps"], 0x21)
        self.assertEqual(identify_event["groupId"], 0x09)
        self.assertEqual(identify_event["mac6"], bytes.fromhex("AABBCCDDEEFF"))


class ControlAdvBuilderTests(unittest.TestCase):
    def test_body_with_no_fields_is_just_header(self):
        body = build_control_body(group_id=0x05, flags=0x02)
        self.assertEqual(body, b"\x05\x02\x00")
        self.assertEqual(len(body), 3)

    def test_full_body_fits_in_body_max_and_round_trips(self):
        body = build_control_body(
            group_id=0x07,
            flags=0x05,
            brightness=200,
            mode=42,
            speed=180,
            intensity=64,
            custom1=10,
            custom2=20,
            custom3=31,
            check1=True,
            check2=False,
            check3=True,
            palette=7,
            color1=(255, 0, 0),
            color2=(0, 255, 0),
            color3=(0, 0, 255),
        )
        self.assertLessEqual(len(body), RLPA.BODY_MAX)
        self.assertEqual(len(body), 21)
        parsed = _parse_control_adv(body)
        self.assertEqual(parsed["groupId"], 0x07)
        self.assertEqual(parsed["flags"], 0x05)
        self.assertEqual(parsed["brightness"], 200)
        self.assertEqual(parsed["mode"], 42)
        self.assertEqual(parsed["speed"], 180)
        self.assertEqual(parsed["intensity"], 64)
        self.assertEqual(parsed["custom1"], 10)
        self.assertEqual(parsed["custom2"], 20)
        self.assertEqual(parsed["custom3"], 31)
        self.assertTrue(parsed["check1"])
        self.assertFalse(parsed["check2"])
        self.assertTrue(parsed["check3"])
        self.assertEqual(parsed["palette"], 7)
        self.assertEqual(parsed["color1"], (255, 0, 0))
        self.assertEqual(parsed["color2"], (0, 255, 0))
        self.assertEqual(parsed["color3"], (0, 0, 255))

    def test_only_mode_change_is_minimal(self):
        body = build_control_body(group_id=0, flags=0x01, mode=5)
        # 3 (header) + 1 (mode)
        self.assertEqual(len(body), 4)
        parsed = _parse_control_adv(body)
        self.assertEqual(parsed["fieldMask"], RL_CTRL_F_MODE)
        self.assertEqual(parsed["mode"], 5)

    def test_only_color_sets_ext_flag(self):
        body = build_control_body(group_id=0, flags=0, color1=(0x12, 0x34, 0x56))
        parsed = _parse_control_adv(body)
        # fieldMask only has the EXT bit; no main-mask singles.
        self.assertEqual(parsed["fieldMask"], RL_CTRL_F_EXT)
        self.assertEqual(parsed["extMask"], RL_CTRL_E_COLOR1)
        self.assertEqual(parsed["color1"], (0x12, 0x34, 0x56))

    def test_checks_only_sets_custom3_checks_byte(self):
        body = build_control_body(group_id=0, flags=0, check2=True)
        parsed = _parse_control_adv(body)
        self.assertEqual(parsed["fieldMask"], RL_CTRL_F_CUSTOM3_CHECKS)
        self.assertEqual(parsed["custom3"], 0)
        self.assertFalse(parsed["check1"])
        self.assertTrue(parsed["check2"])
        self.assertFalse(parsed["check3"])

    def test_opcode_control_adv_is_registered_and_variable_length(self):
        self.assertEqual(RLPA.OPC_CONTROL, 8)
        rule = RLPA.find_rule(RLPA.OPC_CONTROL)
        self.assertIsNotNone(rule)
        self.assertEqual(rule.req_len, 0)  # variable length
        self.assertEqual(rule.policy, RLPA.RESP_NONE)
        self.assertEqual(RLPA.BODY_MAX, 22)


class WledControlAdvancedServiceTests(unittest.TestCase):
    def test_service_calls_transport_with_kwargs(self):
        # Local import to avoid top-level dependency on controller module in this test file.
        from racelink.services.control_service import ControlService

        calls = []

        class _FakeTransport:
            def send_control(self, **kwargs):
                calls.append(kwargs)

        class _FakeController:
            def __init__(self):
                self.transport = _FakeTransport()
                self.device_repository = type("Repo", (), {"list": staticmethod(lambda: [])})()

        class _Dev:
            addr = "AABBCCDDEEFF"
            groupId = 7

        ctrl = _FakeController()
        svc = ControlService(ctrl, None)
        ok = svc.send_wled_control(
            targetDevice=_Dev(),
            params={
                "mode": 5,
                "speed": 200,
                "brightness": 180,
                "color1": (255, 0, 0),
                "check1": True,
            },
        )
        self.assertTrue(ok)
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call["group_id"], 7)
        self.assertEqual(call["recv3"], b"\xDD\xEE\xFF")
        # Flags: POWER_ON (bri>0) | HAS_BRI
        self.assertEqual(call["flags"] & 0x01, 0x01)
        self.assertEqual(call["flags"] & 0x04, 0x04)
        self.assertEqual(call["mode"], 5)
        self.assertEqual(call["speed"], 200)
        self.assertEqual(call["brightness"], 180)
        self.assertEqual(call["color1"], (255, 0, 0))
        self.assertTrue(call["check1"])


if __name__ == "__main__":
    unittest.main()
