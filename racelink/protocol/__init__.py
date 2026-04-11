"""Protocol-facing modules for the RaceLink LoRa packet layer."""

from .addressing import last3_hex, to_hex_str
from .codec import parse_reply_event
from .packets import (
    build_config_body,
    build_control_body,
    build_get_devices_body,
    build_set_group_body,
    build_stream_body,
    build_sync_body,
)
from .rules import (
    DIR_M2N,
    DIR_N2M,
    RESP_ACK,
    RESP_NONE,
    RESP_SPECIFIC,
    PacketRule,
    RULES,
    find_rule,
    opcode_name,
    request_direction,
    response_opcode,
    response_policy,
)

__all__ = [
    "DIR_M2N",
    "DIR_N2M",
    "PacketRule",
    "RESP_ACK",
    "RESP_NONE",
    "RESP_SPECIFIC",
    "RULES",
    "build_config_body",
    "build_control_body",
    "build_get_devices_body",
    "build_set_group_body",
    "build_stream_body",
    "build_sync_body",
    "find_rule",
    "last3_hex",
    "opcode_name",
    "parse_reply_event",
    "request_direction",
    "response_opcode",
    "response_policy",
    "to_hex_str",
]
