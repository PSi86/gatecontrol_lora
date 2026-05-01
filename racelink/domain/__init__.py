"""Domain models and metadata for RaceLink."""

from .capabilities import build_specials_state, get_special_keys_for_caps
from .device_types import (
    RL_DEV_TYPE_CAPS,
    RL_DEV_TYPE_INFO,
    RL_Dev_Type,
    get_dev_type_info,
    is_wled_dev_type,
)
from .flags import (
    FLAG_BITS,
    RL_FLAG_ARM_ON_SYNC,
    RL_FLAG_FORCE_REAPPLY,
    RL_FLAG_FORCE_TT0,
    RL_FLAG_HAS_BRI,
    RL_FLAG_OFFSET_MODE,
    RL_FLAG_POWER_ON,
    USER_FLAG_KEYS,
    build_flags_byte,
    flags_from_mapping,
)
from .models import RL_Device, RL_DeviceGroup
from .specials import (
    RL_PRESET_EDITOR_SCHEMA,
    RL_SPECIALS,
    create_device,
    get_specials_config,
    rl_preset_select_options,
    serialize_rl_preset_editor_schema,
    wled_preset_select_options,
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
    "RL_FLAG_OFFSET_MODE",
    "FLAG_BITS",
    "USER_FLAG_KEYS",
    "build_flags_byte",
    "flags_from_mapping",
    "RL_PRESET_EDITOR_SCHEMA",
    "RL_SPECIALS",
    "build_specials_state",
    "create_device",
    "get_dev_type_info",
    "get_special_keys_for_caps",
    "get_specials_config",
    "is_wled_dev_type",
    "normalize_scopes",
    "rl_preset_select_options",
    "serialize_rl_preset_editor_schema",
    "sse_what_from_scopes",
    "state_scope",
    "rl_backup_devicelist",
    "rl_backup_grouplist",
    "rl_devicelist",
    "rl_grouplist",
    "wled_preset_select_options",
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
