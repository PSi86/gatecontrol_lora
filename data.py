"""Shared data models and constants for GateControl LoRa."""

from __future__ import annotations

from typing import Optional
import time

# ---- LoRa/WLED control flags (shared with lora_proto.h / WLED usermod) ----
# Bit layout must match the firmware.
GC_FLAG_POWER_ON = 0x01  # node power state (0=off, 1=on)
GC_FLAG_ARM_ON_SYNC = 0x02  # config/control arms node; node starts/restarts on next SYNC
GC_FLAG_HAS_BRI = 0x04  # CONTROL brightness field is valid (otherwise keep current / allow SYNC brightness)
GC_FLAG_FORCE_TT0 = 0x08  # optional: force effect timebase to 0 on apply (client-side)
GC_FLAG_FORCE_REAPPLY = 0x10  # optional: re-apply preset even if unchanged (client-side)


class GC_Device:
    def __init__(
        self,
        addr: str,
        type: int,
        name: str,
        groupId: int = 0,
        version: int = 0,
        caps: int = 0,
        voltage_mV: int = 0,
        node_rssi: int = 0,
        node_snr: int = 0,
        flags: int = GC_FLAG_POWER_ON,
        presetId: int = 1,
        brightness: int = 70,
        configByte: int = 0,
    ):
        self.addr: str = addr
        self.type: int = int(type)
        self.name: str = name
        self.version: int = int(version)  # GateControl FW version -> via IDENTIFY_REPLY
        self.caps: int = int(caps)  # capability flags (IDENTIFY_REPLY)
        self.groupId: int = int(groupId)

        # CONTROL state (proto v1.2)
        self.flags: int = int(flags) & 0xFF
        self.presetId: int = int(presetId) & 0xFF
        self.brightness: int = int(brightness) & 0xFF
        self.configByte: int = int(configByte) & 0xFF

        # Telemetry (STATUS_REPLY)
        self.voltage_mV: int = int(voltage_mV)
        self.node_rssi: int = int(node_rssi)
        self.node_snr: int = int(node_snr)

        # Link metrics (measured at master / USB forward)
        self.host_rssi: int = 0
        self.host_snr: int = 0
        self.last_seen_ts = 0  # unix time of last reply

        # Link state (central online/offline; None = unknown / not shown in UI)
        self.link_online = None  # type: Optional[bool]
        self.link_ts = 0.0
        self.link_error = None
        # ACK status: last ACK from this device
        self.last_ack = {"ok": False, "opcode": None, "status": None, "seq": None, "ts": 0.0}

    def update_from_identify(self, version, caps, groupId, mac6_bytes, host_rssi=None, host_snr=None):
        self.version = int(version) if version is not None else self.version
        self.caps = int(caps) if caps is not None else self.caps

        # Only overwrite groupId if device was previously unconfigured
        if self.groupId == 0 and groupId:
            self.groupId = int(groupId) & 0xFF

        if host_rssi is not None:
            self.host_rssi = int(host_rssi)
        if host_snr is not None:
            self.host_snr = int(host_snr)

        self.last_seen_ts = time.time()

        try:
            self.mark_online()
        except Exception:
            pass

    def update_from_status(self, flags, configByte, presetId, brightness, vbat_mV, node_rssi, node_snr, host_rssi=None, host_snr=None):
        # CONTROL snapshot (as reported by node)
        self.flags = int(flags) & 0xFF if flags is not None else self.flags
        self.configByte = int(configByte) & 0xFF if configByte is not None else self.configByte
        self.presetId = int(presetId) & 0xFF if presetId is not None else self.presetId
        self.brightness = int(brightness) & 0xFF if brightness is not None else self.brightness

        # Telemetry
        self.voltage_mV = int(vbat_mV) if vbat_mV is not None else self.voltage_mV
        self.node_rssi = int(node_rssi) if node_rssi is not None else self.node_rssi
        self.node_snr = int(node_snr) if node_snr is not None else self.node_snr
        if host_rssi is not None:
            self.host_rssi = int(host_rssi)
        if host_snr is not None:
            self.host_snr = int(host_snr)

        self.last_seen_ts = time.time()

        # Update link state: any reply implies the node is online.
        try:
            self.mark_online()
        except Exception:
            pass

    def mark_online(self) -> None:
        self.link_online = True
        self.link_ts = time.time()
        self.link_error = None

    def mark_offline(self, reason: str = "") -> None:
        self.link_online = False
        self.link_ts = time.time()
        self.link_error = (reason or None)

    # --- ACK Helpers (generic for any opcode) ---
    def ack_clear(self) -> None:
        self.last_ack = {"ok": False, "opcode": None, "status": None, "seq": None, "ts": 0.0}

    def ack_update(self, opcode: int, status: int, seq=None, host_rssi=None, host_snr=None) -> None:
        self.last_ack = {
            "ok": (int(status) == 0),
            "opcode": int(opcode),
            "status": int(status),
            "seq": (int(seq) if seq is not None else None),
            "ts": time.time(),
        }
        if host_rssi is not None:
            self.host_rssi = int(host_rssi)
        if host_snr is not None:
            self.host_snr = int(host_snr)

        self.last_seen_ts = time.time()
        try:
            self.mark_online()
        except Exception:
            pass

    def ack_ok(self) -> bool:
        return bool(self.last_ack["ok"])


class GC_DeviceGroup:
    def __init__(self, name: str, static_group: int = 0, device_type: int = 0):
        self.name: str = name  # UI Name of Device
        self.static_group: int = static_group  # if static_group is false it needs to be initialized
        self.device_type: int = int(device_type)  # device number in the gc_devicelist


class GC_Type:
    IDENTIFY_COMMUNICATOR = 1  # Same as used from TBS Fusion OSD VRX Plugin
    ESPNOW_GATE = 20  # unified message structure - groups only work with this type

    BASIC_IR_GATE = 21  # IR Area Controller will identify with this code
    CUSTOM_IR_GATE = 22  # not used currently

    WIZMOTE_GATE = 23  # standard WLED type (does not support self identification)
    WLED_CUSTOM = 24  # once custom WLED fw is built this will be the identifier

    GET_DEVICES = 30  # only devices with groupId != 0 should respond here
    SET_GROUP = 31  # send this command to make a device store the received groupId


gc_backup_devicelist = []
gc_backup_grouplist = [GC_DeviceGroup("Unconfigured", 1, 0)]

gc_devicelist: list[GC_Device] = []
gc_grouplist: list[GC_DeviceGroup] = []
