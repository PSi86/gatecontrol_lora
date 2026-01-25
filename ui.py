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
    gc_devicelist,
    gc_grouplist,
)

logger = logging.getLogger(__name__)


class GateControlUIMixin:
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

    def createUiGroupList(self, exclude_static=False):
        logger.debug("GC: Creating UI Device Select Options")
        temp_ui_grouplist = []
        for i, group in enumerate(gc_grouplist):
            if exclude_static is False or (exclude_static is True and group.static_group == 0):
                if group.static_group and str(getattr(group, "name", "")) == "All WLED Devices":
                    value = 255
                else:
                    value = i
                temp_ui_grouplist.append(UIFieldSelectOption(value, group.name))
        return temp_ui_grouplist

    def register_quickset_ui(self):
        default_effect = self.uiEffectList[0].value if self.uiEffectList else "01"
        self._rhapi.ui.register_panel("esp_gc_quickset", "GateControl Quickset", "run")
        self._rhapi.fields.register_option(
            UIField("gc_quickset_group", "Gate Group", UIFieldType.SELECT, options=self.uiGroupList, value=self.uiGroupList[0].value),
            "esp_gc_quickset",
        )
        self._rhapi.fields.register_option(
            UIField("gc_quickset_effect", "Color", UIFieldType.SELECT, options=self.uiEffectList, value=default_effect),
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
            default_effect = self.uiEffectList[0].value if self.uiEffectList else "01"
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
                        UIField("gc_action_effect", "Color", UIFieldType.SELECT, options=self.uiEffectList, value=default_effect),
                        UIField("gc_action_brightness", "Brightness", UIFieldType.BASIC_INT, value=70),
                    ],
                    name="gcaction",
                )
            ]:
                self.action_reg_fn(effect)

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
        payload["help/gc_devices/type"] = ["IDENTIFY_COMMUNICATOR:1, WLED_REV3:10, WLED_REV4:11, WLED_STARTBLOCK_REV3:50"]
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
        payload["help/gc_groups/device_type"] = [
            "0:call all devices set to this group's id. Device_type: 20,21,22 - send to all devices of that type ignoring groupIds"
        ]
        payload["help/backup"] = [
            "If there is an issue with configuration you can create a clean config based on the example elements. (delete '_backup' from element name)"
        ]

        payload["gc_devices_backup"] = [
            {"addr": "3C84279EBFE4", "type": 24, "name": "WLED 3C84279EBFE4", "groupId": 0, "flags": 1, "presetId": 1, "brightness": 70}
        ]
        payload["gc_groups_backup"] = [{"name": "All WLED Devices", "static_group": 1, "device_type": 0}]
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
