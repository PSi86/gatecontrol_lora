"""Domain models for RaceLink."""

from __future__ import annotations

import time
from typing import Optional

from .device_types import RL_FLAG_POWER_ON


class RL_Device:
    def __init__(
        self,
        addr: str,
        dev_type: int,
        name: str,
        groupId: int = 0,
        version: int = 0,
        caps: int = 0,
        voltage_mV: int = 0,
        node_rssi: int = 0,
        node_snr: int = 0,
        flags: int = RL_FLAG_POWER_ON,
        presetId: int = 1,
        brightness: int = 70,
        configByte: int = 0,
    ):
        self.addr: str = addr
        self.dev_type: int = int(dev_type)
        self.name: str = name
        self.version: int = int(version)
        self.caps: int = int(caps)
        self.groupId: int = int(groupId)

        self.flags: int = int(flags) & 0xFF
        self.presetId: int = int(presetId) & 0xFF
        self.brightness: int = int(brightness) & 0xFF
        self.configByte: int = int(configByte) & 0xFF
        self.specials: dict[str, int] = {}

        self.voltage_mV: int = int(voltage_mV)
        self.node_rssi: int = int(node_rssi)
        self.node_snr: int = int(node_snr)

        self.host_rssi: int = 0
        self.host_snr: int = 0
        self.last_seen_ts = 0

        self.link_online = None  # type: Optional[bool]
        self.link_ts = 0.0
        self.link_error = None
        self.last_ack = {"ok": False, "opcode": None, "status": None, "seq": None, "ts": 0.0}

    def update_from_identify(self, version, dev_type, groupId, mac6_bytes, host_rssi=None, host_snr=None):
        self.version = int(version) if version is not None else self.version
        if dev_type is not None:
            self.caps = int(dev_type)
            self.dev_type = int(dev_type)

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
            # swallow-ok: best-effort fallback; caller proceeds with safe default
            pass

    def update_from_status(self, flags, configByte, presetId, brightness, vbat_mV, node_rssi, node_snr, host_rssi=None, host_snr=None):
        self.flags = int(flags) & 0xFF if flags is not None else self.flags
        self.configByte = int(configByte) & 0xFF if configByte is not None else self.configByte
        self.presetId = int(presetId) & 0xFF if presetId is not None else self.presetId
        self.brightness = int(brightness) & 0xFF if brightness is not None else self.brightness

        self.voltage_mV = int(vbat_mV) if vbat_mV is not None else self.voltage_mV
        self.node_rssi = int(node_rssi) if node_rssi is not None else self.node_rssi
        self.node_snr = int(node_snr) if node_snr is not None else self.node_snr
        if host_rssi is not None:
            self.host_rssi = int(host_rssi)
        if host_snr is not None:
            self.host_snr = int(host_snr)

        self.last_seen_ts = time.time()

        try:
            self.mark_online()
        except Exception:
            # swallow-ok: best-effort fallback; caller proceeds with safe default
            pass

    def mark_online(self) -> None:
        self.link_online = True
        self.link_ts = time.time()
        self.link_error = None

    def mark_offline(self, reason: str = "") -> None:
        self.link_online = False
        self.link_ts = time.time()
        self.link_error = reason or None

    def ack_clear(self) -> None:
        self.last_ack = {"ok": False, "opcode": None, "status": None, "seq": None, "ts": 0.0}

    def ack_update(self, opcode: int, status: int, seq=None, host_rssi=None, host_snr=None) -> None:
        self.last_ack = {
            "ok": int(status) == 0,
            "opcode": int(opcode),
            "status": int(status),
            "seq": int(seq) if seq is not None else None,
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
            # swallow-ok: best-effort fallback; caller proceeds with safe default
            pass

    def ack_ok(self) -> bool:
        return bool(self.last_ack["ok"])


class RL_DeviceGroup:
    def __init__(self, name: str, static_group: int = 0, dev_type: int = 0):
        self.name: str = name
        self.static_group: int = static_group
        self.dev_type: int = int(dev_type)
