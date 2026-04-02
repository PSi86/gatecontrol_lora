from __future__ import annotations

import json
import logging

from data_export import DataExporter
from data_import import DataImporter
from RHUI import UIField, UIFieldType

logger = logging.getLogger(__name__)


def register_rl_dataimporter(gc, args):
    args["register_fn"](
        DataImporter(
            "RaceLink Config JSON",
            gc.rl_import_json,
            None,
            [
                UIField("rl_import_devices", "Import Devices", UIFieldType.CHECKBOX, value=False),
                UIField("rl_import_devgroups", "Import Groups", UIFieldType.CHECKBOX, value=False),
            ],
        )
    )


def register_rl_dataexporter(gc, args):
    args["register_fn"](
        DataExporter(
            "RaceLink Config JSON",
            gc.rl_write_json,
            gc.rl_config_json_output,
        )
    )


def rl_write_json(gc, data):
    payload = json.dumps(data, indent="\t")
    return {
        "data": payload,
        "encoding": "application/json",
        "ext": "json",
    }


def rl_config_json_output(gc, rhapi=None):
    payload = {}
    payload["help"] = ["See help tags below current configuration elements"]
    payload["rl_devices"] = [obj.__dict__ for obj in gc.list_devices()]
    payload["rl_groups"] = [obj.__dict__ for obj in gc.list_group_objects()]

    payload["help/rl_devices"] = ["Device List of known devices"]
    payload["help/rl_devices/addr"] = ["MAC of the device without ':' as separator"]
    payload["help/rl_devices/dev_type"] = ["IDENTIFY_COMMUNICATOR:1, WLED_REV3:10, WLED_REV4:11, WLED_STARTBLOCK_REV3:50"]
    payload["help/rl_devices/name"] = ["UI: shown name of a device"]
    payload["help/rl_devices/groupId"] = [
        "Used to group devices for control. Valid numbers start with 3 (0-2 are reserved for device type based groups)"
    ]
    payload["help/rl_devices/flags"] = [
        "bitmask: POWER_ON(0x01), ARM_ON_SYNC(0x02), HAS_BRI(0x04), FORCE_TT0(0x08), FORCE_REAPPLY(0x10)"
    ]
    payload["help/rl_devices/presetId"] = ["1-255: WLED preset index / mapping used by the LoRa usermod"]
    payload["help/rl_devices/brightness"] = [
        "0: off, 1-255:dimming, special function with value 1: IR Controllers will spam the 'darker' signal to set IR devices to absolute minimum brightness."
    ]
    payload["help/rl_groups"] = ["Lookup list for the groupId definitions in the device entries"]
    payload["help/rl_groups/name"] = ["UI: shown name of a group"]
    payload["help/rl_groups/static_group"] = ["0: normal, changeable group, 1: predefined group that will be read only in UI"]
    payload["help/rl_groups/dev_type"] = [
        "0:call all devices set to this group's id. dev_type can target a specific device type when supported."
    ]
    payload["help/backup"] = [
        "If there is an issue with configuration you can create a clean config based on the example elements. (delete '_backup' from element name)"
    ]

    payload["rl_devices_backup"] = [
        {"addr": "3C84279EBFE4", "dev_type": 10, "name": "WLED 3C84279EBFE4", "groupId": 0, "flags": 1, "presetId": 1, "brightness": 70}
    ]
    payload["rl_groups_backup"] = [{"name": "All WLED Nodes", "static_group": 1, "dev_type": 0}]
    return payload


def rl_import_json(gc, importer_class, rhapi, source, args):
    if not source:
        return False

    try:
        data = json.loads(source)
    except Exception as ex:
        logger.error("Unable to import file: %s", str(ex))
        return False

    if args.get("rl_import_devices"):
        logger.debug("Checked Device Import Option")
        if "rl_devices" in data:
            logger.debug("Importing RaceLink Devices...")
            rhapi.db.option_set("rl_device_config", str(data["rl_devices"]))
        else:
            logger.error("JSON contains no RaceLink Devices")

    if args.get("rl_import_devgroups"):
        logger.debug("Checked Group Import Option")
        if "rl_groups" in data:
            logger.debug("Importing RaceLink Groups...")
            rhapi.db.option_set("rl_groups_config", str(data["rl_groups"]))
        else:
            logger.error("JSON contains no RaceLink Device Groups")

    gc.load_from_db()
    return True
