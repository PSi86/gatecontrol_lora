from __future__ import annotations

import logging

from ....data import RL_DeviceGroup
from ..presentation.racelink_webui import register_rl_blueprint

logger = logging.getLogger(__name__)


def activate(plugin) -> None:
    """Expose RaceLink Flask blueprint in RotorHazard host."""
    register_rl_blueprint(
        plugin.rhapi,
        rl_instance=plugin.controller,
        rl_devicelist=plugin.repository.device_items,
        rl_grouplist=plugin.repository.group_items,
        RL_DeviceGroup=RL_DeviceGroup,
        logger=logger,
    )
