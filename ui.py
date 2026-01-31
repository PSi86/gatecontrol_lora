from __future__ import annotations

import json
import logging
from datetime import datetime

from data_import import DataImporter
from data_export import DataExporter
from EventActions import ActionEffect
from RHUI import UIField, UIFieldType, UIFieldSelectOption

from .data import (
    GC_DeviceGroup,
    GC_FLAG_HAS_BRI,
    GC_FLAG_POWER_ON,
    get_dev_type_info,
    get_specials_config,
    get_ui_control_options,
    gc_devicelist,
    gc_grouplist,
)

logger = logging.getLogger(__name__)


class GateControlUIMixin:
    def _get_ui_control_select_options(self, control: str) -> list[UIFieldSelectOption]:
        options = get_ui_control_options(control, effect_list=getattr(self, "uiEffectList", None))
        if not options and control == "effect_select":
            return list(getattr(self, "uiEffectList", []) or [])
        return [UIFieldSelectOption(opt["value"], opt["label"]) for opt in options]

    # called in load_from_db which is called in onStartup
    def register_settings(self):
        logger.debug("GC: Registering Settings UI Elements")
        temp_uiGroupList = [UIFieldSelectOption(0, "New Group")]
        temp_uiGroupList += self.uiDiscoveryGroupList

        self._rhapi.ui.register_panel("esp_gc_settings", "GateControl Plugin", "settings")
        self._rhapi.fields.register_option(
            UIField("esp_gc_device_config", "Device Config", UIFieldType.TEXT, private=False),
            "esp_gc_settings",
        )
        self._rhapi.fields.register_option(
            UIField("esp_gc_groups_config", "Groups Config", UIFieldType.TEXT, private=False),
            "esp_gc_settings",
        )
        self._rhapi.fields.register_option(
            UIField(
                "esp_gc_assignToGroup",
                "Add discovered Devices to Group",
                UIFieldType.SELECT,
                options=temp_uiGroupList,
                value=temp_uiGroupList[0].value,
            ),
            "esp_gc_settings",
        )
        self._rhapi.fields.register_option(
            UIField("esp_gc_assignToNewGroup", "New Group Name", UIFieldType.TEXT, private=False),
            "esp_gc_settings",
        )
        self._rhapi.ui.register_quickbutton(
            "esp_gc_settings",
            "gc_btn_set_defaults",
            "Save Configuration",
            self.save_to_db,
            args={"manual": True},
        )
        self._rhapi.ui.register_quickbutton(
            "esp_gc_settings",
            "gc_btn_force_groups",
            "Set all Groups",
            self.forceGroups,
            args={"manual": True},
        )
        self._rhapi.ui.register_quickbutton(
            "esp_gc_settings",
            "gc_btn_get_devices",
            "Discover Devices",
            self.discoveryAction,
            args={"manual": True},
        )
        self._rhapi.ui.register_quickbutton(
            "esp_gc_settings",
            "gc_run_autodetect",
            "Detect USB Communicator",
            self.discoverPort,
            args={"manual": True},
        )

    def createUiDevList(self):
        logger.debug("GC: Creating UI Device Select Options")
        temp_ui_devlist = []
        for device in gc_devicelist:
            temp_ui_devlist.append(UIFieldSelectOption(device.addr, device.name))
        return temp_ui_devlist

    def gc_createUiDevList(
        self,
        dev_types: list[int] | None = None,
        capabilities: list[str] | None = None,
        outputDevices: bool = True,
        outputGroups: bool = True,
    ):
        logger.debug("GC: Creating filtered UI device/group list")
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

        selected_devs = [dev for dev in gc_devicelist if _matches_device(dev)]
        output = {"devices": [], "groups": []}

        if outputDevices:
            output["devices"] = [UIFieldSelectOption(dev.addr, dev.name) for dev in selected_devs]

        if outputGroups:
            group_ids = {int(getattr(dev, "groupId", 0) or 0) for dev in selected_devs}
            temp_groups = []
            for i, group in enumerate(gc_grouplist):
                if group.static_group and str(getattr(group, "name", "")) == "All WLED Gates":
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
        logger.debug("GC: Creating UI Device Select Options")
        temp_ui_grouplist = []
        for i, group in enumerate(gc_grouplist):
            if exclude_static is False or (exclude_static is True and group.static_group == 0):
                if group.static_group and str(getattr(group, "name", "")) == "All WLED Gates":
                    value = 255
                else:
                    value = i
                temp_ui_grouplist.append(UIFieldSelectOption(value, group.name))
        return temp_ui_grouplist

    def register_quickset_ui(self):
        effect_options = self._get_ui_control_select_options("effect_select")
        default_effect = effect_options[0].value if effect_options else "01"
        self._rhapi.ui.register_panel("esp_gc_quickset", "GateControl Quickset", "run")
        self._rhapi.fields.register_option(
            UIField("gc_quickset_group", "Gate Group", UIFieldType.SELECT, options=self.uiGroupList, value=self.uiGroupList[0].value),
            "esp_gc_quickset",
        )
        self._rhapi.fields.register_option(
            UIField("gc_quickset_effect", "Color", UIFieldType.SELECT, options=effect_options, value=default_effect),
            "esp_gc_quickset",
        )
        self._rhapi.fields.register_option(
            UIField("gc_quickset_brightness", "Brightness", UIFieldType.BASIC_INT, value=70),
            "esp_gc_quickset",
        )
        self._rhapi.ui.register_quickbutton(
            "esp_gc_quickset",
            "run_quickset",
            "Apply",
            self.groupSwitch,
            args={"manual": True},
        )

    def registerActions(self, args=None):
        logger.debug("Registering GateControl Actions")

        if args:
            if "register_fn" in args:
                self.action_reg_fn = args["register_fn"]
                logger.debug("Saved Actions Register Function in GateControl Instance")

        if not args and self.action_reg_fn:
            effect_options = self._get_ui_control_select_options("effect_select")
            default_effect = effect_options[0].value if effect_options else "01"
            for effect in [
                ActionEffect(
                    "GateControl Action",
                    self.groupSwitch,
                    [
                        UIField(
                            "gc_action_group",
                            "Gate Group",
                            UIFieldType.SELECT,
                            options=self.uiGroupList,
                            value=self.uiGroupList[0].value,
                        ),
                        UIField("gc_action_effect", "Color", UIFieldType.SELECT, options=effect_options, value=default_effect),
                        UIField("gc_action_brightness", "Brightness", UIFieldType.BASIC_INT, value=70),
                    ],
                    name="gcaction",
                )
            ]:
                self.action_reg_fn(effect)

            specials = get_specials_config()
            for cap_key, cap_info in specials.items():
                funcs = cap_info.get("functions", []) or []
                if not funcs:
                    continue
                cap_label = cap_info.get("label", cap_key)
                options_by_key = {opt.get("key"): opt for opt in cap_info.get("options", [])}
                for fn_info in funcs:
                    fn_key = fn_info.get("key")
                    fn_label = fn_info.get("label") or cap_label
                    vars_list = fn_info.get("vars", []) or []
                    allow_unicast = bool(fn_info.get("unicast"))
                    allow_broadcast = bool(fn_info.get("broadcast"))
                    fn_type = fn_info.get("type", "control")

                    if fn_type != "control":
                        continue

                    def _build_fields(mode):
                        fields = []
                        if mode == "device":
                            options = self.gc_createUiDevList(capabilities=[cap_key], outputGroups=False)["devices"]
                            if not options:
                                return None
                            fields.append(
                                UIField(
                                    f"gc_special_{fn_key}_device",
                                    "Device",
                                    UIFieldType.SELECT,
                                    options=options,
                                    value=options[0].value if options else "",
                                )
                            )
                        else:
                            options = self.gc_createUiDevList(capabilities=[cap_key], outputDevices=False)["groups"]
                            if not options:
                                return None
                            fields.append(
                                UIField(
                                    f"gc_special_{fn_key}_group",
                                    "Group",
                                    UIFieldType.SELECT,
                                    options=options,
                                    value=options[0].value if options else "",
                                )
                            )
                        for var in vars_list:
                            opt_meta = options_by_key.get(var, {})
                            label = opt_meta.get("label", var)
                            default_val = opt_meta.get("min", 0)
                            ui_meta = (fn_info.get("ui") or {}).get(var, {})
                            control = ui_meta.get("control")
                            if control:
                                select_options = self._get_ui_control_select_options(control)
                                if select_options:
                                    default_select = select_options[0].value
                                    if default_val is not None:
                                        for opt in select_options:
                                            try:
                                                if int(opt.value) == int(default_val):
                                                    default_select = opt.value
                                                    break
                                            except Exception:
                                                if str(opt.value) == str(default_val):
                                                    default_select = opt.value
                                                    break
                                    fields.append(
                                        UIField(
                                            f"gc_special_{fn_key}_{var}",
                                            label,
                                            UIFieldType.SELECT,
                                            options=select_options,
                                            value=default_select,
                                        )
                                    )
                                    continue
                            fields.append(
                                UIField(f"gc_special_{fn_key}_{var}", label, UIFieldType.BASIC_INT, value=default_val)
                            )
                        return fields

                    if allow_unicast:
                        fields = _build_fields("device")
                        if not fields:
                            continue
                        label = f"{cap_label} by Device" if fn_label == cap_label else f"{fn_label} by Device"
                        self.action_reg_fn(
                            ActionEffect(
                                label,
                                self._make_special_action_handler(fn_key, "device"),
                                fields,
                                name=f"gc_special_{fn_key}_device",
                            )
                        )
                    if allow_broadcast:
                        fields = _build_fields("group")
                        if not fields:
                            continue
                        label = f"{cap_label} by Group" if fn_label == cap_label else f"{fn_label} by Group"
                        self.action_reg_fn(
                            ActionEffect(
                                label,
                                self._make_special_action_handler(fn_key, "group"),
                                fields,
                                name=f"gc_special_{fn_key}_group",
                            )
                        )

    def _make_special_action_handler(self, fn_key: str, mode: str):
        def _handler(action, args=None):
            return self.specialAction(action, fn_key, mode)
        return _handler

    def specialAction(self, action, fn_key: str, mode: str):
        specials = get_specials_config()
        fn_info = None
        cap_key = None
        for cap, info in specials.items():
            for fn in info.get("functions", []) or []:
                if fn.get("key") == fn_key:
                    fn_info = fn
                    cap_key = cap
                    break
            if fn_info:
                break
        if not fn_info:
            logger.warning("specialAction: function not found: %s", fn_key)
            return

        vars_list = fn_info.get("vars", []) or []
        params = {}
        for var in vars_list:
            key = f"gc_special_{fn_key}_{var}"
            try:
                params[var] = int(action.get(key, 0))
            except Exception:
                params[var] = action.get(key, 0)

        target_device = None
        target_group = None
        if mode == "device":
            target_addr = action.get(f"gc_special_{fn_key}_device")
            if target_addr:
                target_device = self.getDeviceFromAddress(target_addr)
        else:
            try:
                target_group = int(action.get(f"gc_special_{fn_key}_group"))
            except Exception:
                target_group = None

        comm_name = fn_info.get("comm")
        if not comm_name:
            logger.warning("specialAction: missing comm function for %s", fn_key)
            return

        comm_fn = getattr(self, comm_name, None)
        if not callable(comm_fn):
            logger.warning("specialAction: comm function missing: %s", comm_name)
            return

        logger.debug("GC: specialAction %s (%s)", fn_key, cap_key or "unknown")
        try:
            comm_fn(targetDevice=target_device, targetGroup=target_group, params=params)
        except Exception:
            logger.exception("GC: specialAction failed: %s", fn_key)

    def register_gc_dataimporter(self, args):
        for importer in [
            DataImporter(
                "GateControl Config JSON",
                self.gc_import_json,
                None,
                [
                    UIField("gc_import_devices", "Import Devices", UIFieldType.CHECKBOX, value=False),
                    UIField("gc_import_devgroups", "Import Groups", UIFieldType.CHECKBOX, value=False),
                ],
            ),
        ]:
            args["register_fn"](importer)

    def register_gc_dataexporter(self, args):
        for exporter in [
            DataExporter(
                "GateControl Config JSON",
                self.gc_write_json,
                self.gc_config_json_output,
            )
        ]:
            args["register_fn"](exporter)

    # not used currently
    def registerHandlers(self, args):
        self._rhapi.ui.message_notify("text")

    def gateSwitch(self, action, args=None):
        # control triggered by ActionEffect
        if "gc_action_device" in action:
            logger.debug("Action triggered")
            targetDevice = self.getDeviceFromAddress(action["gc_action_device"])
            if targetDevice is None:
                logger.warning("gateSwitch: device not found: %r", action["gc_action_device"])
                return
            targetDevice.brightness = int(action["gc_action_brightness"])
            targetDevice.presetId = int(action["gc_action_effect"])
            targetDevice.flags = (GC_FLAG_POWER_ON if int(action["gc_action_brightness"]) > 0 else 0) | GC_FLAG_HAS_BRI

            logger.debug("sendGateControl action call - device")
            self.sendGateControl(targetDevice)

        # control triggered by UI button press
        if "manual" in action:
            logger.debug("Manual triggered")
            targetDevice = self.getDeviceFromAddress(self._rhapi.db.option("gc_quickset_device", None))
            if targetDevice is None:
                logger.warning("gateSwitch(manual): device not found in DB option")
                return
            targetDevice.brightness = int(self._rhapi.db.option("gc_quickset_brightness", None))
            targetDevice.presetId = int(self._rhapi.db.option("gc_quickset_effect", None))
            targetDevice.flags = (
                GC_FLAG_POWER_ON if int(self._rhapi.db.option("gc_quickset_brightness", None)) > 0 else 0
            ) | GC_FLAG_HAS_BRI

            logger.debug("sendGateControl manual call - device")
            self.sendGateControl(targetDevice)

    def groupSwitch(self, action, args=None):
        # control triggered by ActionEffect
        if "gc_action_group" in action:
            logger.debug("Action triggered")
            targetGroup = int(action["gc_action_group"])
            targetBrightness = int(action["gc_action_brightness"])
            targetEffect = int(action["gc_action_effect"])
            targetFlags = (GC_FLAG_POWER_ON if int(action["gc_action_brightness"]) > 0 else 0) | GC_FLAG_HAS_BRI

            logger.debug("GC: groupSwitch called by Action (event based)")
            self.sendGroupControl(targetGroup, targetFlags, targetEffect, targetBrightness)

        # control triggered by UI button press
        if "manual" in action:
            logger.debug("Manual triggered")
            targetGroup = int(self._rhapi.db.option("gc_quickset_group", None))
            targetBrightness = int(self._rhapi.db.option("gc_quickset_brightness", None))
            targetEffect = int(self._rhapi.db.option("gc_quickset_effect", None))
            targetFlags = (
                GC_FLAG_POWER_ON if int(self._rhapi.db.option("gc_quickset_brightness", None)) > 0 else 0
            ) | GC_FLAG_HAS_BRI

            logger.debug("GC: groupSwitch called from UI")
            self.sendGroupControl(targetGroup, targetFlags, int(targetEffect), targetBrightness)

    def discoveryAction(self, args):
        group_selected = int(self._rhapi.db.option("esp_gc_assignToGroup", None))
        new_group_str = self._rhapi.db.option("esp_gc_assignToNewGroup", None)

        if group_selected == 0:
            if not new_group_str or len(new_group_str) == 0:
                new_group_str = "New Group"

            new_group_str += " " + datetime.now().strftime("%Y%m%d_%H%M%S")
            group_selected = len(gc_grouplist)

        num_found = self.getDevices(groupFilter=0, addToGroup=group_selected)

        if num_found > 0 and group_selected == len(gc_grouplist):
            gc_grouplist.append(GC_DeviceGroup(new_group_str))
            self.uiGroupList = self.createUiGroupList()
            self.uiDiscoveryGroupList = self.createUiGroupList(True)
            self.register_settings()
            self.register_quickset_ui()
            self.registerActions()
            self._rhapi.ui.broadcast_ui("settings")
            self._rhapi.ui.broadcast_ui("run")

    # GC Data Exporter write function
    def gc_write_json(self, data):
        payload = json.dumps(data, indent="\t")
        return {
            "data": payload,
            "encoding": "application/json",
            "ext": "json",
        }

    # GC Data Exporter data collector function
    def gc_config_json_output(self, rhapi=None):
        payload = {}
        payload["help"] = ["See help tags below current configuration elements"]

        payload["gc_devices"] = [obj.__dict__ for obj in gc_devicelist]
        payload["gc_groups"] = [obj.__dict__ for obj in gc_grouplist]

        payload["help/gc_devices"] = ["Device List of known devices"]
        payload["help/gc_devices/addr"] = ["MAC of the device without ':' as separator"]
        payload["help/gc_devices/dev_type"] = ["IDENTIFY_COMMUNICATOR:1, WLED_REV3:10, WLED_REV4:11, WLED_STARTBLOCK_REV3:50"]
        payload["help/gc_devices/name"] = ["UI: shown name of a device"]
        payload["help/gc_devices/groupId"] = [
            "Used to group devices for control. Valid numbers start with 3 (0-2 are reserved for device type based groups)"
        ]
        payload["help/gc_devices/flags"] = [
            "bitmask: POWER_ON(0x01), ARM_ON_SYNC(0x02), HAS_BRI(0x04), FORCE_TT0(0x08), FORCE_REAPPLY(0x10)"
        ]
        payload["help/gc_devices/presetId"] = ["1-255: WLED preset index / mapping used by the LoRa usermod"]
        payload["help/gc_devices/brightness"] = [
            "0: off, 1-255:dimming, special function with value 1: IR Controllers will spam the 'darker' signal to set IR devices to absolute minimum brightness."
        ]
        payload["help/gc_groups"] = ["Lookup list for the groupId definitions in the device entries"]
        payload["help/gc_groups/name"] = ["UI: shown name of a group"]
        payload["help/gc_groups/static_group"] = [
            "0: normal, changeable group, 1: predefined group that will be read only in UI"
        ]
        payload["help/gc_groups/dev_type"] = [
            "0:call all devices set to this group's id. dev_type can target a specific device type when supported."
        ]
        payload["help/backup"] = [
            "If there is an issue with configuration you can create a clean config based on the example elements. (delete '_backup' from element name)"
        ]

        payload["gc_devices_backup"] = [
            {"addr": "3C84279EBFE4", "dev_type": 10, "name": "WLED 3C84279EBFE4", "groupId": 0, "flags": 1, "presetId": 1, "brightness": 70}
        ]
        payload["gc_groups_backup"] = [{"name": "All WLED Gates", "static_group": 1, "dev_type": 0}]
        return payload

    # GC Data Importer function: write imported data to DB
    def gc_import_json(self, importer_class, rhapi, source, args):
        if not source:
            return False

        try:
            data = json.loads(source)
        except Exception as ex:
            logger.error("Unable to import file: %s", str(ex))
            return False

        if "gc_import_devices" in args and args["gc_import_devices"]:
            logger.debug("Checked Device Import Option")
            if "gc_devices" in data:
                logger.debug("Importing GateControl Devices...")
                rhapi.db.option_set("esp_gc_device_config", str(data["gc_devices"]))
            else:
                logger.error("JSON contains no GateControl Devices")

        if "gc_import_devgroups" in args and args["gc_import_devgroups"]:
            logger.debug("Checked Group Import Option")
            if "gc_groups" in data:
                logger.debug("Importing GateControl Groups...")
                rhapi.db.option_set("esp_gc_groups_config", str(data["gc_groups"]))
            else:
                logger.error("JSON contains no GateControl Device Groups")

        self.load_from_db()
        return True
