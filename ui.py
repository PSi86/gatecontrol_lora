from __future__ import annotations

import logging
from datetime import datetime

from .data import RL_DeviceGroup, get_specials_config
from .presentation.rh.actions_registry import _make_special_action_handler as rh_make_special_action_handler
from .presentation.rh.actions_registry import registerActions as rh_register_actions
from .presentation.rh.actions_registry import specialAction as rh_special_action
from .presentation.rh.config_io import (
    register_rl_dataexporter as rh_register_dataexporter,
    register_rl_dataimporter as rh_register_dataimporter,
    rl_config_json_output as rh_rl_config_json_output,
    rl_import_json as rh_rl_import_json,
    rl_write_json as rh_rl_write_json,
)
from .presentation.rh.quickset_panel import register_quickset_ui as rh_register_quickset_ui
from .presentation.rh.settings_panel import (
    create_filtered_ui_device_list,
    create_ui_device_list,
    create_ui_group_list,
    register_settings as rh_register_settings,
)

logger = logging.getLogger(__name__)


class RaceLinkUIMixin:
    def _get_select_options(self, fn_key: str, var_key: str):
        context = {"rhapi": self._rhapi, "gc": self}
        specials = get_specials_config(context=context)
        for cap_info in specials.values():
            for fn_info in cap_info.get("functions", []) or []:
                if fn_info.get("key") != fn_key:
                    continue
                ui_meta = (fn_info.get("ui") or {}).get(var_key, {})
                generator = ui_meta.get("generator")
                if callable(generator):
                    raw = generator(context=context)
                    return [self.make_ui_select_option(opt["value"], opt["label"]) for opt in raw]
        return []

    def make_ui_select_option(self, value, label):
        from RHUI import UIFieldSelectOption

        return UIFieldSelectOption(value, label)

    def register_settings(self):
        return rh_register_settings(self)

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

    def register_quickset_ui(self):
        return rh_register_quickset_ui(self)

    def registerActions(self, args=None):
        return rh_register_actions(self, args=args)

    def _make_special_action_handler(self, fn_key: str, mode: str):
        return rh_make_special_action_handler(self, fn_key, mode)

    def specialAction(self, action, fn_key: str, mode: str):
        return rh_special_action(self, action, fn_key, mode)

    def register_rl_dataimporter(self, args):
        return rh_register_dataimporter(self, args)

    def register_rl_dataexporter(self, args):
        return rh_register_dataexporter(self, args)

    def registerHandlers(self, args):
        self._rhapi.ui.message_notify("text")

    def nodeSwitch(self, action, args=None):
        if "rl_action_device" in action:
            logger.debug("Action triggered")
            target_device = self.getDeviceFromAddress(action["rl_action_device"])
            if target_device is None:
                logger.warning("nodeSwitch: device not found: %r", action["rl_action_device"])
                return
            self.control_service.apply_device_switch(
                target_device=target_device,
                brightness=int(action["rl_action_brightness"]),
                preset_id=int(action["rl_action_effect"]),
            )

        if "manual" in action:
            logger.debug("Manual triggered")
            target_device = self.getDeviceFromAddress(self._rhapi.db.option("rl_quickset_device", None))
            if target_device is None:
                logger.warning("nodeSwitch(manual): device not found in DB option")
                return
            self.control_service.apply_device_switch(
                target_device=target_device,
                brightness=int(self._rhapi.db.option("rl_quickset_brightness", None)),
                preset_id=int(self._rhapi.db.option("rl_quickset_effect", None)),
            )

    def groupSwitch(self, action, args=None):
        if "rl_action_group" in action:
            logger.debug("Action triggered")
            self.control_service.apply_group_switch(
                group_id=int(action["rl_action_group"]),
                brightness=int(action["rl_action_brightness"]),
                preset_id=int(action["rl_action_effect"]),
            )

        if "manual" in action:
            logger.debug("Manual triggered")
            self.control_service.apply_group_switch(
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
            group_selected = self.get_group_count()

        num_found = self.getDevices(groupFilter=0, addToGroup=group_selected)

        if num_found > 0 and group_selected == self.get_group_count():
            self.add_group(RL_DeviceGroup(new_group_str))
            self.uiGroupList = self.createUiGroupList()
            self.uiDiscoveryGroupList = self.createUiGroupList(True)
            self.register_settings()
            self.register_quickset_ui()
            self.registerActions()
            self._rhapi.ui.broadcast_ui("settings")
            self._rhapi.ui.broadcast_ui("run")

    def rl_write_json(self, data):
        return rh_rl_write_json(self, data)

    def rl_config_json_output(self, rhapi=None):
        return rh_rl_config_json_output(self, rhapi=rhapi)

    def rl_import_json(self, importer_class, rhapi, source, args):
        return rh_rl_import_json(self, importer_class, rhapi, source, args)
