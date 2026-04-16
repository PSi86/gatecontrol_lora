from __future__ import annotations

import logging
from typing import Optional, Union

try:
    from racelink.domain import (
        RL_Device,
        RL_DeviceGroup,
        RL_FLAG_HAS_BRI,
        RL_FLAG_POWER_ON,
        build_specials_state,
        create_device,
    )
    from racelink.services import (
        ConfigService,
        ControlService,
        DiscoveryService,
        GatewayService,
        StartblockService,
        StatusService,
        StreamService,
        SyncService,
    )
    from racelink.state import get_runtime_state_repository
    from racelink.state.persistence import dump_records, load_records
    from racelink.transport import GatewaySerialTransport, LP, mac_last3_from_hex
except ImportError:  # pragma: no cover - compatibility path for package-style plugin loading
    from .racelink.domain import (
        RL_Device,
        RL_DeviceGroup,
        RL_FLAG_HAS_BRI,
        RL_FLAG_POWER_ON,
        build_specials_state,
        create_device,
    )
    from .racelink.services import (
        ConfigService,
        ControlService,
        DiscoveryService,
        GatewayService,
        StartblockService,
        StatusService,
        StreamService,
        SyncService,
    )
    from .racelink.state import get_runtime_state_repository
    from .racelink.state.persistence import dump_records, load_records
    from .racelink.transport import GatewaySerialTransport, LP, mac_last3_from_hex

logger = logging.getLogger(__name__)


class RaceLink_Host:
    """Host controller coordinating runtime state, transport, and core services."""

    def __init__(self, rhapi, name, label, state_repository=None):
        self._rhapi = rhapi
        self.name = name
        self.label = label
        self.state_repository = state_repository or get_runtime_state_repository()
        self.transport = None
        self.ready = False
        self.deviceCfgValid = False
        self.groupCfgValid = False

        # Transport-level pending expectation (for online/offline determination).
        self._pending_expect = None  # dict with keys: dev, rule, opcode7, sender_last3, ts

        self._transport_hooks_installed = False
        self._pending_config = {}
        self._reconnect_in_progress = False
        self._last_reconnect_ts = 0.0
        self._last_error_notify_ts = 0.0
        # Basic colors: 1-9; Basic effects: 10-19; Special Effects (WLED only): 20-100
        self.uiEffectList = [
            {"value": "01", "label": "Red"},
            {"value": "02", "label": "Green"},
            {"value": "03", "label": "Blue"},
            {"value": "04", "label": "White"},
            {"value": "05", "label": "Yellow"},
            {"value": "06", "label": "Cyan"},
            {"value": "07", "label": "Magenta"},
            {"value": "10", "label": "Blink Multicolor"},
            {"value": "11", "label": "Pulse White"},
            {"value": "12", "label": "Colorloop"},
            {"value": "13", "label": "Blink RGB"},
            {"value": "20", "label": "WLED Chaser"},
            {"value": "21", "label": "WLED Chaser inverted"},
            {"value": "22", "label": "WLED Rainbow"},
        ]
        self.gateway_service = GatewayService(self)
        self.control_service = ControlService(self, self.gateway_service)
        self.config_service = ConfigService(self, self.gateway_service)
        self.discovery_service = DiscoveryService(self, self.gateway_service)
        self.status_service = StatusService(self, self.gateway_service)
        self.stream_service = StreamService(self, self.gateway_service)
        self.startblock_service = StartblockService(self, self.stream_service)
        self.sync_service = SyncService(self, self.gateway_service)

    def _option(self, key: str, default=None):
        return self._rhapi.db.option(key, default)

    def _option_set(self, key: str, value) -> None:
        self._rhapi.db.option_set(key, value)

    def _translate(self, text: str) -> str:
        return self._rhapi.__(text)

    def _notify(self, message: str) -> None:
        ui = getattr(self._rhapi, "ui", None)
        notify = getattr(ui, "message_notify", None) if ui else None
        if callable(notify):
            notify(message)

    def _broadcast_ui(self, panel: str) -> None:
        ui = getattr(self._rhapi, "ui", None)
        broadcaster = getattr(ui, "broadcast_ui", None) if ui else None
        if callable(broadcaster):
            broadcaster(panel)

    @property
    def device_repository(self):
        return self.state_repository.devices

    @property
    def group_repository(self):
        return self.state_repository.groups

    @property
    def backup_device_repository(self):
        return self.state_repository.backup_devices

    @property
    def backup_group_repository(self):
        return self.state_repository.backup_groups

    def onStartup(self, _args):
        self.load_from_db()
        self.discoverPort({})

    def save_to_db(self, args):
        logger.debug("RL: Writing current states to Database")
        config_str_devices = dump_records(self.device_repository.list())
        self._option_set("rl_device_config", config_str_devices)

        if len(self.group_repository.list()) >= len(self.backup_group_repository.list()):
            config_str_groups = dump_records(self.group_repository.list())
        else:
            config_str_groups = dump_records(self.backup_group_repository.list())
        self._option_set("rl_groups_config", config_str_groups)

    def load_from_db(self):
        logger.debug("RL: Applying config from Database")
        config_str_devices = self._option("rl_device_config", None)
        config_str_groups = self._option("rl_groups_config", None)

        if config_str_devices is None:
            config_str_devices = dump_records(self.backup_device_repository.list())
            self._option_set("rl_device_config", config_str_devices)

        if config_str_devices == "":
            config_str_devices = dump_records([])
            self._option_set("rl_device_config", config_str_devices)

        config_list_devices = load_records(
            config_str_devices,
            default=[obj.__dict__ for obj in self.backup_device_repository.list()],
        )
        loaded_devices = []

        for device in config_list_devices:
            logger.debug(device)
            try:
                flags = device.get("flags", None)
                preset_id = device.get("presetId", None)

                if flags is None:
                    legacy_state = int(device.get("state", 1) or 0)
                    flags = RL_FLAG_POWER_ON if legacy_state else 0
                    if "brightness" in device:
                        flags |= RL_FLAG_HAS_BRI

                if preset_id is None:
                    preset_id = int(device.get("effect", 1) or 1)

                brightness = int(device.get("brightness", 70) or 0)

                dev_type = device.get("dev_type", None)
                if dev_type is None:
                    dev_type = device.get("device_type", None)
                if dev_type is None:
                    dev_type = device.get("caps", device.get("type", 0))

                special_state = build_specials_state(int(dev_type or 0), device)
                loaded_devices.append(
                    create_device(
                        addr=str(device.get("addr", "")).upper(),
                        dev_type=int(dev_type or 0),
                        name=str(device.get("name", "")),
                        groupId=int(device.get("groupId", 0) or 0),
                        version=int(device.get("version", 0) or 0),
                        caps=int(dev_type or 0),
                        flags=int(flags) & 0xFF,
                        presetId=int(preset_id) & 0xFF,
                        brightness=brightness & 0xFF,
                        specials=special_state,
                    )
                )
            except Exception:
                logger.exception("RL: failed to load device entry from DB: %r", device)
                continue
        self.device_repository.replace_all(loaded_devices)

        if config_str_groups is None or config_str_groups == "":
            config_str_groups = dump_records(self.backup_group_repository.list())
            self._option_set("rl_groups_config", config_str_groups)

        config_list_groups = load_records(
            config_str_groups,
            default=[obj.__dict__ for obj in self.backup_group_repository.list()],
        )
        loaded_groups = []

        for group in config_list_groups:
            logger.debug(group)
            group_dev_type = group.get("dev_type", group.get("device_type", 0))
            loaded_groups.append(RL_DeviceGroup(group["name"], group["static_group"], group_dev_type))

        loaded_groups = [
            group
            for group in loaded_groups
            if str(getattr(group, "name", "")).strip().lower() not in {"unconfigured", "all wled devices"}
        ]

        if not any(str(getattr(group, "name", "")).strip().lower() == "all wled nodes" for group in loaded_groups):
            loaded_groups.append(RL_DeviceGroup("All WLED Nodes", static_group=1, dev_type=0))
        else:
            for group in loaded_groups:
                if str(getattr(group, "name", "")).strip().lower() == "all wled nodes":
                    group.name = "All WLED Nodes"
                    group.static_group = 1
                    group.dev_type = 0
        self.group_repository.replace_all(loaded_groups)

    def discoverPort(self, args):
        """Initialize the active gateway transport."""
        port = self._option("psi_comms_port", None)
        try:
            self._transport_hooks_installed = False
            self.transport = GatewaySerialTransport(port=port, on_event=None)
            ok = self.transport.discover_and_open()
            if ok:
                self.transport.start()
                self.ready = True
                self._install_transport_hooks()
                used = self.transport.port or "unknown"
                mac = getattr(self.transport, "ident_mac", None)
                if mac:
                    logger.info("RaceLink Gateway ready on %s with MAC: %s", used, mac)
                    if "manual" in args:
                        self._notify(self._translate("RaceLink Gateway ready on {} with MAC: {}").format(used, mac))
                return
            self.ready = False
            logger.warning("No RaceLink Gateway module discovered or configured")
            if "manual" in args:
                self._notify(self._translate("No RaceLink Gateway module discovered or configured"))
        except Exception as ex:
            self.ready = False
            logger.error("Gateway transport init failed: %s", ex)
            if "manual" in args:
                self._notify(self._translate("Failed to initialize communicator: {}").format(str(ex)))

    def onRaceStart(self, _args):
        logger.warning("RaceLink Race Start Event")

    def onRaceFinish(self, _args):
        logger.warning("RaceLink Race Finish Event")

    def onRaceStop(self, _args):
        logger.warning("RaceLink Race Stop Event")

    def onSendMessage(self, args):
        logger.warning("Event onSendMessage")

    def getDevices(self, groupFilter=255, targetDevice=None, addToGroup=-1):
        result = self.discovery_service.discover_devices(
            group_filter=groupFilter,
            target_device=targetDevice,
            add_to_group=addToGroup,
        )
        found = int(result.get("found", 0) or 0)
        if hasattr(self, "_rhapi") and hasattr(self._rhapi, "ui"):
            if addToGroup > 0 and addToGroup < 255:
                msg = "Device Discovery finished with {} devices found and added to GroupId: {}".format(found, addToGroup)
            else:
                msg = "Device Discovery finished with {} devices found.".format(found)
            self._notify(msg)
        return found

    def getStatus(self, groupFilter=255, targetDevice=None):
        result = self.status_service.get_status(group_filter=groupFilter, target_device=targetDevice)
        return int(result.get("updated", 0) or 0)

    def setNodeGroupId(self, targetDevice: RL_Device, forceSet: bool = False, wait_for_ack: bool = True) -> bool:
        if not getattr(self, "transport", None):
            logger.warning("setNodeGroupId: communicator not ready")
            return False

        self._install_transport_hooks()

        recv3 = mac_last3_from_hex(targetDevice.addr)
        group_id = int(targetDevice.groupId) & 0xFF
        is_broadcast = recv3 == b"\xFF\xFF\xFF"

        if not is_broadcast:
            targetDevice.ack_clear()

        def _send():
            self.transport.send_set_group(recv3, group_id)

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
        num_groups = len(self.group_repository.list())

        for device in self.device_repository.list():
            if sanityCheck is True and device.groupId >= num_groups:
                device.groupId = 0
            self.setNodeGroupId(device, forceSet=True)

    def _require_transport(self, context: str):
        if getattr(self, "transport", None):
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

    def _update_group_control_cache(self, group_id: int, flags: int, preset_id: int, brightness: int) -> None:
        for device in self.device_repository.list():
            try:
                if (int(getattr(device, "groupId", 0)) & 0xFF) != group_id:
                    continue
                device.flags = flags
                device.presetId = preset_id
                device.brightness = brightness
            except Exception:
                continue

    def sendRaceLink(self, targetDevice, flags=None, presetId=None, brightness=None):
        """Compatibility entrypoint forwarding device control to ControlService."""
        return self.control_service.send_device_control(targetDevice, flags, presetId, brightness)

    def sendGroupControl(self, gcGroupId, gcFlags, gcPresetId, gcBrightness):
        """Compatibility entrypoint forwarding group control to ControlService."""
        return self.control_service.send_group_control(gcGroupId, gcFlags, gcPresetId, gcBrightness)

    def sendWledControl(self, *, targetDevice=None, targetGroup=None, params=None):
        """Compatibility entrypoint forwarding WLED actions to ControlService."""
        return self.control_service.send_wled_control(targetDevice=targetDevice, targetGroup=targetGroup, params=params)

    def sendStartblockConfig(self, *, targetDevice=None, targetGroup=None, params=None):
        """Compatibility entrypoint forwarding startblock config to StartblockService."""
        return self.startblock_service.send_startblock_config(
            target_device=targetDevice,
            target_group=targetGroup,
            params=params,
        )

    def _is_startblock_device(self, dev: RL_Device) -> bool:
        """Compatibility helper kept for legacy callers during controller slimming."""
        return self.startblock_service.is_startblock_device(dev)

    def _iter_startblock_devices(self, *, targetDevice=None, targetGroup=None) -> list[RL_Device]:
        """Compatibility helper kept for legacy callers during controller slimming."""
        return self.startblock_service.iter_startblock_devices(
            target_device=targetDevice,
            target_group=targetGroup,
        )

    def get_current_heat_slot_list(self):
        """Compatibility helper forwarding heat-slot lookup to the active source adapter."""
        return self.startblock_service.get_current_heat_slot_list()

    def sendStartblockControl(self, *, targetDevice=None, targetGroup=None, params=None):
        """Compatibility entrypoint forwarding startblock dispatch to StartblockService."""
        return self.startblock_service.send_startblock_control(
            target_device=targetDevice,
            target_group=targetGroup,
            params=params,
        )

    def _normalize_startblock_slot_list(self, slot_list):
        """Compatibility helper forwarding slot normalization to StartblockService."""
        return self.startblock_service.normalize_slot_list(slot_list)

    def _send_and_wait_for_reply(
        self,
        recv3: bytes,
        opcode7: int,
        send_fn,
        timeout_s: float = 8.0,
    ) -> tuple[list[dict], bool]:
        return self.gateway_service.send_and_wait_for_reply(recv3, opcode7, send_fn, timeout_s=timeout_s)

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
        """Compatibility entrypoint forwarding config writes to ConfigService."""
        return self.config_service.send_config(
            option,
            data0=data0,
            data1=data1,
            data2=data2,
            data3=data3,
            recv3=recv3,
            wait_for_ack=wait_for_ack,
            timeout_s=timeout_s,
        )

    def _apply_config_update(self, dev: RL_Device, option: int, data0: int) -> None:
        """Compatibility hook forwarding ACK-side config updates to ConfigService."""
        return self.config_service.apply_config_update(dev, option, data0)

    def sendSync(self, ts24, brightness, recv3=b"\xFF\xFF\xFF"):
        """Compatibility entrypoint forwarding sync packets to SyncService."""
        return self.sync_service.send_sync(ts24, brightness, recv3=recv3)

    @staticmethod
    def _stream_ctrl(start: bool, stop: bool, packets_left: int) -> int:
        return StreamService.build_ctrl(start, stop, packets_left)

    def sendStream(
        self,
        payload: bytes,
        groupId: int | None = None,
        device: RL_Device | None = None,
        retries: int = 2,
        timeout_s: float = 8.0,
    ) -> dict[str, int]:
        """Compatibility entrypoint forwarding payload streams to StreamService."""
        return self.stream_service.send_stream(payload, groupId=groupId, device=device, retries=retries, timeout_s=timeout_s)

    def _wait_rx_window(self, send_fn, collect_pred=None, fail_safe_s: float = 8.0):
        return self.gateway_service.wait_rx_window(send_fn, collect_pred=collect_pred, fail_safe_s=fail_safe_s)

    def _opcode_name(self, opcode7: int) -> str:
        return self.gateway_service.opcode_name(opcode7)

    def _log_transport_reply(self, ev: dict) -> None:
        return self.gateway_service.log_transport_reply(ev)

    def _log_rx_window_event(self, ev: dict) -> None:
        return self.gateway_service.log_rx_window_event(ev)

    def _handle_ack_event(self, ev: dict) -> None:
        return self.gateway_service.handle_ack_event(ev)

    def _install_transport_hooks(self) -> None:
        return self.gateway_service.install_transport_hooks()

    def _on_transport_tx(self, ev: dict) -> None:
        return self.gateway_service.on_transport_tx(ev)

    def _on_transport_event_gc(self, ev: dict) -> None:
        return self.gateway_service.on_transport_event(ev)

    def _schedule_reconnect(self, reason: str) -> None:
        return self.gateway_service.schedule_reconnect(reason)

    def _pending_try_match(self, ev: dict) -> None:
        return self.gateway_service.pending_try_match(ev)

    def _pending_window_closed(self, ev: dict) -> None:
        return self.gateway_service.pending_window_closed(ev)

    def getDeviceFromAddress(self, addr: str) -> Optional[RL_Device]:
        """MAC as a hex string without separators: 12 chars (full) or 6 chars (last 3 bytes)."""
        if not addr:
            return None
        s = str(addr).strip().upper()
        if len(s) == 12:
            return self.device_repository.get_by_addr(s)
        if len(s) == 6:
            return self.device_repository.get_by_addr(s)
        return None

    @staticmethod
    def _to_hex_str(addr: Union[str, bytes, bytearray, None]) -> str:
        if addr is None:
            return ""
        if isinstance(addr, (bytes, bytearray)):
            return bytes(addr).hex().upper()
        return str(addr).strip().replace(":", "").replace(" ", "").upper()
