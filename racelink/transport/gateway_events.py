"""Low-level transport constants and event definitions for the RaceLink gateway."""

from __future__ import annotations

try:
    from .. import racelink_proto_auto as RLPA

    _HAVE_AUTO = True
except Exception as exc:
    raise ImportError("RaceLink protocol mirror missing: expected racelink.racelink_proto_auto") from exc


class LP:
    DIR_M2N = 0x00
    DIR_N2M = 0x80

    OPC_DEVICES = 0x01
    OPC_SET_GROUP = 0x02
    OPC_STATUS = 0x03
    OPC_CONTROL = 0x04
    OPC_CONFIG = 0x05
    OPC_SYNC = 0x06
    OPC_STREAM = 0x07
    OPC_ACK = 0x7E

    @staticmethod
    def make_type(direction: int, opcode7: int) -> int:
        return direction | (opcode7 & 0x7F)


if _HAVE_AUTO:
    try:
        LP.DIR_M2N = getattr(RLPA, "DIR_M2N", LP.DIR_M2N)
        LP.DIR_N2M = getattr(RLPA, "DIR_N2M", LP.DIR_N2M)
        LP.OPC_DEVICES = getattr(RLPA, "OPC_DEVICES", LP.OPC_DEVICES)
        LP.OPC_SET_GROUP = getattr(RLPA, "OPC_SET_GROUP", LP.OPC_SET_GROUP)
        LP.OPC_STATUS = getattr(RLPA, "OPC_STATUS", LP.OPC_STATUS)
        LP.OPC_CONTROL = getattr(RLPA, "OPC_CONTROL", getattr(RLPA, "OPC_WLED_CONTROL", LP.OPC_CONTROL))
        LP.OPC_CONFIG = getattr(RLPA, "OPC_CONFIG", LP.OPC_CONFIG)
        LP.OPC_SYNC = getattr(RLPA, "OPC_SYNC", LP.OPC_SYNC)
        LP.OPC_STREAM = getattr(RLPA, "OPC_STREAM", LP.OPC_STREAM)
        LP.OPC_ACK = getattr(RLPA, "OPC_ACK", LP.OPC_ACK)

        make_type = getattr(RLPA, "make_type", LP.make_type)

        def _make_type(direction, opcode):
            return make_type(direction, opcode)

        LP.make_type = staticmethod(_make_type)
    except Exception:
        pass


EV_ERROR = 0xF0
EV_RX_WINDOW_OPEN = 0xF1
EV_RX_WINDOW_CLOSED = 0xF2
EV_TX_DONE = 0xF3
