from __future__ import annotations

import logging

from RHUI import UIField, UIFieldType, UIFieldSelectOption

logger = logging.getLogger(__name__)


def register_settings(gc):
    logger.debug("RL: Registering Settings UI Elements")
    temp_ui_group_list = [UIFieldSelectOption(0, "New Group")]
    temp_ui_group_list += gc.uiDiscoveryGroupList

    gc._rhapi.ui.register_panel("rl_settings", "RaceLink Plugin", "settings")
    gc._rhapi.fields.register_option(
        UIField("rl_device_config", "Device Config", UIFieldType.TEXT, private=False),
        "rl_settings",
    )
    gc._rhapi.fields.register_option(
        UIField("rl_groups_config", "Groups Config", UIFieldType.TEXT, private=False),
        "rl_settings",
    )
    gc._rhapi.fields.register_option(
        UIField(
            "rl_assignToGroup",
            "Add discovered Devices to Group",
            UIFieldType.SELECT,
            options=temp_ui_group_list,
            value=temp_ui_group_list[0].value,
        ),
        "rl_settings",
    )
    gc._rhapi.fields.register_option(
        UIField("rl_assignToNewGroup", "New Group Name", UIFieldType.TEXT, private=False),
        "rl_settings",
    )
    gc._rhapi.ui.register_quickbutton(
        "rl_settings",
        "rl_btn_set_defaults",
        "Save Configuration",
        gc.save_to_db,
        args={"manual": True},
    )
    gc._rhapi.ui.register_quickbutton(
        "rl_settings",
        "rl_btn_force_groups",
        "Set all Groups",
        gc.forceGroups,
        args={"manual": True},
    )
    gc._rhapi.ui.register_quickbutton(
        "rl_settings",
        "rl_btn_get_devices",
        "Discover Devices",
        gc.discoveryAction,
        args={"manual": True},
    )
    gc._rhapi.ui.register_quickbutton(
        "rl_settings",
        "rl_run_autodetect",
        "Detect USB Communicator",
        gc.discoverPort,
        args={"manual": True},
    )


def create_ui_device_list(gc):
    logger.debug("RL: Creating UI Device Select Options")
    return [UIFieldSelectOption(device.addr, device.name) for device in gc.list_devices()]


def create_filtered_ui_device_list(
    gc,
    dev_types: list[int] | None = None,
    capabilities: list[str] | None = None,
    output_devices: bool = True,
    output_groups: bool = True,
):
    logger.debug("RL: Creating filtered UI device/group list")
    dev_types_set = set(int(d) for d in dev_types) if dev_types else None
    cap_set = set(capabilities) if capabilities else None

    selected_devs = gc.query_devices(dev_types=dev_types_set, capabilities=cap_set)
    output = {"devices": [], "groups": []}

    if output_devices:
        output["devices"] = [UIFieldSelectOption(dev.addr, dev.name) for dev in selected_devs]

    if output_groups:
        output["groups"] = [
            UIFieldSelectOption(value, name)
            for value, name in gc.query_groups_for_devices(selected_devs, cap_set)
        ]

    return output


def create_ui_group_list(gc, exclude_static=False):
    logger.debug("RL: Creating UI Group Select Options")
    groups = gc.query_groups(exclude_static=exclude_static)
    return [UIFieldSelectOption(value, group.name) for value, group in groups]
