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

    # legacy RH callback names
    def register_settings(self):
        self.register_settings_ui()

    def registerActions(self, args=None):
        self.register_actions(args=args)

    def createUiDevList(self):
        return create_ui_device_list(self)

    def rl_createUiDevList(self, dev_types=None, capabilities=None, outputDevices=True, outputGroups=True):
        return create_filtered_ui_device_list(
            self,
            dev_types=dev_types,
            capabilities=capabilities,
            output_devices=outputDevices,
            output_groups=outputGroups,
        )

    def createUiGroupList(self, exclude_static=False):
        return create_ui_group_list(self, exclude_static=exclude_static)

    def _make_special_action_handler(self, fn_key: str, mode: str):
        return _make_special_action_handler(self, fn_key, mode)

    def specialAction(self, action, fn_key: str, mode: str):
        return special_action(self, action, fn_key, mode)

    def register_rl_dataimporter(self, args):
        return register_rl_dataimporter(self, args)

    def register_rl_dataexporter(self, args):
        return register_rl_dataexporter(self, args)

    def nodeSwitch(self, action, args=None):
        if "rl_action_device" not in action and "manual" not in action:
            return

        target_addr = action.get("rl_action_device") if "rl_action_device" in action else self._rhapi.db.option("rl_quickset_device", None)
        target_device = self.controller.getDeviceFromAddress(target_addr)
        if target_device is None:
            logger.warning("nodeSwitch: device not found: %r", target_addr)
            return

        brightness = action.get("rl_action_brightness") if "rl_action_device" in action else self._rhapi.db.option("rl_quickset_brightness", None)
        preset_id = action.get("rl_action_effect") if "rl_action_device" in action else self._rhapi.db.option("rl_quickset_effect", None)
        self.controller.app.apply_device_switch(target_device=target_device, brightness=int(brightness), preset_id=int(preset_id))

    def groupSwitch(self, action, args=None):
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

    def discoveryAction(self, args):
        group_selected = int(self._rhapi.db.option("rl_assignToGroup", None))
        new_group_str = self._rhapi.db.option("rl_assignToNewGroup", None)

        if group_selected == 0:
            if not new_group_str:
                new_group_str = "New Group"
            new_group_str += " " + datetime.now().strftime("%Y%m%d_%H%M%S")
            group_selected = self.controller.get_group_count()

        num_found = self.controller.getDevices(groupFilter=0, addToGroup=group_selected)

        if num_found > 0 and group_selected == self.controller.get_group_count():
            self.controller.add_group(RL_DeviceGroup(new_group_str))
            self.controller.uiGroupList = self.createUiGroupList()
            self.controller.uiDiscoveryGroupList = self.createUiGroupList(True)
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

    def __getattr__(self, item):
        app = getattr(self.controller, "app", None)
        if app and hasattr(app, item):
            return getattr(app, item)
        if item in self.controller.__dict__:
            return self.controller.__dict__[item]
        raise AttributeError(item)
