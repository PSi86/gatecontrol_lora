from __future__ import annotations

import logging
import re
from typing import Dict, Optional, Tuple, Union

from .core.services.config_service import ConfigService
from .core.services.control_service import ControlService
from .core.repository import InMemoryDeviceRepository, LegacyConfigMigration
from .core.services.device_service import DeviceService
from .core.services.startblock_service import StartblockService
from .infrastructure.lora_transport_adapter import LoRaTransportAdapter
from .ui import RaceLinkUIMixin
from .providers.rotorhazard_provider import RotorHazardRaceProvider
from RHUI import UIFieldSelectOption
from .data import (
    RL_Device,
    RL_DeviceGroup,
    RL_Dev_Type,
    get_dev_type_info,
)

# ---- lora proto registry (auto-generated from lora_proto.h) ----
try:
    from . import lora_proto_auto as LPA
except Exception:
    import lora_proto_auto as LPA

# ---- transport import (tolerant to both package and flat layout) ----
try:
    from .racelink_transport import LP, _mac_last3_from_hex
except Exception:
    from racelink_transport import LP, _mac_last3_from_hex

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
    def __init__(self, rhapi, name, label, repository: InMemoryDeviceRepository | None = None, race_provider=None):
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
        self.repository = repository or InMemoryDeviceRepository()
        self.transport_adapter = LoRaTransportAdapter(
            rhapi=self._rhapi,
            get_device_by_address=self.getDeviceFromAddress,
            on_status_update=self._on_status_update,
            on_identify_update=self._on_identify_update,
            on_disconnect=self._on_transport_disconnect,
            repository=self.repository,
        )
        self.device_service = DeviceService(self.transport_adapter, self.repository, notifier=self)
        self.control_service = ControlService(self.transport_adapter, self.repository)
        self.config_service = ConfigService(self.transport_adapter, device_lookup=self)
        self.race_provider = race_provider or RotorHazardRaceProvider(self._rhapi)
        self.startblock_service = StartblockService(
            self.transport_adapter,
            self.control_service,
            self.race_provider,
            self.save_to_db,
            self.repository,
        )
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
        self._rhapi.db.option_set("rl_device_config", LegacyConfigMigration.dump_devices(self.repository))
        self._rhapi.db.option_set("rl_groups_config", LegacyConfigMigration.dump_groups(self.repository))

    def load_from_db(self):
        logger.debug("RL: Applying config from Database")
        config_str_devices = self._rhapi.db.option("rl_device_config", None)
        config_str_groups = self._rhapi.db.option("rl_groups_config", None)

        if config_str_devices is None:
            config_str_devices = "[]"
            self._rhapi.db.option_set("rl_device_config", config_str_devices)

        if config_str_devices == "":
            config_str_devices = "[]"
            self._rhapi.db.option_set("rl_device_config", config_str_devices)

        LegacyConfigMigration.load_devices_into_repo(config_str_devices, self.repository)

        if config_str_groups is None or config_str_groups == "":
            config_str_groups = str([{"name": "All WLED Nodes", "static_group": 1, "dev_type": 0}])
            self._rhapi.db.option_set("rl_groups_config", config_str_groups)

        LegacyConfigMigration.load_groups_into_repo(config_str_groups, self.repository)

        self.uiDeviceList = self.createUiDevList()
        self.uiGroupList = self.createUiGroupList()
        self.uiDiscoveryGroupList = self.createUiGroupList(True)
        self.register_settings()
        self.register_quickset_ui()
        self.registerActions()
        self._rhapi.ui.broadcast_ui("settings")
        self._rhapi.ui.broadcast_ui("run")

    def discoverPort(self, args):
        self.ready = self.transport_adapter.discover_port(args)
        self.lora = self.transport_adapter.lora

    def onRaceStart(self, _args):
        logger.warning("RaceLink Race Start Event")

    def onRaceFinish(self, _args):
        logger.warning("RaceLink Race Finish Event")

    def onRaceStop(self, _args):
        logger.warning("RaceLink Race Stop Event")

    def onSendMessage(self, args):
        logger.warning("Event onSendMessage")

    def getDevices(self, groupFilter=255, targetDevice=None, addToGroup=-1):
        return self.device_service.discover_devices(groupFilter, targetDevice, addToGroup, self.setNodeGroupId)

    def getStatus(self, groupFilter=255, targetDevice=None):
        return self.device_service.get_status(groupFilter, targetDevice)

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
        num_groups = len(self.repository.all_groups())

        for device in self.repository.all():
            if sanityCheck is True and device.groupId >= num_groups:
                device.groupId = 0
            self.setNodeGroupId(device, forceSet=True)
            #time.sleep(0.2)

    def _require_lora(self, context: str):
        self.lora = self.transport_adapter.lora
        if getattr(self, "lora", None):
            return True
        logger.warning("%s: communicator not ready", context)
        return False



    def sendRaceLink(self, targetDevice, flags=None, presetId=None, brightness=None):
        self.control_service.send_racelink(targetDevice, flags, presetId, brightness)

    def sendGroupControl(self, gcGroupId, gcFlags, gcPresetId, gcBrightness):
        self.control_service.send_group_control(gcGroupId, gcFlags, gcPresetId, gcBrightness)

    def sendWledControl(self, *, targetDevice=None, targetGroup=None, params=None):
        return self.control_service.send_wled_control(target_device=targetDevice, target_group=targetGroup, params=params)

    def sendStartblockConfig(self, *, targetDevice=None, targetGroup=None, params=None):
        return self.startblock_service.send_startblock_config(
            target_device=targetDevice,
            target_group=targetGroup,
            params=params,
            send_config_fn=self.sendConfig,
        )

    def get_current_heat_slot_list(self):
        return self.startblock_service.get_current_heat_slot_list()

    def sendStartblockControl(self, *, targetDevice=None, targetGroup=None, params=None):
        return self.startblock_service.send_startblock_control(
            target_device=targetDevice,
            target_group=targetGroup,
            params=params,
        )

    def _normalize_startblock_slot_list(self, slot_list):
        return self.startblock_service._normalize_startblock_slot_list(slot_list)

    def _send_and_wait_for_reply(
        self,
        recv3: bytes,
        opcode7: int,
        send_fn,
        timeout_s: float = 8.0,
    ) -> tuple[list[dict], bool]:
        return self.transport_adapter.send_and_wait_for_reply(recv3, opcode7, send_fn, timeout_s)

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
        return self.config_service.send_config(option, data0, data1, data2, data3, recv3, wait_for_ack, timeout_s)

    def _apply_config_update(self, dev: RL_Device, option: int, data0: int) -> None:
        self.config_service.apply_config_update(dev, option, data0)

    def sendSync(self, ts24, brightness, recv3=b"\xFF\xFF\xFF"):
        if not getattr(self, "lora", None):
            logger.warning("sendSync: communicator not ready")
            return
        self.lora.send_sync(recv3=recv3, ts24=int(ts24) & 0xFFFFFF, brightness=int(brightness) & 0xFF)


    def sendStream(
        self,
        payload: bytes,
        groupId: int | None = None,
        device: RL_Device | None = None,
        retries: int = 2,
        timeout_s: float = 8.0,
    ) -> dict[str, int]:
        return self.control_service.send_stream(payload, group_id=groupId, device=device, retries=retries, timeout_s=timeout_s)

    def _wait_rx_window(self, send_fn, collect_pred=None, fail_safe_s: float = 8.0):
        return self.transport_adapter.wait_rx_window(send_fn, collect_pred, fail_safe_s)

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

    def _install_transport_hooks(self) -> None:
        self.transport_adapter.install_hooks()

    def _on_status_update(self, ev: dict) -> None:
        sender3_hex = self._to_hex_str(ev.get("sender3"))
        dev = self.getDeviceFromAddress(sender3_hex) if sender3_hex else None
        if not dev:
            return
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

    def _on_identify_update(self, ev: dict, dev: RL_Device) -> None:
        mac6 = ev.get("mac6")
        dev.update_from_identify(
            ev.get("version"),
            ev.get("caps"),
            ev.get("groupId"),
            mac6,
            ev.get("host_rssi"),
            ev.get("host_snr"),
        )

    def _on_transport_disconnect(self) -> None:
        self.ready = False

    def list_devices(self) -> list[RL_Device]:
        return self.repository.all()

    def list_group_objects(self) -> list[RL_DeviceGroup]:
        return self.repository.all_groups()

    def get_group_count(self) -> int:
        return len(self.repository.all_groups())

    def add_group(self, group: RL_DeviceGroup) -> None:
        self.repository.add_group(group)

    def query_devices(self, *, dev_types=None, capabilities=None) -> list[RL_Device]:
        dev_types_set = set(int(d) for d in (dev_types or [])) if dev_types else None
        cap_set = set(capabilities or []) if capabilities else None

        def _matches_device(dev: RL_Device):
            if dev_types_set and int(getattr(dev, "dev_type", 0) or 0) not in dev_types_set:
                return False
            if cap_set:
                caps = set(get_dev_type_info(getattr(dev, "dev_type", 0)).get("caps", []))
                if not cap_set.issubset(caps):
                    return False
            return True

        return [dev for dev in self.repository.all() if _matches_device(dev)]

    def query_groups(self, *, exclude_static: bool = False) -> list[tuple[int, RL_DeviceGroup]]:
        out = []
        for idx, group in enumerate(self.repository.all_groups()):
            if exclude_static and int(getattr(group, "static_group", 0)) == 1:
                continue
            value = 255 if (group.static_group and str(getattr(group, "name", "")) == "All WLED Nodes") else idx
            out.append((value, group))
        return out

    def query_groups_for_devices(self, devices: list[RL_Device], capabilities=None) -> list[tuple[int, str]]:
        cap_set = set(capabilities or []) if capabilities else None
        group_ids = {int(getattr(dev, "groupId", 0) or 0) for dev in devices}
        out = []
        for idx, group in enumerate(self.repository.all_groups()):
            if group.static_group and str(getattr(group, "name", "")) == "All WLED Nodes":
                if cap_set and "WLED" not in cap_set:
                    continue
                if devices:
                    out.append((255, group.name))
                continue
            if idx in group_ids:
                out.append((idx, group.name))
        return out

    def getDeviceFromAddress(self, addr: str) -> Optional[RL_Device]:
        return self.device_service.get_device_from_address(addr)

    def get_device_by_address(self, addr: str) -> Optional[RL_Device]:
        return self.getDeviceFromAddress(addr)

    def notify(self, msg: str) -> None:
        self._rhapi.ui.message_notify(msg)

    @staticmethod
    def _to_hex_str(addr: Union[str, bytes, bytearray, None]) -> str:
        if addr is None:
            return ""
        if isinstance(addr, (bytes, bytearray)):
            return bytes(addr).hex().upper()
        return str(addr).strip().replace(":", "").replace(" ", "").upper()
