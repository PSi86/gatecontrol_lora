"""Created by Peter Simandl "PSi86" in 2026.
Works with Rotorhazard 4.0.
"""

from .bootstrap import build_plugin
from .data import (
    RL_Device,
    RL_DeviceGroup,
    RL_Dev_Type,
    RL_FLAG_POWER_ON,
    RL_FLAG_ARM_ON_SYNC,
    RL_FLAG_HAS_BRI,
    RL_FLAG_FORCE_TT0,
    RL_FLAG_FORCE_REAPPLY,
)

rl_instance = None


def initialize(rhapi):
    global rl_instance
    plugin = build_plugin(rhapi)
    rl_instance = plugin.controller
    return plugin


__all__ = [
    "RL_Device",
    "RL_DeviceGroup",
    "RL_Dev_Type",
    "RL_FLAG_POWER_ON",
    "RL_FLAG_ARM_ON_SYNC",
    "RL_FLAG_HAS_BRI",
    "RL_FLAG_FORCE_TT0",
    "RL_FLAG_FORCE_REAPPLY",
    "initialize",
]
