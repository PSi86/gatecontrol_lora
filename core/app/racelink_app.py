from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from ..events import HostRaceEvent, HostRaceEventType
from ..repository import InMemoryDeviceRepository, LegacyConfigMigration
from ..services.config_service import ConfigService
from ..services.control_service import ControlService
from ..services.device_service import DeviceService
from ..services.startblock_service import StartblockService
from ...data import RL_Device, RL_DeviceGroup, get_dev_type_info
from ...racelink_transport import LP, _mac_last3_from_hex

logger = logging.getLogger(__name__)


class RaceLinkApp:
    """Host-agnostic application service orchestrating domain services."""

    def __init__(
        self,
        *,
        repository: InMemoryDeviceRepository,
        transport_port,
        race_provider_port,
        race_event_port=None,
        notify_fn: Callable[[str], None] | None = None,
        config_getter: Callable[[str, object], object] | None = None,
        config_setter: Callable[[str, object], None] | None = None,
    ):
        self.repository = repository
        self.transport_port = transport_port
        self.race_provider_port = race_provider_port
        self.race_event_port = race_event_port
        self._notify_fn = notify_fn
        self._config_getter = config_getter
        self._config_setter = config_setter

        self.device_service = DeviceService(self.transport_port, self.repository, notifier=self)
        self.control_service = ControlService(self.transport_port, self.repository)
        self.config_service = ConfigService(self.transport_port, device_lookup=self)
        self.startblock_service = StartblockService(
            self.transport_port,
            self.control_service,
            self.race_provider_port,
            self.save_to_db,
            self.repository,
        )
        self._event_stream_started = False

    def start_event_stream(self) -> None:
        if self.race_event_port is None or self._event_stream_started:
            return
        self.race_event_port.start(self.on_race_event)
        self._event_stream_started = True

    def stop_event_stream(self) -> None:
        if self.race_event_port is None or not self._event_stream_started:
            return
        self.race_event_port.stop()
        self._event_stream_started = False

    def on_race_event(self, event: HostRaceEvent) -> None:
        event_type = getattr(event, "type", None)
        payload = getattr(event, "payload", None)
        if event_type == HostRaceEventType.RACE_STARTED:
            self.on_race_start(payload)
        elif event_type == HostRaceEventType.RACE_FINISHED:
            self.on_race_finish(payload)
        elif event_type == HostRaceEventType.RACE_STOPPED:
            self.on_race_stop(payload)
        elif event_type == HostRaceEventType.RACE_SNAPSHOT:
            logger.debug("RaceLink Race Snapshot Event payload=%s", payload)

    @staticmethod
    def _extract_source_payload(payload: Any) -> dict[str, Any]:
        if isinstance(payload, dict):
            source = payload.get("source_payload", payload)
            if isinstance(source, dict):
                return source
        return {}

    @staticmethod
    def _as_int(raw: Any, default: int | None = None) -> int | None:
        if raw is None:
            return default
        try:
            return int(raw)
        except Exception:
            return default

    @classmethod
    def _map_race_event_payload(cls, payload: Any) -> dict[str, Any]:
        source_payload = cls._extract_source_payload(payload)
        mapped = {
            "heat_id": cls._as_int(source_payload.get("heat_id", source_payload.get("current_heat"))),
            "group_id": cls._as_int(source_payload.get("rl_group_id", source_payload.get("group_id")), 255),
            "start_preset_id": cls._as_int(source_payload.get("rl_start_preset_id", source_payload.get("start_preset_id")), 1),
            "start_brightness": cls._as_int(source_payload.get("rl_start_brightness", source_payload.get("start_brightness")), 70),
            "finish_preset_id": cls._as_int(source_payload.get("rl_finish_preset_id", source_payload.get("finish_preset_id")), 2),
            "finish_brightness": cls._as_int(source_payload.get("rl_finish_brightness", source_payload.get("finish_brightness")), 100),
            "stop_preset_id": cls._as_int(source_payload.get("rl_stop_preset_id", source_payload.get("stop_preset_id")), 1),
            "stop_brightness": cls._as_int(source_payload.get("rl_stop_brightness", source_payload.get("stop_brightness")), 0),
            "trigger_startblock": bool(source_payload.get("rl_trigger_startblock", True)),
        }
        return mapped

    def on_race_start(self, args=None):
        mapped = self._map_race_event_payload(args)
        control = self.control_service.apply_race_event_group_control(
            group_id=mapped["group_id"],
            preset_id=mapped["start_preset_id"],
            brightness=mapped["start_brightness"],
        )
        startblock = None
        if mapped["trigger_startblock"]:
            startblock = self.startblock_service.trigger_race_event(
                event_name=HostRaceEventType.RACE_STARTED,
                target_group=mapped["group_id"],
            )
        logger.info("RaceLink Race Start Event mapped=%s control=%s startblock=%s", mapped, control, startblock)

    def on_race_finish(self, args=None):
        mapped = self._map_race_event_payload(args)
        control = self.control_service.apply_race_event_group_control(
            group_id=mapped["group_id"],
            preset_id=mapped["finish_preset_id"],
            brightness=mapped["finish_brightness"],
        )
        startblock = None
        if mapped["trigger_startblock"]:
            startblock = self.startblock_service.trigger_race_event(
                event_name=HostRaceEventType.RACE_FINISHED,
                target_group=mapped["group_id"],
            )
        logger.info("RaceLink Race Finish Event mapped=%s control=%s startblock=%s", mapped, control, startblock)

    def on_race_stop(self, args=None):
        mapped = self._map_race_event_payload(args)
        control = self.control_service.apply_race_event_group_control(
            group_id=mapped["group_id"],
            preset_id=mapped["stop_preset_id"],
            brightness=mapped["stop_brightness"],
        )
        startblock = self.startblock_service.trigger_race_event(
            event_name=HostRaceEventType.RACE_STOPPED,
            target_group=mapped["group_id"],
        )
        logger.info("RaceLink Race Stop Event mapped=%s control=%s startblock=%s", mapped, control, startblock)

    # Backward-compatible callbacks
    def onRaceStart(self, args=None):
        self.on_race_start(args)

    def onRaceFinish(self, args=None):
        self.on_race_finish(args)

    def onRaceStop(self, args=None):
        self.on_race_stop(args)

    def save_to_db(self, _args=None):
        if not callable(self._config_setter):
            return
        logger.debug("RL: Writing current states to Database")
        self._config_setter("rl_device_config", LegacyConfigMigration.dump_devices(self.repository))
        self._config_setter("rl_groups_config", LegacyConfigMigration.dump_groups(self.repository))

    def load_from_db(self):
        if not callable(self._config_getter) or not callable(self._config_setter):
            return

        logger.debug("RL: Applying config from Database")
        config_str_devices = self._config_getter("rl_device_config", None)
        config_str_groups = self._config_getter("rl_groups_config", None)

        if config_str_devices is None or config_str_devices == "":
            config_str_devices = "[]"
            self._config_setter("rl_device_config", config_str_devices)

        LegacyConfigMigration.load_devices_into_repo(config_str_devices, self.repository)

        if config_str_groups is None or config_str_groups == "":
            config_str_groups = str([{"name": "All WLED Nodes", "static_group": 1, "dev_type": 0}])
            self._config_setter("rl_groups_config", config_str_groups)

        LegacyConfigMigration.load_groups_into_repo(config_str_groups, self.repository)

    def getDevices(self, groupFilter=255, targetDevice=None, addToGroup=-1):
        return self.device_service.discover_devices(groupFilter, targetDevice, addToGroup, self.setNodeGroupId)

    def getStatus(self, groupFilter=255, targetDevice=None):
        return self.device_service.get_status(groupFilter, targetDevice)

    def setNodeGroupId(self, targetDevice: RL_Device, forceSet: bool = False, wait_for_ack: bool = True) -> bool:
        del forceSet
        if not self.transport_port.ensure_ready("setNodeGroupId"):
            return False

        self.transport_port.install_hooks()

        recv3 = _mac_last3_from_hex(targetDevice.addr)
        group_id = int(targetDevice.groupId) & 0xFF
        is_broadcast = recv3 == b"\xFF\xFF\xFF"

        if not is_broadcast:
            targetDevice.ack_clear()

        def _send():
            self.transport_port.lora.send_set_group(recv3, group_id)

        if not wait_for_ack or is_broadcast:
            _send()
            return True

        events, _ = self.transport_port.send_and_wait_for_reply(recv3, LP.OPC_SET_GROUP, _send, timeout_s=8.0)
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
        del args
        logger.debug("Forcing all known devices to their stored groups.")
        num_groups = len(self.repository.all_groups())

        for device in self.repository.all():
            if sanityCheck is True and device.groupId >= num_groups:
                device.groupId = 0
            self.setNodeGroupId(device, forceSet=True)

    def apply_device_switch(self, *, target_device: RL_Device, brightness: int, preset_id: int):
        self.control_service.apply_device_switch(target_device=target_device, brightness=brightness, preset_id=preset_id)

    def apply_group_switch(self, *, group_id: int, brightness: int, preset_id: int):
        self.control_service.apply_group_switch(group_id=group_id, brightness=brightness, preset_id=preset_id)

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

    def sendStream(
        self,
        payload: bytes,
        groupId: int | None = None,
        device: RL_Device | None = None,
        retries: int = 2,
        timeout_s: float = 8.0,
    ) -> dict[str, int]:
        return self.control_service.send_stream(payload, group_id=groupId, device=device, retries=retries, timeout_s=timeout_s)

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
        if callable(self._notify_fn):
            self._notify_fn(msg)
