"""Created by Peter Simandl "PSi86" in 2026.
Works with Rotorhazard 4.0.
"""

import logging

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
from .platform.rh_adapter import RotorHazardAdapter

logger = logging.getLogger(__name__)


rl_instance = None
rh_adapter = None


def initialize(rhapi):
    """RotorHazard plugin entrypoint."""

    global rl_instance, rh_adapter

    rh_adapter = RotorHazardAdapter(rhapi)
    rl_instance = rh_adapter.initialize()


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
