from __future__ import annotations

import logging
import re
import threading
import time
import json
from typing import Dict, Optional, Tuple, Union

from .ui import RaceLinkUIMixin
from RHUI import UIFieldSelectOption
from .data import (
    RL_Device,
    RL_DeviceGroup,
    RL_Dev_Type,
    build_specials_state,
    create_device,
    get_dev_type_info,
    RL_FLAG_HAS_BRI,
    RL_FLAG_POWER_ON,
    rl_backup_devicelist,
    rl_backup_grouplist,
    rl_devicelist,
    rl_grouplist,
)

# ---- lora proto registry (auto-generated from lora_proto.h) ----
try:
    from . import lora_proto_auto as LPA
except Exception:
    import lora_proto_auto as LPA

# ---- transport import (tolerant to both package and flat layout) ----
try:
    from .racelink_transport import (
        LP,
        EV_ERROR,
        EV_RX_WINDOW_CLOSED,
        EV_RX_WINDOW_OPEN,
        LoRaUSB,
        _mac_last3_from_hex,
    )
except Exception:
    from racelink_transport import (
        LP,
        EV_ERROR,
        EV_RX_WINDOW_CLOSED,
        EV_RX_WINDOW_OPEN,
        LoRaUSB,
        _mac_last3_from_hex,
    )

logger = logging.getLogger(__name__)

_STARTBLOCK_VER = 0x01

_DE_UMLAUT_MAP = {
    "ä": "ae",
    "ö": "oe",
    "ü": "ue",
    "ß": "ss",
    "Ä": "AE",
    "Ö": "OE",
    "Ü": "UE",
}

_ALLOWED_NAME_RE = re.compile(r"[^A-Z0-9 _\-\.\+]", re.IGNORECASE)


def _sanitize_pilot_name(name: str, max_len: int = 32) -> str:
    if not name:
        return ""
    for k, v in _DE_UMLAUT_MAP.items():
        name = name.replace(k, v)
    name = name.strip().upper()
    name = _ALLOWED_NAME_RE.sub(" ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:max_len] if max_len > 0 else name


def _encode_channel_fixed2(label: str) -> bytes:
    """
    Immer exakt 2 Bytes ASCII.
    - upper()
    - padding mit '-' falls zu kurz
    - truncate falls zu lang
    """
    lab = (label or "").strip().upper()
    lab2 = (lab + "--")[:2]
    return lab2.encode("ascii", errors="replace")


def build_startblock_payload_v1(
    slot: int,
    channel_label: str,
    pilot_name: str,
    max_name_len: int = 32,
    name_encoding: str = "ascii",
) -> bytes:
    """
    [ver][slot][chan2][name_len u8][name bytes]
    """
    slot_b = max(0, min(255, int(slot)))
    chan2 = _encode_channel_fixed2(channel_label)

    clean_name = _sanitize_pilot_name(pilot_name, max_len=max_name_len)

    if name_encoding.lower() == "utf-8":
        name_bytes = clean_name.encode("utf-8", errors="replace")
    else:
        name_bytes = clean_name.encode("ascii", errors="replace")

    if len(name_bytes) > 255:
        name_bytes = name_bytes[:255]

    out = bytearray()
    out.append(_STARTBLOCK_VER)
    out.append(slot_b)
    out.extend(chan2)  # 2 bytes
    out.append(len(name_bytes))  # 1 byte
    out.extend(name_bytes)
    return bytes(out)


class RaceLink_LoRa(RaceLinkUIMixin):
    def __init__(self, rhapi, name, label):
        self._rhapi = rhapi
        self.name = name
        self.label = label
        self.lora = None
        self.ready = False
        self.action_reg_fn = None
        self.deviceCfgValid = False
        self.groupCfgValid = False
        self.uiDeviceList = None
        self.uiGroupList = None
        self.uiDiscoveryGroupList = None

        # Transport-level pending expectation (for online/offline determination).
        self._pending_expect = None  # dict with keys: dev, rule, opcode7, sender_last3, ts

        self._transport_hooks_installed = False
        self._pending_config = {}
        self._reconnect_in_progress = False
        self._last_reconnect_ts = 0.0
        self._last_error_notify_ts = 0.0
        # Basic colors: 1-9; Basic effects: 10-19; Special Effects (WLED only): 20-100
        self.uiEffectList = [
            UIFieldSelectOption("01", "Red"),
            UIFieldSelectOption("02", "Green"),
            UIFieldSelectOption("03", "Blue"),
            UIFieldSelectOption("04", "White"),
            UIFieldSelectOption("05", "Yellow"),
            UIFieldSelectOption("06", "Cyan"),
            UIFieldSelectOption("07", "Magenta"),
            UIFieldSelectOption("10", "Blink Multicolor"),
            UIFieldSelectOption("11", "Pulse White"),
            UIFieldSelectOption("12", "Colorloop"),
            UIFieldSelectOption("13", "Blink RGB"),
            UIFieldSelectOption("20", "WLED Chaser"),
            UIFieldSelectOption("21", "WLED Chaser inverted"),
            UIFieldSelectOption("22", "WLED Rainbow"),
        ]

    def onStartup(self, _args):
        self.load_from_db()
        self.discoverPort({})

    def save_to_db(self, args):
        logger.debug("RL: Writing current states to Database")
        config_str_devices = str([obj.__dict__ for obj in rl_devicelist])
        self._rhapi.db.option_set("rl_device_config", config_str_devices)

        if len(rl_grouplist) >= len(rl_backup_grouplist):
            config_str_groups = str([obj.__dict__ for obj in rl_grouplist])
        else:
            config_str_groups = str([obj.__dict__ for obj in rl_backup_grouplist])
        self._rhapi.db.option_set("rl_groups_config", config_str_groups)

    def load_from_db(self):
        logger.debug("RL: Applying config from Database")
        config_str_devices = self._rhapi.db.option("rl_device_config", None)
        config_str_groups = self._rhapi.db.option("rl_groups_config", None)

        if config_str_devices is None:
            config_str_devices = str([obj.__dict__ for obj in rl_backup_devicelist])
            self._rhapi.db.option_set("rl_device_config", config_str_devices)

        if config_str_devices == "":
            config_str_devices = "[]"
            self._rhapi.db.option_set("rl_device_config", config_str_devices)

        config_list_devices = list(eval(config_str_devices))
        rl_devicelist.clear()

        for device in config_list_devices:
            logger.debug(device)
            try:
                flags = device.get("flags", None)
                presetId = device.get("presetId", None)

                if flags is None:
                    legacy_state = int(device.get("state", 1) or 0)
                    flags = RL_FLAG_POWER_ON if legacy_state else 0
                    if "brightness" in device:
                        flags |= RL_FLAG_HAS_BRI

                if presetId is None:
                    presetId = int(device.get("effect", 1) or 1)

                brightness = int(device.get("brightness", 70) or 0)

                dev_type = device.get("dev_type", None)
                if dev_type is None:
                    dev_type = device.get("device_type", None)
                if dev_type is None:
                    dev_type = device.get("caps", device.get("type", 0))

                special_state = build_specials_state(int(dev_type or 0), device)
                rl_devicelist.append(
                    create_device(
                        addr=str(device.get("addr", "")).upper(),
                        dev_type=int(dev_type or 0),
                        name=str(device.get("name", "")),
                        groupId=int(device.get("groupId", 0) or 0),
                        version=int(device.get("version", 0) or 0),
                        caps=int(dev_type or 0),
                        flags=int(flags) & 0xFF,
                        presetId=int(presetId) & 0xFF,
                        brightness=brightness & 0xFF,
                        specials=special_state,
                    )
                )
            except Exception:
                logger.exception("RL: failed to load device entry from DB: %r", device)
                continue

        if config_str_groups is None or config_str_groups == "":
            config_str_groups = str([obj.__dict__ for obj in rl_backup_grouplist])
            self._rhapi.db.option_set("rl_groups_config", config_str_groups)

        config_list_groups = list(eval(config_str_groups))
        rl_grouplist.clear()

        for group in config_list_groups:
            logger.debug(group)
            group_dev_type = group.get("dev_type", group.get("device_type", 0))
            rl_grouplist.append(RL_DeviceGroup(group["name"], group["static_group"], group_dev_type))

        rl_grouplist[:] = [
            g
            for g in rl_grouplist
            if str(getattr(g, "name", "")).strip().lower() not in {"unconfigured", "all wled devices"}
        ]

        if not any(str(getattr(g, "name", "")).strip().lower() == "all wled nodes" for g in rl_grouplist):
            rl_grouplist.append(RL_DeviceGroup("All WLED Nodes", static_group=1, dev_type=0))
        else:
            for g in rl_grouplist:
                if str(getattr(g, "name", "")).strip().lower() == "all wled nodes":
                    g.name = "All WLED Nodes"
                    g.static_group = 1
                    g.dev_type = 0

        self.uiDeviceList = self.createUiDevList()
        self.uiGroupList = self.createUiGroupList()
        self.uiDiscoveryGroupList = self.createUiGroupList(True)
        self.register_settings()
        self.register_quickset_ui()
        self.registerActions()
        self._rhapi.ui.broadcast_ui("settings")
        self._rhapi.ui.broadcast_ui("run")

    def discoverPort(self, args):
        """Initialize communicator via LoRaUSB only. No direct serial here."""
        port = self._rhapi.db.option("psi_comms_port", None)
        try:
            self._transport_hooks_installed = False
            self.lora = LoRaUSB(port=port, on_event=None)
            ok = self.lora.discover_and_open()
            if ok:
                self.lora.start()
                self.ready = True
                self._install_transport_hooks()
                used = self.lora.port or "unknown"
                mac = getattr(self.lora, "ident_mac", None)
                if mac:
                    logger.info("RaceLink Communicator ready on %s with MAC: %s", used, mac)
                    if "manual" in args:
                        self._rhapi.ui.message_notify(self._rhapi.__("RaceLink Communicator ready on {} with MAC: {}").format(used, mac))
                return
            else:
                self.ready = False
                logger.warning("No RaceLink Communicator module discovered or configured")
                if "manual" in args:
                    self._rhapi.ui.message_notify(self._rhapi.__("No RaceLink Communicator module discovered or configured"))
        except Exception as ex:
            self.ready = False
            logger.error("LoRaUSB init failed: %s", ex)
            if "manual" in args:
                self._rhapi.ui.message_notify(self._rhapi.__("Failed to initialize communicator: {}").format(str(ex)))

    def onRaceStart(self, _args):
        logger.warning("RaceLink Race Start Event")

    def onRaceFinish(self, _args):
        logger.warning("RaceLink Race Finish Event")

    def onRaceStop(self, _args):
        logger.warning("RaceLink Race Stop Event")

    def onSendMessage(self, args):
        logger.warning("Event onSendMessage")

    def getDevices(self, groupFilter=255, targetDevice=None, addToGroup=-1):
        if not getattr(self, "lora", None):
            logger.warning("getDevices: communicator not ready")
            return 0

        self._install_transport_hooks()

        if targetDevice is None:
            recv3 = b"\xFF\xFF\xFF"
            groupId = int(groupFilter) & 0xFF
        else:
            recv3 = _mac_last3_from_hex(targetDevice.addr)
            groupId = int(targetDevice.groupId) & 0xFF

        found = 0
        responders = set()

        def _collect(ev: dict) -> bool:
            nonlocal found
            try:
                if ev.get("opc") == LP.OPC_DEVICES and ev.get("reply") == "IDENTIFY_REPLY":
                    found += 1
                    mac6 = ev.get("mac6")
                    if isinstance(mac6, (bytes, bytearray)) and len(mac6) == 6:
                        responders.add(bytes(mac6).hex().upper())
                    else:
                        sender3 = ev.get("sender3")
                        sender_hex = self._to_hex_str(sender3)
                        if sender_hex:
                            responders.add(sender_hex.upper())
                    return True
            except Exception:
                pass
            return False

        logger.debug("GET_DEVICES -> recv3=%s group=%d flags=%d", recv3.hex().upper(), groupId, 0)

        try:
            self.lora.drain_events(0.0)
        except Exception:
            pass

        self._wait_rx_window(
            lambda: self.lora.send_get_devices(recv3=recv3, group_id=groupId, flags=0),
            collect_pred=_collect,
            fail_safe_s=8.0,
        )

        if addToGroup > 0 and addToGroup < 255:
            for addr in responders:
                dev = self.getDeviceFromAddress(addr)
                if not dev:
                    continue
                dev.groupId = addToGroup
                self.setNodeGroupId(dev)

        if hasattr(self, "_rhapi") and hasattr(self._rhapi, "ui"):
            if addToGroup > 0 and addToGroup < 255:
                msg = "Device Discovery finished with {} devices found and added to GroupId: {}".format(found, addToGroup)
            else:
                msg = "Device Discovery finished with {} devices found.".format(found)
            self._rhapi.ui.message_notify(msg)
        return found

    def getStatus(self, groupFilter=255, targetDevice=None):
        if not getattr(self, "lora", None):
            logger.warning("getStatus: communicator not ready")
            return 0

        self._install_transport_hooks()

        if targetDevice is None:
            recv3 = b"\xFF\xFF\xFF"
            groupId = int(groupFilter) & 0xFF
            sender_filter = None
        else:
            recv3 = _mac_last3_from_hex(targetDevice.addr)
            groupId = int(targetDevice.groupId) & 0xFF
            sender_filter = recv3.hex().upper()

        updated = 0
        responders = set()

        def _collect(ev: dict) -> bool:
            nonlocal updated
            try:
                if ev.get("opc") == LP.OPC_STATUS and ev.get("reply") == "STATUS_REPLY":
                    if sender_filter:
                        sender3 = ev.get("sender3")
                        if isinstance(sender3, (bytes, bytearray)) and bytes(sender3).hex().upper() != sender_filter:
                            return False
                    updated += 1
                    try:
                        mac6 = ev.get("mac6")
                        if isinstance(mac6, (bytes, bytearray)) and len(mac6) == 6:
                            responders.add(bytes(mac6).hex().upper())
                        else:
                            sender3 = ev.get("sender3")
                            if isinstance(sender3, (bytes, bytearray)) and len(sender3) == 3:
                                responders.add(bytes(sender3).hex().upper())
                    except Exception:
                        pass
                    return True
            except Exception:
                pass
            return False

        try:
            self.lora.drain_events(0.0)
        except Exception:
            pass

        collected, got_closed = self._wait_rx_window(
            lambda: self.lora.send_get_status(recv3=recv3, group_id=groupId, flags=0),
            collect_pred=_collect,
            fail_safe_s=8.0,
        )

        if got_closed:
            if targetDevice is not None:
                if updated == 0:
                    try:
                        targetDevice.mark_offline("Missing reply (STATUS)")
                    except Exception:
                        pass
            else:
                if groupFilter == 255:
                    targets = list(rl_devicelist)
                else:
                    targets = [dev for dev in rl_devicelist if int(getattr(dev, "groupId", 0)) == int(groupFilter)]
                for dev in targets:
                    try:
                        mac = (dev.addr or "").upper()
                        if not mac:
                            continue
                        if mac not in responders and mac[-6:] not in responders:
                            dev.mark_offline("Missing reply (STATUS)")
                    except Exception:
                        pass

        return updated

    def setNodeGroupId(self, targetDevice: RL_Device, forceSet: bool = False, wait_for_ack: bool = True) -> bool:
        if not getattr(self, "lora", None):
            logger.warning("setNodeGroupId: communicator not ready")
            return False

        self._install_transport_hooks()

        recv3 = _mac_last3_from_hex(targetDevice.addr)
        group_id = int(targetDevice.groupId) & 0xFF
        is_broadcast = recv3 == b"\xFF\xFF\xFF"

        if not is_broadcast:
            targetDevice.ack_clear()

        def _send():
            self.lora.send_set_group(recv3, group_id)

        if not wait_for_ack or is_broadcast:
            _send()
            return True

        events, _ = self._send_and_wait_for_reply(recv3, LP.OPC_SET_GROUP, _send, timeout_s=8.0)
        if not events:
            logger.warning("No ACK_OK for SET_GROUP to %s (timeout)", targetDevice.addr)
            return False

        ev = events[-1]
        ok = int(ev.get("ack_status", 1)) == 0
        if not ok:
            logger.warning(
                "No ACK_OK for SET_GROUP to %s (status=%s, opcode=%s)",
                targetDevice.addr,
                ev.get("ack_status"),
                ev.get("ack_of"),
            )
        return ok

    def forceGroups(self, args=None, sanityCheck: bool = True):
        logger.debug("Forcing all known devices to their stored groups.")
        num_groups = len(rl_grouplist)

        for device in rl_devicelist:
            if sanityCheck is True and device.groupId >= num_groups:
                device.groupId = 0
            self.setNodeGroupId(device, forceSet=True)
            #time.sleep(0.2)

    def _require_lora(self, context: str):
        if getattr(self, "lora", None):
            return True
        logger.warning("%s: communicator not ready", context)
        return False

    @staticmethod
    def _coerce_control_values(flags, preset_id, brightness, *, fallback: RL_Device | None = None):
        if fallback is not None:
            flags = fallback.flags if flags is None else flags
            preset_id = fallback.presetId if preset_id is None else preset_id
            brightness = fallback.brightness if brightness is None else brightness
        return int(flags) & 0xFF, int(preset_id) & 0xFF, int(brightness) & 0xFF

    @staticmethod
    def _update_group_control_cache(group_id: int, flags: int, preset_id: int, brightness: int) -> None:
        for device in rl_devicelist:
            try:
                if (int(getattr(device, "groupId", 0)) & 0xFF) != group_id:
                    continue
                device.flags = flags
                device.presetId = preset_id
                device.brightness = brightness
            except Exception:
                continue

    def sendRaceLink(self, targetDevice, flags=None, presetId=None, brightness=None):
        """Send CONTROL to a single node (receiver = last3 of targetDevice.addr)."""
        if not self._require_lora("sendRaceLink"):
            return
        recv3 = _mac_last3_from_hex(targetDevice.addr)
        groupId = int(targetDevice.groupId) & 0xFF

        f, p, b = self._coerce_control_values(flags, presetId, brightness, fallback=targetDevice)

        self.lora.send_control(recv3=recv3, group_id=groupId, flags=f, preset_id=p, brightness=b)

        targetDevice.flags = f
        targetDevice.presetId = p
        targetDevice.brightness = b
        logger.debug(
            "RL: Updated Device %s: flags=0x%02X presetId=%d brightness=%d",
            targetDevice.addr,
            targetDevice.flags,
            targetDevice.presetId,
            targetDevice.brightness,
        )

    def sendGroupControl(self, gcGroupId, gcFlags, gcPresetId, gcBrightness):
        """Broadcast CONTROL to a group (receiver=FFFFFF); update local cache for group devices."""
        if not self._require_lora("sendGroupControl"):
            return

        groupId = int(gcGroupId) & 0xFF
        f, p, b = self._coerce_control_values(gcFlags, gcPresetId, gcBrightness)

        self._update_group_control_cache(groupId, f, p, b)

        self.lora.send_control(
            recv3=b"\xFF\xFF\xFF",
            group_id=groupId,
            flags=f,
            preset_id=p,
            brightness=b,
        )

    def sendWledControl(self, *, targetDevice=None, targetGroup=None, params=None):
        if params is None:
            params = {}
        preset_id = int(params.get("presetId", 1))
        brightness = int(params.get("brightness", 0))
        flags = (RL_FLAG_POWER_ON if brightness > 0 else 0) | RL_FLAG_HAS_BRI

        if targetGroup is not None:
            self.sendGroupControl(int(targetGroup), flags, preset_id, brightness)
            return True
        if targetDevice is not None:
            self.sendRaceLink(targetDevice, flags, preset_id, brightness)
            return True
        return False

    def sendStartblockConfig(self, *, targetDevice=None, targetGroup=None, params=None):
        if targetGroup is not None:
            return False
        if not targetDevice or not self._require_lora("sendStartblockConfig"):
            return False
        if params is None:
            params = {}
        slots = int(params.get("startblock_slots", 1))
        first_slot = int(params.get("startblock_first_slot", 1))
        recv3 = _mac_last3_from_hex(targetDevice.addr)
        if not recv3:
            return False
        ok_slots = self.sendConfig(option=0x8C, data0=slots, recv3=recv3, wait_for_ack=True)
        if not ok_slots:
            return False
        ok_first = self.sendConfig(option=0x8D, data0=first_slot, recv3=recv3, wait_for_ack=True)
        if not ok_first:
            return False
        targetDevice.specials["startblock_slots"] = slots & 0xFF
        targetDevice.specials["startblock_first_slot"] = first_slot & 0xFF
        try:
            self.save_to_db({"manual": True})
        except Exception:
            pass
        return True

    def _is_startblock_device(self, dev: RL_Device) -> bool:
        """True, wenn der Device-Type die STARTBLOCK Capability hat."""
        try:
            type_id = getattr(dev, "caps", getattr(dev, "dev_type", 0))
            info = get_dev_type_info(type_id)
            return bool(info.get("STARTBLOCK"))
        except Exception:
            return False

    def _iter_startblock_devices(self, *, targetDevice=None, targetGroup=None) -> list[RL_Device]:
        """
        Liefert die Startblock-Geräte, die angesprochen werden sollen:
        - wenn targetDevice gesetzt: genau dieses (wenn STARTBLOCK)
        - wenn targetGroup gesetzt: alle STARTBLOCK-Geräte dieser Gruppe
        - sonst: alle STARTBLOCK-Geräte
        """
        if targetDevice is not None:
            return [targetDevice] if self._is_startblock_device(targetDevice) else []

        if targetGroup is not None:
            gid = int(targetGroup)
            return [
                dev
                for dev in rl_devicelist
                if self._is_startblock_device(dev) and int(getattr(dev, "groupId", 0) or 0) == gid
            ]

        return [dev for dev in rl_devicelist if self._is_startblock_device(dev)]

    def get_current_heat_slot_list(self):
        """
        Returns: [(slot_0based, pilot_callsign, racechannel), ...] sorted by slot.
        racechannel ist z.B. "R3" oder "--" wenn Band/Channel nicht gesetzt ist.
        """

        # 1) Racechannels aus RHAPI frequencyset (so wie du es gezeigt hast)
        freq = json.loads(self._rhapi.race.frequencyset.frequencies)
        bands = freq["b"]
        channels = freq["c"]
        racechannels = [
            "--" if band is None else f"{band}{channels[i]}"
            for i, band in enumerate(bands)
        ]

        # 2) Slot-Belegung aus aktuellem Heat
        ctx = self._rhapi._racecontext
        rhdata = ctx.rhdata
        race = ctx.race
        heat_nodes = rhdata.get_heatNodes_by_heat(race.current_heat) or []

        callsign_by_slot = {}
        for hn in heat_nodes:
            slot = int(getattr(hn, "node_index"))
            pid = getattr(hn, "pilot_id", None)
            p = rhdata.get_pilot(pid) if pid else None
            callsign_by_slot[slot] = (p.callsign if p else "")

        # 3) Ergebnisliste nach Slot sortiert (0..N-1)
        n = min(len(racechannels), 8)  # Startblock max 8
        return [(i, callsign_by_slot.get(i, ""), racechannels[i]) for i in range(n)]

    def sendStartblockControl(self, *, targetDevice=None, targetGroup=None, params=None):
        if not self._require_lora("sendStartblockControl"):
            return {}
        if params is None:
            params = {}

        # Default: immer Current Heat nutzen (UI sendet den Key oft gar nicht)
        use_heat = params.get("startblock_use_current_heat")
        if use_heat is None:
            use_heat = True

        # 1) Slots-Daten holen
        if use_heat:
            slot_list = self.get_current_heat_slot_list()  # [(slot0, callsign, racechannel), ...]
        else:
            # Optional: falls UI manuelle Daten liefert (wenn du das nutzt)
            slot_list = params.get("startblock_slot_list") or []
            slot_list = self._normalize_startblock_slot_list(slot_list)

        # Immer 8 Slots (0..7) bereitstellen
        slot_map = {int(s): (cs or "", rc or "--") for (s, cs, rc) in slot_list}
        slots_0based = [(i, *slot_map.get(i, ("", "--"))) for i in range(8)]

        # 3) Wenn targetGroup gesetzt: Gruppe ist Ziel, keine Device-Details prüfen
        if targetGroup is not None:
            gid = int(targetGroup)
            sent = []
            # 6) Payloads für alle Slots nacheinander broadcasten
            for slot0, cs, rc in slots_0based:
                payload = build_startblock_payload_v1(slot0 + 1, rc, cs)
                sent.append({
                    "slot": slot0 + 1,
                    "result": self.sendStream(payload, groupId=gid)
                })
            return {"mode": "group", "groupId": gid, "sent": sent}

        # 2) targetDevice explizit -> STARTBLOCK prüfen
        if targetDevice is not None:
            if not self._is_startblock_device(targetDevice):
                return {"error": "targetDevice has no STARTBLOCK capability"}
            devices = [targetDevice]
        else:
            # 4) targetDevice None und targetGroup None -> passende Devices suchen
            devices = self._iter_startblock_devices(targetDevice=None, targetGroup=None)

        if not devices:
            return {"mode": "unicast", "sent": []}

        # 5) startblock_slots / startblock_first_slot lesen und Slot->Device Mapping bauen
        slot_to_dev = {}  # slot_1based -> device
        dev_ranges = []
        for dev in devices:
            try:
                startblock_slots = int(dev.specials["startblock_slots"])
                startblock_first_slot = int(dev.specials["startblock_first_slot"])
            except Exception:
                # Fallback: wenn Specials fehlen, als "vollständig" behandeln
                startblock_slots = 8
                startblock_first_slot = 1

            # clamp
            startblock_slots = max(1, min(8, startblock_slots))
            startblock_first_slot = max(1, min(8, startblock_first_slot))
            last = min(8, startblock_first_slot + startblock_slots - 1)

            dev_ranges.append((dev, startblock_first_slot, last))

            for s in range(startblock_first_slot, last + 1):
                # Falls Überschneidung: erstes Gerät gewinnt (kannst du bei Bedarf anders lösen)
                slot_to_dev.setdefault(s, dev)

        # 6) Unicast: pro Slot das passende Gerät adressieren (bis zu 8 Streams)
        sent = []
        for slot0, cs, rc in slots_0based:
            slot1 = slot0 + 1
            dev = slot_to_dev.get(slot1)
            if dev is None:
                continue

            payload = build_startblock_payload_v1(slot1, rc, cs)
            sent.append({
                "slot": slot1,
                "device": getattr(dev, "deviceId", getattr(dev, "mac", None)),
                "result": self.sendStream(payload, device=dev),
            })

        return {
            "mode": "unicast",
            "devices": [
                {
                    "device": getattr(d, "deviceId", getattr(d, "mac", None)),
                    "first": a,
                    "last": b
                } for (d, a, b) in dev_ranges
            ],
            "sent": sent
        }

    def _normalize_startblock_slot_list(self, slot_list):
        """
        Akzeptiert grob:
        - [(slot0, callsign, racechannel), ...]
        - [{"slot":0,"callsign":"...","racechannel":"R3"}, ...]
        """
        out = []
        for item in (slot_list or []):
            if isinstance(item, (list, tuple)) and len(item) >= 3:
                out.append((int(item[0]), str(item[1] or ""), str(item[2] or "--")))
            elif isinstance(item, dict):
                s = int(item.get("slot", 0))
                cs = str(item.get("callsign", "") or "")
                rc = str(item.get("racechannel", "--") or "--")
                out.append((s, cs, rc))
        return out

    def _send_and_wait_for_reply(
        self,
        recv3: bytes,
        opcode7: int,
        send_fn,
        timeout_s: float = 8.0,
    ) -> tuple[list[dict], bool]:
        """Send a packet and block until a matching reply/ACK arrives or RX window closes."""
        if not getattr(self, "lora", None):
            return [], False

        self._install_transport_hooks()

        opcode7 = int(opcode7) & 0x7F
        recv3_b = bytes(recv3 or b"")
        sender_filter = recv3_b if recv3_b and recv3_b != b"\xFF\xFF\xFF" else None
        sender_filter_hex = sender_filter.hex().upper() if sender_filter else ""
        sender_dev = self.getDeviceFromAddress(sender_filter_hex) if sender_filter_hex else None

        try:
            rule = LPA.find_rule(opcode7)
        except Exception:
            rule = None

        policy = int(getattr(rule, "policy", getattr(LPA, "RESP_NONE", 0))) if rule else int(getattr(LPA, "RESP_NONE", 0))
        if policy == int(getattr(LPA, "RESP_NONE", 0)):
            send_fn()
            return [], False

        rsp_opc = int(getattr(rule, "rsp_opcode7", -1)) & 0x7F if rule else -1

        def _collect(ev: dict) -> bool:
            try:
                sender3 = ev.get("sender3")
                if sender_filter is not None:
                    if not isinstance(sender3, (bytes, bytearray)):
                        return False
                    if bytes(sender3) != sender_filter:
                        return False

                opc = int(ev.get("opc", -1))
                if policy == int(getattr(LPA, "RESP_ACK", 1)):
                    if opc == int(LP.OPC_ACK) and int(ev.get("ack_of", -1)) == opcode7:
                        if sender_dev:
                            sender_dev.mark_online()
                        return True
                elif policy == int(getattr(LPA, "RESP_SPECIFIC", 2)):
                    if opc == rsp_opc:
                        if sender_dev:
                            sender_dev.mark_online()
                        return True
            except Exception:
                return False
            return False

        collected, got_closed = self._wait_rx_window(send_fn, collect_pred=_collect, fail_safe_s=timeout_s)
        return collected, got_closed

    def sendConfig(
        self,
        option,
        data0=0,
        data1=0,
        data2=0,
        data3=0,
        recv3=b"\xFF\xFF\xFF",
        wait_for_ack: bool = False,
        timeout_s: float = 6.0,
    ):
        if not getattr(self, "lora", None):
            logger.warning("sendConfig: communicator not ready")
            return False if wait_for_ack else None
        recv3_hex = recv3.hex().upper() if isinstance(recv3, (bytes, bytearray)) else ""
        dev = None
        if recv3_hex and recv3_hex != "FFFFFF":
            self._pending_config[recv3_hex] = {
                "option": int(option) & 0xFF,
                "data0": int(data0) & 0xFF,
            }
            dev = self.getDeviceFromAddress(recv3_hex)
            if dev and wait_for_ack:
                dev.ack_clear()

        def _send():
            self.lora.send_config(
                recv3=recv3,
                option=int(option) & 0xFF,
                data0=int(data0) & 0xFF,
                data1=int(data1) & 0xFF,
                data2=int(data2) & 0xFF,
                data3=int(data3) & 0xFF,
            )

        if wait_for_ack:
            if not dev:
                _send()
                return False
            events, _ = self._send_and_wait_for_reply(recv3, LP.OPC_CONFIG, _send, timeout_s=timeout_s)
            if not events:
                return False
            ev = events[-1]
            return bool(int(ev.get("ack_status", 1)) == 0)
        _send()
        return True

    def _apply_config_update(self, dev: RL_Device, option: int, data0: int) -> None:
        bit_map = {
            0x01: 0,
            0x03: 1,
            0x04: 2,
        }
        bit = bit_map.get(int(option))
        if bit is None:
            return
        mask = 1 << bit
        if int(data0):
            dev.configByte = int(dev.configByte) | mask
        else:
            dev.configByte = int(dev.configByte) & (~mask & 0xFF)

    def sendSync(self, ts24, brightness, recv3=b"\xFF\xFF\xFF"):
        if not getattr(self, "lora", None):
            logger.warning("sendSync: communicator not ready")
            return
        self.lora.send_sync(recv3=recv3, ts24=int(ts24) & 0xFFFFFF, brightness=int(brightness) & 0xFF)

    @staticmethod
    def _stream_ctrl(start: bool, stop: bool, packets_left: int) -> int:
        """Build STREAM ctrl byte: bit7=start, bit6=stop, bits0-5=packets_left (0-63)."""
        ctrl = (0x80 if start else 0x00) | (0x40 if stop else 0x00)
        return ctrl | (int(packets_left) & 0x3F)

    def sendStream(
        self,
        payload: bytes,
        groupId: int | None = None,
        device: RL_Device | None = None,
        retries: int = 2,
        timeout_s: float = 8.0,
    ) -> dict[str, int]:
        """Send up to 128 bytes as STREAM payload to the master for downstream splitting."""
        if not getattr(self, "lora", None):
            logger.warning("sendStream: communicator not ready")
            return {}

        self._install_transport_hooks()

        data = bytes(payload or b"")
        if len(data) > 128:
            raise ValueError("payload too large (max 128 bytes)")

        if device is None and groupId is None:
            raise ValueError("sendStream requires groupId or device")

        total_packets = max(1, (len(data) + 7) // 8)
        start = True
        stop = total_packets == 1
        packets_left = 0 if stop else total_packets
        ctrl = self._stream_ctrl(start, stop, packets_left)

        if device is None:
            targets = [
                dev for dev in rl_devicelist if int(getattr(dev, "groupId", 0) or 0) == int(groupId)
            ]
        else:
            targets = [device]

        target_last3 = {_mac_last3_from_hex(dev.addr) for dev in targets if dev and dev.addr}
        target_last3.discard(b"\xFF\xFF\xFF")
        expected = len(target_last3)
        if expected == 0:
            return {"expected": 0, "acked": 0}

        recv3 = b"\xFF\xFF\xFF" if device is None else _mac_last3_from_hex(device.addr)
        if recv3 == b"\xFF\xFF\xFF" and device is not None:
            return {"expected": expected, "acked": 0}

        try:
            self.lora.drain_events(0.0)
        except Exception:
            pass

        acked = set()

        def _collect(ev: dict) -> bool:
            try:
                if ev.get("opc") != LP.OPC_ACK:
                    return False
                if int(ev.get("ack_of", -1)) != int(LP.OPC_STREAM):
                    return False
                sender3 = ev.get("sender3")
                if not isinstance(sender3, (bytes, bytearray)):
                    return False
                sender3_b = bytes(sender3)
                if sender3_b not in target_last3:
                    return False
                acked.add(sender3_b)
                return True
            except Exception:
                return False

        for attempt in range(max(0, int(retries)) + 1):
            self._wait_rx_window(
                lambda: self.lora.send_stream(recv3=recv3, ctrl=ctrl, data=data),
                collect_pred=_collect,
                fail_safe_s=timeout_s,
            )
            if len(acked) >= expected:
                break
            if attempt < int(retries):
                time.sleep(0.1)

        return {"expected": expected, "acked": len(acked)}

    def _wait_rx_window(self, send_fn, collect_pred=None, fail_safe_s: float = 8.0):
        if not getattr(self, "lora", None):
            return [], False

        lora = self.lora
        collected = []
        got_closed = False

        if hasattr(lora, "add_listener") and hasattr(lora, "remove_listener"):
            import threading

            closed_ev = threading.Event()

            def _cb(ev: dict):
                nonlocal got_closed
                try:
                    if not isinstance(ev, dict):
                        return
                    if ev.get("type") == EV_RX_WINDOW_CLOSED:
                        got_closed = True
                        closed_ev.set()
                        return
                    if collect_pred and collect_pred(ev):
                        collected.append(ev)
                except Exception:
                    pass

            lora.add_listener(_cb)
            try:
                send_fn()
                closed_ev.wait(timeout=float(fail_safe_s))
            finally:
                try:
                    lora.remove_listener(_cb)
                except Exception:
                    pass
            return collected, got_closed

        send_fn()
        t_end = time.time() + float(fail_safe_s)
        while time.time() < t_end:
            for ev in lora.drain_events(timeout_s=0.1):
                if ev.get("type") == EV_RX_WINDOW_CLOSED:
                    got_closed = True
                    return collected, got_closed
                if collect_pred and collect_pred(ev):
                    collected.append(ev)
        return collected, got_closed

    def _opcode_name(self, opcode7: int) -> str:
        try:
            rule = LPA.find_rule(int(opcode7) & 0x7F)
        except Exception:
            rule = None
        if rule and getattr(rule, "name", None):
            return str(rule.name)
        return f"0x{int(opcode7) & 0x7F:02X}"

    def _log_lora_reply(self, ev: dict) -> None:
        try:
            opc = int(ev.get("opc", -1)) & 0x7F
        except Exception:
            return

        sender3_hex = self._to_hex_str(ev.get("sender3")) or "??????"

        if opc == int(LP.OPC_ACK):
            ack_of = ev.get("ack_of")
            ack_status = ev.get("ack_status")
            ack_seq = ev.get("ack_seq")
            if ack_of is None or ack_status is None:
                return
            ack_name = self._opcode_name(int(ack_of))
            logger.debug(
                "ACK from %s: ack_of=%s (%s) status=%s seq=%s",
                sender3_hex,
                int(ack_of),
                ack_name,
                int(ack_status),
                ack_seq,
            )
            return

        if opc == int(LP.OPC_STATUS) and ev.get("reply") == "STATUS_REPLY":
            logger.debug(
                "STATUS from %s: flags=0x%02X cfg=0x%02X preset=%s bri=%s vbat=%s rssi=%s snr=%s host_rssi=%s host_snr=%s",
                sender3_hex,
                int(ev.get("flags", 0) or 0) & 0xFF,
                int(ev.get("configByte", 0) or 0) & 0xFF,
                ev.get("presetId"),
                ev.get("brightness"),
                ev.get("vbat_mV"),
                ev.get("node_rssi"),
                ev.get("node_snr"),
                ev.get("host_rssi"),
                ev.get("host_snr"),
            )
            return

        if opc == int(LP.OPC_DEVICES) and ev.get("reply") == "IDENTIFY_REPLY":
            mac6 = ev.get("mac6")
            mac12 = bytes(mac6).hex().upper() if isinstance(mac6, (bytes, bytearray)) and len(mac6) == 6 else None
            dev_type = ev.get("caps")
            dtype_name = get_dev_type_info(dev_type).get("name")
            logger.debug(
                "IDENTIFY from %s: mac=%s group=%s ver=%s dev_type=%s (%s) host_rssi=%s host_snr=%s",
                sender3_hex,
                mac12 or sender3_hex,
                ev.get("groupId"),
                ev.get("version"),
                dev_type,
                dtype_name,
                ev.get("host_rssi"),
                ev.get("host_snr"),
            )
            return

        if ev.get("reply"):
            logger.debug("RX %s from %s (opc=0x%02X)", ev.get("reply"), sender3_hex, opc)

    def _log_rx_window_event(self, ev: dict) -> None:
        t = ev.get("type")
        if getattr(self, "lora", None):
            state = int(ev.get("rx_windows", getattr(self.lora, "rx_window_state", 0)) or 0)
        else:
            state = int(ev.get("rx_windows", 0) or 0)
        if t == EV_RX_WINDOW_OPEN:
            logger.debug("RX window OPEN: state=%s min_ms=%s", state, ev.get("window_ms"))
        elif t == EV_RX_WINDOW_CLOSED:
            logger.debug("RX window CLOSED: state=%s delta=%s", state, ev.get("rx_count_delta"))

    def _handle_ack_event(self, ev: dict) -> None:
        try:
            sender3_hex = self._to_hex_str(ev.get("sender3"))
            dev = self.getDeviceFromAddress(sender3_hex) if sender3_hex else None
            if not dev:
                return

            ack_of = ev.get("ack_of")
            ack_status = ev.get("ack_status")
            ack_seq = ev.get("ack_seq")
            host_rssi = ev.get("host_rssi")
            host_snr = ev.get("host_snr")

            if ack_of is None or ack_status is None:
                return

            dev.ack_update(int(ack_of), int(ack_status), ack_seq, host_rssi, host_snr)

            if int(ack_of) == int(LP.OPC_CONFIG) and int(ack_status) == 0:
                pending = self._pending_config.pop(sender3_hex, None)
                if pending:
                    self._apply_config_update(dev, pending.get("option", 0), pending.get("data0", 0))

        except Exception:
            logger.exception("ACK handling failed")

    def _install_transport_hooks(self) -> None:
        if self._transport_hooks_installed:
            return
        lora = getattr(self, "lora", None)
        if not lora:
            return

        try:
            if hasattr(lora, "add_listener"):
                lora.add_listener(self._on_transport_event_gc)
            else:
                prev = getattr(lora, "on_event", None)

                def _mux(ev):
                    try:
                        self._on_transport_event_gc(ev)
                    except Exception:
                        pass
                    if prev:
                        try:
                            prev(ev)
                        except Exception:
                            pass

                lora.on_event = _mux
        except Exception:
            logger.exception("RaceLink: failed to install transport RX listener")

        try:
            if hasattr(lora, "add_tx_listener"):
                lora.add_tx_listener(self._on_transport_tx)
        except Exception:
            logger.exception("RaceLink: failed to install transport TX listener")

        self._transport_hooks_installed = True

    def _on_transport_tx(self, ev: dict) -> None:
        try:
            if not ev or ev.get("type") != "TX_M2N":
                return
            recv3 = ev.get("recv3")
            if not isinstance(recv3, (bytes, bytearray)) or len(recv3) != 3:
                return
            recv3_b = bytes(recv3)

            if recv3_b == b"\xFF\xFF\xFF":
                return

            opcode7 = int(ev.get("opc", -1)) & 0x7F
            try:
                rule = LPA.find_rule(opcode7)
            except Exception:
                rule = None
            if not rule:
                return

            if int(getattr(rule, "req_dir", getattr(LPA, "DIR_M2N", 0))) != int(getattr(LPA, "DIR_M2N", 0)):
                return

            policy = int(getattr(rule, "policy", getattr(LPA, "RESP_NONE", 0)))
            if policy == int(getattr(LPA, "RESP_NONE", 0)):
                return

            dev = self.getDeviceFromAddress(recv3_b.hex().upper())
            if not dev:
                return

            self._pending_expect = {
                "dev": dev,
                "rule": rule,
                "opcode7": opcode7,
                "sender_last3": (dev.addr or "").upper()[-6:],
                "ts": time.time(),
            }
        except Exception:
            logger.exception("RaceLink: TX hook failed")

    def _on_transport_event_gc(self, ev: dict) -> None:
        try:
            if not isinstance(ev, dict):
                return

            t = ev.get("type")

            if t == EV_ERROR:
                reason = str(ev.get("data") or "unknown error")
                self.ready = False
                now = time.time()
                if (now - self._last_error_notify_ts) > 2:
                    self._last_error_notify_ts = now
                    try:
                        self._rhapi.ui.message_notify(
                            self._rhapi.__("RaceLink Communicator disconnected: {}").format(reason)
                        )
                    except Exception:
                        logger.exception("RaceLink: failed to notify UI about disconnect")
                self._schedule_reconnect(reason)
                return

            if t in (EV_RX_WINDOW_OPEN, EV_RX_WINDOW_CLOSED):
                self._log_rx_window_event(ev)
                if t == EV_RX_WINDOW_CLOSED:
                    self._pending_window_closed(ev)
                return

            opc = ev.get("opc")
            if opc is None:
                return

            self._log_lora_reply(ev)

            if int(opc) == int(LP.OPC_ACK):
                self._handle_ack_event(ev)
            elif int(opc) == int(LP.OPC_STATUS) and ev.get("reply") == "STATUS_REPLY":
                sender3_hex = self._to_hex_str(ev.get("sender3"))
                dev = self.getDeviceFromAddress(sender3_hex) if sender3_hex else None
                if dev:
                    dev.update_from_status(
                        ev.get("flags"),
                        ev.get("configByte"),
                        ev.get("presetId"),
                        ev.get("brightness"),
                        ev.get("vbat_mV"),
                        ev.get("node_rssi"),
                        ev.get("node_snr"),
                        ev.get("host_rssi"),
                        ev.get("host_snr"),
                    )
            elif int(opc) == int(LP.OPC_DEVICES) and ev.get("reply") == "IDENTIFY_REPLY":

                mac6 = ev.get("mac6")
                if isinstance(mac6, (bytes, bytearray)) and len(mac6) == 6:
                    mac12 = bytes(mac6).hex().upper()
                    dev = self.getDeviceFromAddress(mac12)
                    if not dev:
                        dev_type = ev.get("caps", 0)
                        dev = create_device(addr=mac12, dev_type=int(dev_type or 0), name=f"WLED {mac12}")
                        rl_devicelist.append(dev)
                        try:
                            if hasattr(self, "createUiDevList"):
                                self.uiDeviceList = self.createUiDevList()
                        except Exception:
                            pass

                    dev.update_from_identify(
                        ev.get("version"),
                        ev.get("caps"),
                        ev.get("groupId"),
                        mac6,
                        ev.get("host_rssi"),
                        ev.get("host_snr"),
                    )

            self._pending_try_match(ev)

        except Exception:
            logger.exception("RaceLink: RX hook failed")

    def _schedule_reconnect(self, reason: str) -> None:
        now = time.time()
        if self._reconnect_in_progress or (now - self._last_reconnect_ts) < 5:
            return
        self._last_reconnect_ts = now
        self._reconnect_in_progress = True

        def _reconnect():
            try:
                logger.warning("RaceLink: attempting LoRaUSB reconnect after error: %s", reason)
                try:
                    if self.lora:
                        self.lora.close()
                except Exception:
                    pass
                self.lora = None
                self.discoverPort({})
            finally:
                self._reconnect_in_progress = False

        threading.Thread(target=_reconnect, daemon=True).start()

    def _pending_try_match(self, ev: dict) -> None:
        p = self._pending_expect
        if not p:
            return

        try:
            sender3_hex = self._to_hex_str(ev.get("sender3")).upper()
            if not sender3_hex:
                return
            if sender3_hex != (p.get("sender_last3") or "").upper():
                return

            rule = p.get("rule")
            opcode7 = int(p.get("opcode7", -1)) & 0x7F
            policy = int(getattr(rule, "policy", getattr(LPA, "RESP_NONE", 0)))

            if policy == int(getattr(LPA, "RESP_ACK", 1)):
                if int(ev.get("opc", -1)) == int(LP.OPC_ACK) and int(ev.get("ack_of", -2)) == opcode7:
                    dev = p.get("dev")
                    if dev:
                        dev.mark_online()
                    self._pending_expect = None
            elif policy == int(getattr(LPA, "RESP_SPECIFIC", 2)):
                rsp_opc = int(getattr(rule, "rsp_opcode7", -1)) & 0x7F
                if int(ev.get("opc", -1)) == rsp_opc:
                    dev = p.get("dev")
                    if dev:
                        dev.mark_online()
                    self._pending_expect = None
        except Exception:
            logger.exception("RaceLink: pending match failed")

    def _pending_window_closed(self, ev: dict) -> None:
        p = self._pending_expect
        if not p:
            return

        try:
            dev = p.get("dev")
            rule = p.get("rule")
            opcode7 = int(p.get("opcode7", -1)) & 0x7F
            name = getattr(rule, "name", f"opc=0x{opcode7:02X}")
            if dev:
                dev.mark_offline(f"Missing reply ({name})")
        finally:
            self._pending_expect = None

    def getDeviceFromAddress(self, addr: str) -> Optional[RL_Device]:
        """MAC als String ohne Trennzeichen: 12 (voll) oder 6 (last3)."""
        if not addr:
            return None
        s = str(addr).strip().upper()
        if len(s) == 12:
            for d in rl_devicelist:
                if (d.addr or "").upper() == s:
                    return d
            return None
        if len(s) == 6:
            for d in rl_devicelist:
                if (d.addr or "").upper().endswith(s):
                    return d
            return None
        return None

    @staticmethod
    def _to_hex_str(addr: Union[str, bytes, bytearray, None]) -> str:
        if addr is None:
            return ""
        if isinstance(addr, (bytes, bytearray)):
            return bytes(addr).hex().upper()
        return str(addr).strip().replace(":", "").replace(" ", "").upper()
