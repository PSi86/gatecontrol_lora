from __future__ import annotations

import logging
from typing import Optional, Union

from .core.app import RaceLinkApp
from .core.repository import InMemoryDeviceRepository
from .data import RL_Device
from .infrastructure.lora_transport_adapter import LoRaTransportAdapter

# ---- lora proto registry (auto-generated from lora_proto.h) ----
try:
    from . import lora_proto_auto as LPA
except Exception:
    import lora_proto_auto as LPA

# ---- transport import (tolerant to both package and flat layout) ----
try:
    from .racelink_transport import LP
except Exception:
    from racelink_transport import LP

logger = logging.getLogger(__name__)


class RaceLink_LoRa:
    """RH-facing controller/facade delegating host-agnostic logic to RaceLinkApp."""

    def __init__(self, rhapi, name, label, repository: InMemoryDeviceRepository | None = None, race_provider=None, race_event_port=None):
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

        # No implicit provider fallback exists; host plugin must inject RaceProviderPort.
        if race_provider is None:
            raise ValueError("race_provider is required; the active host plugin must inject a RaceProviderPort.")

        self.app = RaceLinkApp(
            repository=self.repository,
            transport_port=self.transport_adapter,
            race_provider_port=race_provider,
            race_event_port=race_event_port,
            notify_fn=self.notify,
            config_getter=lambda key, default=None: self._rhapi.db.option(key, default),
            config_setter=lambda key, value: self._rhapi.db.option_set(key, value),
        )

        # Backward-compatible attribute access expected by UI/presentation modules.
        self.device_service = self.app.device_service
        self.control_service = self.app.control_service
        self.config_service = self.app.config_service
        self.startblock_service = self.app.startblock_service

        self.uiEffectList = []
        self.host_ui = None

    def bind_host_ui(self, host_ui):
        self.host_ui = host_ui

    def __getattr__(self, item):
        logger.warning("RaceLink_LoRa dynamic attribute fallback hit for '%s'", item)
        host_ui = self.__dict__.get("host_ui")
        if host_ui and hasattr(host_ui, item):
            return getattr(host_ui, item)

        app = self.__dict__.get("app")
        if app and hasattr(app, item):
            return getattr(app, item)
        raise AttributeError(item)

    def on_startup(self, _args):
        self.app.load_from_db()
        self.uiDeviceList = self.create_ui_device_list()
        self.uiGroupList = self.create_ui_group_list()
        self.uiDiscoveryGroupList = self.create_ui_group_list(True)
        self.register_settings_ui()
        self.register_quickset_ui()
        self.register_actions()
        self._rhapi.ui.broadcast_ui("settings")
        self._rhapi.ui.broadcast_ui("run")
        self.discover_port({})

    def discover_port(self, args):
        self.ready = self.transport_adapter.discover_port(args)
        self.lora = self.transport_adapter.lora

    def onStartup(self, _args):
        self.on_startup(_args)

    def discoverPort(self, args):
        self.discover_port(args)

    def create_ui_device_list(self):
        return self.host_ui.create_ui_device_list()

    def create_ui_group_list(self, exclude_static=False):
        return self.host_ui.create_ui_group_list(exclude_static=exclude_static)

    def create_filtered_ui_device_list(self, *, dev_types=None, capabilities=None, output_devices=True, output_groups=True):
        return self.host_ui.create_filtered_ui_device_list(
            dev_types=dev_types,
            capabilities=capabilities,
            output_devices=output_devices,
            output_groups=output_groups,
        )

    def register_settings_ui(self):
        self.host_ui.register_settings_ui()

    def register_quickset_ui(self):
        self.host_ui.register_quickset_ui()

    def register_actions(self, args=None):
        self.host_ui.register_actions(args=args)

    def group_switch_action(self, action, args=None):
        self.host_ui.group_switch_action(action, args=args)

    def node_switch_action(self, action, args=None):
        self.host_ui.node_switch_action(action, args=args)

    def discover_devices_action(self, args):
        self.host_ui.discover_devices_action(args)

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

        if ev.get("reply"):
            logger.debug("RX %s from %s (opc=0x%02X)", ev.get("reply"), sender3_hex, opc)

    def notify(self, msg: str) -> None:
        self._rhapi.ui.message_notify(msg)

    @staticmethod
    def _to_hex_str(addr: Union[str, bytes, bytearray, None]) -> str:
        if addr is None:
            return ""
        if isinstance(addr, (bytes, bytearray)):
            return bytes(addr).hex().upper()
        return str(addr).strip().replace(":", "").replace(" ", "").upper()

    def get_device_by_address(self, addr: str) -> Optional[RL_Device]:
        return self.get_device_from_address(addr)

    def get_device_from_address(self, addr: str) -> Optional[RL_Device]:
        return self.app.getDeviceFromAddress(addr)

    def getDeviceFromAddress(self, addr: str) -> Optional[RL_Device]:
        return self.get_device_from_address(addr)

    def save_to_db(self, args=None):
        self.app.save_to_db(args)

    def load_from_db(self):
        self.app.load_from_db()

    def force_groups(self, args=None, sanity_check: bool = True):
        self.app.forceGroups(args, sanityCheck=sanity_check)

    def forceGroups(self, args=None, sanityCheck: bool = True):
        self.force_groups(args=args, sanity_check=sanityCheck)

    def get_devices(self, group_filter=255, target_device=None, add_to_group=-1):
        return self.app.getDevices(groupFilter=group_filter, targetDevice=target_device, addToGroup=add_to_group)

    def get_status(self, group_filter=255, target_device=None):
        return self.app.getStatus(groupFilter=group_filter, targetDevice=target_device)

    def list_devices(self):
        return self.app.list_devices()

    def list_group_objects(self):
        return self.app.list_group_objects()

    def get_group_count(self):
        return self.app.get_group_count()

    def add_group(self, group):
        self.app.add_group(group)

    def query_devices(self, *, dev_types=None, capabilities=None):
        return self.app.query_devices(dev_types=dev_types, capabilities=capabilities)

    def query_groups(self, *, exclude_static: bool = False):
        return self.app.query_groups(exclude_static=exclude_static)

    def query_groups_for_devices(self, devices, cap_set=None):
        return self.app.query_groups_for_devices(devices, cap_set)

    def send_wled_control(self, *, target_device=None, target_group=None, params=None):
        return self.app.sendWledControl(targetDevice=target_device, targetGroup=target_group, params=params)

    def send_startblock_config(self, *, target_device=None, target_group=None, params=None):
        return self.app.sendStartblockConfig(targetDevice=target_device, targetGroup=target_group, params=params)

    def send_startblock_control(self, *, target_device=None, target_group=None, params=None):
        return self.app.sendStartblockControl(targetDevice=target_device, targetGroup=target_group, params=params)

    def send_group_control(self, group_id, flags, preset_id, brightness):
        return self.app.sendGroupControl(group_id, flags, preset_id, brightness)

    def send_racelink(self, target_device, flags=None, preset_id=None, brightness=None):
        return self.app.sendRaceLink(target_device, flags, preset_id, brightness)

    def send_config(self, option, data0=0, data1=0, data2=0, data3=0, recv3=b"\xFF\xFF\xFF", wait_for_ack=False, timeout_s=6.0):
        return self.app.sendConfig(option, data0, data1, data2, data3, recv3, wait_for_ack, timeout_s)

    def send_stream(self, payload: bytes, group_id: int | None = None, device: RL_Device | None = None, retries: int = 2, timeout_s: float = 8.0):
        return self.app.sendStream(payload, groupId=group_id, device=device, retries=retries, timeout_s=timeout_s)

    def getDevices(self, groupFilter=255, targetDevice=None, addToGroup=-1):
        return self.get_devices(group_filter=groupFilter, target_device=targetDevice, add_to_group=addToGroup)

    def getStatus(self, groupFilter=255, targetDevice=None):
        return self.get_status(group_filter=groupFilter, target_device=targetDevice)

    def sendWledControl(self, *, targetDevice=None, targetGroup=None, params=None):
        return self.send_wled_control(target_device=targetDevice, target_group=targetGroup, params=params)

    def sendStartblockConfig(self, *, targetDevice=None, targetGroup=None, params=None):
        return self.send_startblock_config(target_device=targetDevice, target_group=targetGroup, params=params)

    def sendStartblockControl(self, *, targetDevice=None, targetGroup=None, params=None):
        return self.send_startblock_control(target_device=targetDevice, target_group=targetGroup, params=params)

    def sendGroupControl(self, gcGroupId, gcFlags, gcPresetId, gcBrightness):
        return self.send_group_control(gcGroupId, gcFlags, gcPresetId, gcBrightness)

    def sendRaceLink(self, targetDevice, flags=None, presetId=None, brightness=None):
        return self.send_racelink(targetDevice, flags, presetId, brightness)

    def sendConfig(self, option, data0=0, data1=0, data2=0, data3=0, recv3=b"\xFF\xFF\xFF", wait_for_ack=False, timeout_s=6.0):
        return self.send_config(option, data0, data1, data2, data3, recv3, wait_for_ack, timeout_s)

    def sendStream(self, payload: bytes, groupId: int | None = None, device: RL_Device | None = None, retries: int = 2, timeout_s: float = 8.0):
        return self.send_stream(payload, group_id=groupId, device=device, retries=retries, timeout_s=timeout_s)
