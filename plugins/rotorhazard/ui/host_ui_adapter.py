from __future__ import annotations

import logging
from datetime import datetime

from RHUI import UIFieldSelectOption

from ....core.ports.host_ui import HostUIPort
from ....data import RL_DeviceGroup, get_specials_config
from .actions_registry import _make_special_action_handler, register_actions, special_action
from .config_io import register_rl_dataexporter, register_rl_dataimporter, rl_config_json_output, rl_import_json, rl_write_json
from .quickset_panel import register_quickset_ui
from .settings_panel import create_filtered_ui_device_list, create_ui_device_list, create_ui_group_list, register_settings

logger = logging.getLogger(__name__)


class RotorHazardHostUIAdapter(HostUIPort):
    def __init__(self, controller):
        self.controller = controller
        self._rhapi = controller._rhapi

    def _get_select_options(self, fn_key: str, var_key: str):
        context = {"rhapi": self._rhapi, "gc": self.controller}
        specials = get_specials_config(context=context)
        for cap_info in specials.values():
            for fn_info in cap_info.get("functions", []) or []:
                if fn_info.get("key") != fn_key:
                    continue
                ui_meta = (fn_info.get("ui") or {}).get(var_key, {})
                generator = ui_meta.get("generator")
                if callable(generator):
                    raw = generator(context=context)
                    return [UIFieldSelectOption(opt["value"], opt["label"]) for opt in raw]
        return []

    def register_settings_ui(self) -> None:
        register_settings(self)

    def register_quickset_ui(self) -> None:
        register_quickset_ui(self)

    def register_actions(self, args=None) -> None:
        register_actions(self, args=args)

    def create_ui_device_list(self):
        return create_ui_device_list(self)

    def create_filtered_ui_device_list(self, dev_types=None, capabilities=None, output_devices=True, output_groups=True):
        return create_filtered_ui_device_list(
            self,
            dev_types=dev_types,
            capabilities=capabilities,
            output_devices=output_devices,
            output_groups=output_groups,
        )

    def create_ui_group_list(self, exclude_static=False):
        return create_ui_group_list(self, exclude_static=exclude_static)

    def _make_special_action_handler(self, fn_key: str, mode: str):
        return _make_special_action_handler(self, fn_key, mode)

    def specialAction(self, action, fn_key: str, mode: str):
        return special_action(self, action, fn_key, mode)

    def register_rl_dataimporter(self, args):
        return register_rl_dataimporter(self, args)

    def register_rl_dataexporter(self, args):
        return register_rl_dataexporter(self, args)

    def node_switch_action(self, action, args=None):
        if "rl_action_device" not in action and "manual" not in action:
            return

        target_addr = action.get("rl_action_device") if "rl_action_device" in action else self._rhapi.db.option("rl_quickset_device", None)
        target_device = self.controller.get_device_from_address(target_addr)
        if target_device is None:
            logger.warning("node_switch_action: device not found: %r", target_addr)
            return

        brightness = action.get("rl_action_brightness") if "rl_action_device" in action else self._rhapi.db.option("rl_quickset_brightness", None)
        preset_id = action.get("rl_action_effect") if "rl_action_device" in action else self._rhapi.db.option("rl_quickset_effect", None)
        self.controller.app.apply_device_switch(target_device=target_device, brightness=int(brightness), preset_id=int(preset_id))

    def group_switch_action(self, action, args=None):
        if "rl_action_group" in action:
            self.controller.app.apply_group_switch(
                group_id=int(action["rl_action_group"]),
                brightness=int(action["rl_action_brightness"]),
                preset_id=int(action["rl_action_effect"]),
            )
            return

        if "manual" in action:
            self.controller.app.apply_group_switch(
                group_id=int(self._rhapi.db.option("rl_quickset_group", None)),
                brightness=int(self._rhapi.db.option("rl_quickset_brightness", None)),
                preset_id=int(self._rhapi.db.option("rl_quickset_effect", None)),
            )

    def discover_devices_action(self, args):
        group_selected = int(self._rhapi.db.option("rl_assignToGroup", None))
        new_group_str = self._rhapi.db.option("rl_assignToNewGroup", None)

        if group_selected == 0:
            if not new_group_str:
                new_group_str = "New Group"
            new_group_str += " " + datetime.now().strftime("%Y%m%d_%H%M%S")
            group_selected = self.controller.get_group_count()

        num_found = self.controller.get_devices(group_filter=0, add_to_group=group_selected)

        if num_found > 0 and group_selected == self.controller.get_group_count():
            self.controller.add_group(RL_DeviceGroup(new_group_str))
            self.controller.uiGroupList = self.create_ui_group_list()
            self.controller.uiDiscoveryGroupList = self.create_ui_group_list(True)
            self.register_settings_ui()
            self.register_quickset_ui()
            self.register_actions()
            self._rhapi.ui.broadcast_ui("settings")
            self._rhapi.ui.broadcast_ui("run")

    def rl_write_json(self, data):
        return rl_write_json(self, data)

    def rl_config_json_output(self, rhapi=None):
        return rl_config_json_output(self, rhapi=rhapi)

    def rl_import_json(self, importer_class, rhapi, source, args):
        return rl_import_json(self, importer_class, rhapi, source, args)

    @property
    def uiGroupList(self):
        return self.controller.uiGroupList

    @property
    def uiDiscoveryGroupList(self):
        return self.controller.uiDiscoveryGroupList

    def list_devices(self):
        return self.controller.list_devices()

    def list_group_objects(self):
        return self.controller.list_group_objects()

    def query_devices(self, *, dev_types=None, capabilities=None):
        return self.controller.query_devices(dev_types=dev_types, capabilities=capabilities)

    def query_groups(self, *, exclude_static=False):
        return self.controller.query_groups(exclude_static=exclude_static)

    def query_groups_for_devices(self, devices, cap_set=None):
        return self.controller.query_groups_for_devices(devices, cap_set)

    def get_group_count(self):
        return self.controller.get_group_count()

    def add_group(self, group):
        self.controller.add_group(group)

    def get_device_from_address(self, addr):
        return self.controller.get_device_from_address(addr)

    def save_to_db(self, args=None):
        self.controller.save_to_db(args)

    def force_groups(self, args=None, sanity_check=True):
        self.controller.force_groups(args=args, sanity_check=sanity_check)

    def get_devices(self, group_filter=255, target_device=None, add_to_group=-1):
        return self.controller.get_devices(group_filter=group_filter, target_device=target_device, add_to_group=add_to_group)

    def discover_port(self, args=None):
        self.controller.discover_port(args or {})

    def send_wled_control(self, *, target_device=None, target_group=None, params=None):
        return self.controller.send_wled_control(target_device=target_device, target_group=target_group, params=params)

    def send_startblock_config(self, *, target_device=None, target_group=None, params=None):
        return self.controller.send_startblock_config(target_device=target_device, target_group=target_group, params=params)

    def send_startblock_control(self, *, target_device=None, target_group=None, params=None):
        return self.controller.send_startblock_control(target_device=target_device, target_group=target_group, params=params)

    # Legacy wrappers (thin, deprecate after RH migration).
    def register_settings(self):
        logger.warning("Legacy call register_settings() -> register_settings_ui()")
        self.register_settings_ui()

    def registerActions(self, args=None):
        logger.warning("Legacy call registerActions() -> register_actions()")
        self.register_actions(args=args)

    def createUiDevList(self):
        logger.warning("Legacy call createUiDevList() -> create_ui_device_list()")
        return self.create_ui_device_list()

    def rl_createUiDevList(self, dev_types=None, capabilities=None, outputDevices=True, outputGroups=True):
        logger.warning("Legacy call rl_createUiDevList() -> create_filtered_ui_device_list()")
        return self.create_filtered_ui_device_list(
            dev_types=dev_types,
            capabilities=capabilities,
            output_devices=outputDevices,
            output_groups=outputGroups,
        )

    def createUiGroupList(self, exclude_static=False):
        logger.warning("Legacy call createUiGroupList() -> create_ui_group_list()")
        return self.create_ui_group_list(exclude_static=exclude_static)

    def nodeSwitch(self, action, args=None):
        logger.warning("Legacy call nodeSwitch() -> node_switch_action()")
        return self.node_switch_action(action, args=args)

    def groupSwitch(self, action, args=None):
        logger.warning("Legacy call groupSwitch() -> group_switch_action()")
        return self.group_switch_action(action, args=args)

    def discoveryAction(self, args):
        logger.warning("Legacy call discoveryAction() -> discover_devices_action()")
        return self.discover_devices_action(args)
