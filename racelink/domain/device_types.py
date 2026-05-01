"""Device type constants for RaceLink.

The ``RL_FLAG_*`` constants are re-exported from :mod:`.flags` for
backwards compatibility with existing imports; new code should prefer
``from .flags import ...`` directly.
"""

from .flags import (
    RL_FLAG_ARM_ON_SYNC,
    RL_FLAG_FORCE_REAPPLY,
    RL_FLAG_FORCE_TT0,
    RL_FLAG_HAS_BRI,
    RL_FLAG_OFFSET_MODE,
    RL_FLAG_POWER_ON,
)


class RL_Dev_Type:
    GATEWAY_REV1 = 1
    NODE_WLED_REV1 = 10
    NODE_WLED_REV3 = 11
    NODE_WLED_REV4 = 12
    NODE_WLED_REV5 = 13
    NODE_WLED_STARTBLOCK_REV3 = 50


RL_DEV_TYPE_CAPS = ["STARTBLOCK", "LEDMATRIX", "WLED"]

RL_DEV_TYPE_INFO = {
    RL_Dev_Type.GATEWAY_REV1: {"name": "Gateway_Rev1"},
    RL_Dev_Type.NODE_WLED_REV1: {"name": "WLED_Rev1", "caps": ["WLED"]},
    RL_Dev_Type.NODE_WLED_REV3: {"name": "WLED_Rev3", "caps": ["WLED"]},
    RL_Dev_Type.NODE_WLED_REV4: {"name": "WLED_Rev4", "caps": ["WLED"]},
    RL_Dev_Type.NODE_WLED_REV5: {"name": "WLED_Rev5", "caps": ["WLED"]},
    RL_Dev_Type.NODE_WLED_STARTBLOCK_REV3: {"name": "WLED_Startblock_Rev3", "caps": ["STARTBLOCK", "WLED"]},
}


def get_dev_type_info(type_id: int | None) -> dict:
    tid = int(type_id or 0)
    base = RL_DEV_TYPE_INFO.get(tid, {"name": f"UNKNOWN_{tid}"})
    caps = set(base.get("caps", []))
    info = {"name": base.get("name", f"UNKNOWN_{tid}"), "caps": sorted(caps)}
    for cap in RL_DEV_TYPE_CAPS:
        info[cap] = cap in caps
    return info


def is_wled_dev_type(type_id: int | None) -> bool:
    info = get_dev_type_info(type_id)
    return bool(info.get("WLED"))
