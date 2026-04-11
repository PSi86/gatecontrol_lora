"""RotorHazard-specific UI adapter."""

from __future__ import annotations

import logging
from datetime import datetime

from RHUI import UIField, UIFieldType, UIFieldSelectOption

from ....data import RL_DeviceGroup, get_dev_type_info, get_specials_config
from .actions import RotorHazardActionsMixin
from .dataio import RotorHazardDataIOMixin
from .source import RotorHazardSource

logger = logging.getLogger(__name__)


class RotorHazardUIAdapter(RotorHazardActionsMixin, RotorHazardDataIOMixin):
    def __init__(self, controller, rhapi):
        self.controller = controller
        self.rhapi = rhapi
        self.source = RotorHazardSource(controller, rhapi)

    def _devices(self):
        return self.controller.device_repository.list()

    def _groups(self):
        return self.controller.group_repository.list()

    def _get_select_options(self, fn_key: str, var_key: str) -> list[UIFieldSelectOption]:
        context = {"rhapi": self.rhapi, "gc": self.controller}
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

    def register_settings(self):
        logger.debug("RL: Registering Settings UI Elements")
        temp_uiGroupList = [UIFieldSelectOption(0, "New Group")]
        temp_uiGroupList += self.controller.uiDiscoveryGroupList

        self.rhapi.ui.register_panel("rl_settings", "RaceLink Plugin", "settings")
        self.rhapi.fields.register_option(UIField("rl_device_config", "Device Config", UIFieldType.TEXT, private=False), "rl_settings")
        self.rhapi.fields.register_option(UIField("rl_groups_config", "Groups Config", UIFieldType.TEXT, private=False), "rl_settings")
        self.rhapi.fields.register_option(
            UIField("rl_assignToGroup", "Add discovered Devices to Group", UIFieldType.SELECT, options=temp_uiGroupList, value=temp_uiGroupList[0].value),
            "rl_settings",
        )
        self.rhapi.fields.register_option(UIField("rl_assignToNewGroup", "New Group Name", UIFieldType.TEXT, private=False), "rl_settings")
        self.rhapi.ui.register_quickbutton("rl_settings", "rl_btn_set_defaults", "Save Configuration", self.controller.save_to_db, args={"manual": True})
        self.rhapi.ui.register_quickbutton("rl_settings", "rl_btn_force_groups", "Set all Groups", self.controller.forceGroups, args={"manual": True})
        self.rhapi.ui.register_quickbutton("rl_settings", "rl_btn_get_devices", "Discover Devices", self.discoveryAction, args={"manual": True})
        self.rhapi.ui.register_quickbutton("rl_settings", "rl_run_autodetect", "Detect USB Communicator", self.controller.discoverPort, args={"manual": True})

    def createUiDevList(self):
        logger.debug("RL: Creating UI Device Select Options")
        return [UIFieldSelectOption(device.addr, device.name) for device in self._devices()]

    def rl_createUiDevList(self, dev_types=None, capabilities=None, outputDevices=True, outputGroups=True):
        logger.debug("RL: Creating filtered UI device/group list")
        dev_types_set = set(int(d) for d in dev_types) if dev_types else None
        cap_set = set(capabilities) if capabilities else None

        def _matches_device(dev):
            if dev_types_set and int(getattr(dev, "dev_type", 0) or 0) not in dev_types_set:
                return False
            if cap_set:
                caps = set(get_dev_type_info(getattr(dev, "dev_type", 0)).get("caps", []))
                if not cap_set.issubset(caps):
                    return False
            return True

        selected_devs = [dev for dev in self._devices() if _matches_device(dev)]
        output = {"devices": [], "groups": []}

        if outputDevices:
            output["devices"] = [UIFieldSelectOption(dev.addr, dev.name) for dev in selected_devs]

        if outputGroups:
            group_ids = {int(getattr(dev, "groupId", 0) or 0) for dev in selected_devs}
            temp_groups = []
            for i, group in enumerate(self._groups()):
                if group.static_group and str(getattr(group, "name", "")) == "All WLED Nodes":
                    if cap_set and "WLED" not in cap_set:
                        continue
                    if selected_devs:
                        temp_groups.append(UIFieldSelectOption(255, group.name))
                    continue
                if i in group_ids:
                    temp_groups.append(UIFieldSelectOption(i, group.name))
            output["groups"] = temp_groups

        return output

    def createUiGroupList(self, exclude_static=False):
        logger.debug("RL: Creating UI Device Select Options")
        temp_ui_grouplist = []
        for i, group in enumerate(self._groups()):
            if exclude_static is False or (exclude_static is True and group.static_group == 0):
                value = 255 if group.static_group and str(getattr(group, "name", "")) == "All WLED Nodes" else i
                temp_ui_grouplist.append(UIFieldSelectOption(value, group.name))
        return temp_ui_grouplist

    def register_quickset_ui(self):
        effect_options = self._get_select_options("wled_control", "presetId")
        default_effect = effect_options[0].value if effect_options else "01"
        self.rhapi.ui.register_panel("rl_quickset", "RaceLink Quickset", "run")
        self.rhapi.fields.register_option(UIField("rl_quickset_group", "Node Group", UIFieldType.SELECT, options=self.controller.uiGroupList, value=self.controller.uiGroupList[0].value), "rl_quickset")
        self.rhapi.fields.register_option(UIField("rl_quickset_effect", "Color", UIFieldType.SELECT, options=effect_options, value=default_effect), "rl_quickset")
        self.rhapi.fields.register_option(UIField("rl_quickset_brightness", "Brightness", UIFieldType.BASIC_INT, value=70), "rl_quickset")
        self.rhapi.ui.register_quickbutton("rl_quickset", "run_quickset", "Apply", self.groupSwitch, args={"manual": True})

    def apply_presets_options(self, parsed):
        if not parsed:
            self.controller.uiEffectList = [UIFieldSelectOption("0", "No presets.json found")]
        else:
            self.controller.uiEffectList = [UIFieldSelectOption(str(pid), name) for pid, name in parsed]
        try:
            self.register_quickset_ui()
            self.registerActions()
            self.rhapi.ui.broadcast_ui("run")
        except Exception:
            pass

    def discoveryAction(self, args):
        group_selected = int(self.rhapi.db.option("rl_assignToGroup", None))
        new_group_str = self.rhapi.db.option("rl_assignToNewGroup", None)

        if group_selected == 0:
            if not new_group_str or len(new_group_str) == 0:
                new_group_str = "New Group"
            new_group_str += " " + datetime.now().strftime("%Y%m%d_%H%M%S")
            group_selected = len(self._groups())

        num_found = self.controller.getDevices(groupFilter=0, addToGroup=group_selected)

        if num_found > 0 and group_selected == len(self._groups()):
            self.controller.group_repository.append(RL_DeviceGroup(new_group_str))
            self.controller.uiGroupList = self.createUiGroupList()
            self.controller.uiDiscoveryGroupList = self.createUiGroupList(True)
            self.register_settings()
            self.register_quickset_ui()
            self.registerActions()
            self.rhapi.ui.broadcast_ui("settings")
            self.rhapi.ui.broadcast_ui("run")

    def get_current_heat_slot_list(self):
        return self.source.get_current_heat_slot_list()
