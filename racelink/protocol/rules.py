"""Protocol rule access built on top of the generated header mirror."""

from __future__ import annotations

try:
    from .. import racelink_proto_auto as RLPA
except Exception as exc:  # pragma: no cover
    raise ImportError("RaceLink protocol mirror missing: expected racelink.racelink_proto_auto") from exc


PacketRule = RLPA.PacketRule

DIR_M2N = RLPA.DIR_M2N
DIR_N2M = RLPA.DIR_N2M

RESP_NONE = RLPA.RESP_NONE
RESP_ACK = RLPA.RESP_ACK
RESP_SPECIFIC = RLPA.RESP_SPECIFIC

RULES = RLPA.RULES


def find_rule(opcode7: int):
    return RLPA.find_rule(opcode7)


def opcode_name(opcode7: int) -> str:
    rule = find_rule(opcode7)
    if rule and getattr(rule, "name", None):
        return str(rule.name)
    return f"0x{int(opcode7) & 0x7F:02X}"


def response_policy(opcode7: int) -> int:
    rule = find_rule(opcode7)
    if not rule:
        return RESP_NONE
    return int(getattr(rule, "policy", RESP_NONE))


def response_opcode(opcode7: int) -> int:
    rule = find_rule(opcode7)
    if not rule:
        return -1
    return int(getattr(rule, "rsp_opcode7", -1)) & 0x7F


def request_direction(opcode7: int) -> int:
    rule = find_rule(opcode7)
    if not rule:
        return DIR_M2N
    return int(getattr(rule, "req_dir", DIR_M2N))
