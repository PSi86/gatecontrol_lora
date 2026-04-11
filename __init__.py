"""Created by Peter Simandl "PSi86" in 2026.
Works with Rotorhazard 4.0.
"""

from .racelink.integrations.rotorhazard import plugin as _rh_plugin
from .controller import RaceLink_LoRa
from .data import (
    RL_Device,
    RL_DeviceGroup,
    RL_Dev_Type,
    RL_FLAG_POWER_ON,
    RL_FLAG_ARM_ON_SYNC,
    RL_FLAG_HAS_BRI,
    RL_FLAG_FORCE_TT0,
    RL_FLAG_FORCE_REAPPLY,
    rl_backup_devicelist,
    rl_backup_grouplist,
    rl_devicelist,
    rl_grouplist,
)

initialize = _rh_plugin.initialize


def __getattr__(name):
    if name == "rl_instance":
        return _rh_plugin.rl_instance
    if name == "rl_app":
        return _rh_plugin.rl_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "RL_Device",
    "RL_DeviceGroup",
    "RL_Dev_Type",
    "RL_FLAG_POWER_ON",
    "RL_FLAG_ARM_ON_SYNC",
    "RL_FLAG_HAS_BRI",
    "RL_FLAG_FORCE_TT0",
    "RL_FLAG_FORCE_REAPPLY",
    "RaceLink_LoRa",
    "rl_backup_devicelist",
    "rl_backup_grouplist",
    "rl_devicelist",
    "rl_grouplist",
    "initialize",
]
