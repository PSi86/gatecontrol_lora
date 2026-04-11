"""Legacy compatibility shim for imports that have moved to ``racelink.domain``."""

try:
    from .racelink.state import get_runtime_state_repository
    from .racelink.domain import (
        RL_DEV_TYPE_CAPS,
        RL_DEV_TYPE_INFO,
        RL_Device,
        RL_DeviceGroup,
        RL_Dev_Type,
        RL_FLAG_ARM_ON_SYNC,
        RL_FLAG_FORCE_REAPPLY,
        RL_FLAG_FORCE_TT0,
        RL_FLAG_HAS_BRI,
        RL_FLAG_POWER_ON,
        RL_SPECIALS,
        build_specials_state,
        create_device,
        effect_select_options,
        get_dev_type_info,
        get_special_keys_for_caps,
        get_specials_config,
        is_wled_dev_type,
    )
except ImportError:  # pragma: no cover
    from racelink.state import get_runtime_state_repository
    from racelink.domain import (
        RL_DEV_TYPE_CAPS,
        RL_DEV_TYPE_INFO,
        RL_Device,
        RL_DeviceGroup,
        RL_Dev_Type,
        RL_FLAG_ARM_ON_SYNC,
        RL_FLAG_FORCE_REAPPLY,
        RL_FLAG_FORCE_TT0,
        RL_FLAG_HAS_BRI,
        RL_FLAG_POWER_ON,
        RL_SPECIALS,
        build_specials_state,
        create_device,
        effect_select_options,
        get_dev_type_info,
        get_special_keys_for_caps,
        get_specials_config,
        is_wled_dev_type,
    )

_state_repository = get_runtime_state_repository()
rl_backup_devicelist = _state_repository.backup_devices.list()
rl_backup_grouplist = _state_repository.backup_groups.list()
rl_devicelist = _state_repository.devices.list()
rl_grouplist = _state_repository.groups.list()

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
    "rl_backup_devicelist",
    "rl_backup_grouplist",
    "rl_devicelist",
    "rl_grouplist",
]
