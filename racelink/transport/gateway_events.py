"""Low-level transport constants and event definitions for the LoRa gateway."""

from __future__ import annotations

try:
    import lora_proto_auto as LPA

    _HAVE_AUTO = True
except Exception:
    _HAVE_AUTO = False


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
        LP.DIR_M2N = getattr(LPA, "DIR_M2N", LP.DIR_M2N)
        LP.DIR_N2M = getattr(LPA, "DIR_N2M", LP.DIR_N2M)
        LP.OPC_DEVICES = getattr(LPA, "OPC_DEVICES", LP.OPC_DEVICES)
        LP.OPC_SET_GROUP = getattr(LPA, "OPC_SET_GROUP", LP.OPC_SET_GROUP)
        LP.OPC_STATUS = getattr(LPA, "OPC_STATUS", LP.OPC_STATUS)
        LP.OPC_CONTROL = getattr(LPA, "OPC_CONTROL", getattr(LPA, "OPC_WLED_CONTROL", LP.OPC_CONTROL))
        LP.OPC_CONFIG = getattr(LPA, "OPC_CONFIG", LP.OPC_CONFIG)
        LP.OPC_SYNC = getattr(LPA, "OPC_SYNC", LP.OPC_SYNC)
        LP.OPC_STREAM = getattr(LPA, "OPC_STREAM", LP.OPC_STREAM)
        LP.OPC_ACK = getattr(LPA, "OPC_ACK", LP.OPC_ACK)

        make_type = getattr(LPA, "make_type", LP.make_type)

        def _make_type(direction, opcode):
            return make_type(direction, opcode)

        LP.make_type = staticmethod(_make_type)
    except Exception:
        pass


EV_ERROR = 0xF0
EV_RX_WINDOW_OPEN = 0xF1
EV_RX_WINDOW_CLOSED = 0xF2
EV_TX_DONE = 0xF3
