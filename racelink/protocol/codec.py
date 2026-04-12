"""Protocol codec helpers for typed transport reply events."""

from __future__ import annotations

import struct

from . import rules


def parse_reply_event(type_byte: int, data: bytes, *, timestamp: float, host_rssi: int, host_snr: int, rx_windows: int) -> dict:
    hdr = data[:7]
    body = data[7:-3]
    sender3 = bytes(hdr[0:3])
    receiver3 = bytes(hdr[3:6])
    opc = type_byte & 0x7F

    ev = {
        "type": type_byte,
        "dir": type_byte & 0x80,
        "opc": opc,
        "sender3": sender3,
        "receiver3": receiver3,
        "host_rssi": host_rssi,
        "host_snr": host_snr,
        "ts": timestamp,
        "rx_windows": rx_windows,
    }

    if opc == 0x01:
        if len(body) == 9:
            ev.update({"reply": "IDENTIFY_REPLY", "version": body[0], "caps": body[1], "groupId": body[2], "mac6": bytes(body[3:9])})
        else:
            ev.update({"reply": "IDENTIFY_REPLY", "body_raw": body})
    elif opc == 0x03:
        if len(body) == 8:
            flags, config_byte, preset_id, brightness, vbat_mV, rssi_node, snr_node = struct.unpack("<BBBBHbb", body)
            ev.update(
                {
                    "reply": "STATUS_REPLY",
                    "flags": flags,
                    "configByte": config_byte,
                    "presetId": preset_id,
                    "brightness": brightness,
                    "vbat_mV": vbat_mV,
                    "node_rssi": rssi_node,
                    "node_snr": snr_node,
                }
            )
        elif len(body) == 7:
            flags, preset_id, brightness, vbat_mV, rssi_node, snr_node = struct.unpack("<BBBHbb", body)
            ev.update(
                {
                    "reply": "STATUS_REPLY",
                    "flags": flags,
                    "configByte": 0,
                    "presetId": preset_id,
                    "brightness": brightness,
                    "vbat_mV": vbat_mV,
                    "node_rssi": rssi_node,
                    "node_snr": snr_node,
                }
            )
        else:
            ev.update({"reply": "STATUS_REPLY", "body_raw": body})
    elif opc == 0x7E:
        if len(body) >= 2:
            ack_of = body[0] & 0x7F
            ack_status = body[1]
            ev.update({"reply": "ACK", "ack_of": ack_of, "ack_status": ack_status})
            if len(body) >= 3:
                ev.update({"ack_seq": body[2]})
        else:
            ev.update({"reply": "ACK", "body_raw": body})
    else:
        ev.update({"reply": rules.opcode_name(opc), "body_raw": body})

    return ev
