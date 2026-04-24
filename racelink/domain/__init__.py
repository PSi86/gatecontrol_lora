"""Domain models and metadata for RaceLink."""

from .capabilities import build_specials_state, get_special_keys_for_caps
from .device_types import (
    RL_DEV_TYPE_CAPS,
    RL_DEV_TYPE_INFO,
    RL_Dev_Type,
    RL_FLAG_ARM_ON_SYNC,
    RL_FLAG_FORCE_REAPPLY,
    RL_FLAG_FORCE_TT0,
    RL_FLAG_HAS_BRI,
    RL_FLAG_POWER_ON,
    get_dev_type_info,
    is_wled_dev_type,
)
from .models import RL_Device, RL_DeviceGroup
from .specials import (
    RL_SPECIALS,
    create_device,
    effect_select_options,
    get_specials_config,
)
from . import state_scope
from .state_scope import normalize_scopes, sse_what_from_scopes

__all__ = [
    "RL_Device",
    "RL_DeviceGroup",
    "RL_Dev_Type",
    "RL_DEV_TYPE_CAPS",
    "RL_DEV_TYPE_INFO",
    "RL_FLAG_POWER_ON",
    "RL_FLAG_ARM_ON_SYNC",
    "RL_FLAG_HAS_BRI",
    "RL_FLAG_FORCE_TT0",
    "RL_FLAG_FORCE_REAPPLY",
    "RL_SPECIALS",
    "build_specials_state",
    "create_device",
    "effect_select_options",
    "get_dev_type_info",
    "get_special_keys_for_caps",
    "get_specials_config",
    "is_wled_dev_type",
    "normalize_scopes",
    "sse_what_from_scopes",
    "state_scope",
    "rl_backup_devicelist",
    "rl_backup_grouplist",
    "rl_devicelist",
    "rl_grouplist",
]


def __getattr__(name):
    if name in {"rl_backup_devicelist", "rl_backup_grouplist", "rl_devicelist", "rl_grouplist"}:
        from ..state import get_runtime_state_repository

        state_repository = get_runtime_state_repository()
        mapping = {
            "rl_backup_devicelist": state_repository.backup_devices.list(),
            "rl_backup_grouplist": state_repository.backup_groups.list(),
            "rl_devicelist": state_repository.devices.list(),
            "rl_grouplist": state_repository.groups.list(),
        }
        return mapping[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
