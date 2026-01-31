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
        dev_type: int,
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
        self.dev_type: int = int(dev_type)
        self.name: str = name
        self.version: int = int(version)  # GateControl FW version -> via IDENTIFY_REPLY
        # NOTE: identify reply field "caps" is now used as device type.
        self.caps: int = int(caps)
        self.groupId: int = int(groupId)

        # CONTROL state (proto v1.2)
        self.flags: int = int(flags) & 0xFF
        self.presetId: int = int(presetId) & 0xFF
        self.brightness: int = int(brightness) & 0xFF
        self.configByte: int = int(configByte) & 0xFF
        self.specials: dict[str, int] = {}

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

    def update_from_identify(self, version, dev_type, groupId, mac6_bytes, host_rssi=None, host_snr=None):
        self.version = int(version) if version is not None else self.version
        if dev_type is not None:
            self.caps = int(dev_type)
            self.dev_type = int(dev_type)

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
    def __init__(self, name: str, static_group: int = 0, dev_type: int = 0):
        self.name: str = name  # UI Name of Device
        self.static_group: int = static_group  # if static_group is false it needs to be initialized
        self.dev_type: int = int(dev_type)  # device number in the gc_devicelist


class GC_Dev_Type:
    IDENTIFY_COMMUNICATOR = 1
    WLED_REV3 = 10
    WLED_REV4 = 11
    WLED_STARTBLOCK_REV3 = 50

GC_DEV_TYPE_CAPS = ["STARTBLOCK", "LEDMATRIX", "WLED"]


def _normalize_select_options(raw_options) -> list[dict]:
    options: list[dict] = []
    for opt in raw_options or []:
        if isinstance(opt, dict):
            value = opt.get("value", opt.get("key"))
            label = opt.get("label", opt.get("name", value))
        else:
            value = getattr(opt, "value", opt)
            label = getattr(opt, "label", getattr(opt, "name", value))
        if value is None:
            continue
        options.append({"value": str(value), "label": str(label)})
    return options


def effect_select_options(*, context=None, **_kwargs) -> list[dict]:
    ctx = context or {}
    gc_instance = ctx.get("gc_instance") or ctx.get("gc")
    effect_list = None
    if gc_instance is not None:
        effect_list = getattr(gc_instance, "uiEffectList", None)
    if effect_list is None:
        effect_list = ctx.get("uiEffectList") or ctx.get("effect_list")
    return _normalize_select_options(effect_list)

GC_SPECIALS = {
    "STARTBLOCK": {
        "label": "Startblock",
        "options": [
            {"key": "startblock_slots", "label": "Number Of Slots", "option": 0x8C, "min": 1, "max": 8},
            {"key": "startblock_first_slot", "label": "First Slot", "option": 0x8D, "min": 1, "max": 8},
        ],
        "functions": [
            {
                "key": "startblock_config",
                "label": "Startblock",
                "comm": "sendStartblockConfig",
                "vars": ["startblock_slots", "startblock_first_slot"],
                "type": "config",
                "unicast": True,
                "broadcast": False,
            },
            {
                "key": "startblock_control",
                "label": "Startblock",
                "comm": "sendStartblockControl",
                "vars": ["startblock_slots", "startblock_first_slot"],
                "type": "control",
                "unicast": True,
                "broadcast": True,
            }
        ],
    },
    "WLED": {
        "label": "WLED",
        "options": [
            {"key": "presetId", "label": "Preset", "option": 0x90, "min": 1, "max": 255},
            {"key": "brightness", "label": "Brightness", "option": 0x91, "min": 0, "max": 255},
        ],
        "functions": [
            {
                "key": "wled_control",
                "label": "WLED",
                "comm": "sendWledControl",
                "vars": ["presetId", "brightness"],
                "ui": {
                    "presetId": {"generator": effect_select_options},
                },
                "type": "control",
                "unicast": True,
                "broadcast": True,
            }
        ],
    },
    "LEDMATRIX": {"label": "Matrix", "options": [], "functions": []},
}

GC_DEV_TYPE_INFO = {
    GC_Dev_Type.IDENTIFY_COMMUNICATOR: {"name": "IDENTIFY_COMMUNICATOR"},
    GC_Dev_Type.WLED_REV3: {"name": "WLED_REV3", "caps": ["WLED"]},
    GC_Dev_Type.WLED_REV4: {"name": "WLED_REV4", "caps": ["WLED"]},
    GC_Dev_Type.WLED_STARTBLOCK_REV3: {"name": "WLED_STARTBLOCK_REV3", "caps": ["STARTBLOCK", "WLED"]},
}


def get_dev_type_info(type_id: int | None) -> dict:
    tid = int(type_id or 0)
    base = GC_DEV_TYPE_INFO.get(tid, {"name": f"UNKNOWN_{tid}"})
    caps = set(base.get("caps", []))
    info = {"name": base.get("name", f"UNKNOWN_{tid}"), "caps": sorted(caps)}
    for cap in GC_DEV_TYPE_CAPS:
        info[cap] = cap in caps
    return info


def is_wled_dev_type(type_id: int | None) -> bool:
    info = get_dev_type_info(type_id)
    return bool(info.get("WLED"))


def get_specials_config(*, context: dict | None = None, serialize_ui: bool = False) -> dict:
    data = {}
    for cap, info in GC_SPECIALS.items():
        options = [dict(opt) for opt in info.get("options", [])]
        functions = []
        for fn in info.get("functions", []):
            fn_copy = dict(fn)
            ui_meta = {}
            for var_key, ui_info in (fn.get("ui") or {}).items():
                ui_copy = dict(ui_info)
                generator = ui_copy.get("generator")
                if callable(generator):
                    if serialize_ui:
                        ui_copy.pop("generator", None)
                        ui_copy["options"] = generator(context=context or {})
                    else:
                        ui_copy["generator"] = generator
                ui_meta[var_key] = ui_copy
            if ui_meta:
                fn_copy["ui"] = ui_meta
            functions.append(fn_copy)
        data[cap] = {
            **{k: v for k, v in info.items() if k not in {"options", "functions"}},
            "options": options,
            "functions": functions,
        }
    return data


def get_special_keys_for_caps(caps: list[str]) -> list[str]:
    keys = []
    for cap in caps:
        spec = GC_SPECIALS.get(cap, {})
        for opt in spec.get("options", []):
            key = opt.get("key")
            if key:
                keys.append(key)
    return keys


def build_specials_state(type_id: int | None, stored: dict | None = None) -> dict[str, int]:
    caps = get_dev_type_info(type_id).get("caps", [])
    stored = stored or {}
    state: dict[str, int] = {}
    for cap in caps:
        spec = GC_SPECIALS.get(cap, {})
        for opt in spec.get("options", []):
            key = opt.get("key")
            if not key:
                continue
            default_val = opt.get("min", 0)
            try:
                state[key] = int(stored.get(key, default_val))
            except Exception:
                state[key] = int(default_val)
    return state


def create_device(*, dev_type: int, specials: dict | None = None, **kwargs) -> GC_Device:
    dev = GC_Device(dev_type=dev_type, **kwargs)
    dev.specials = build_specials_state(dev_type, specials)
    return dev


gc_backup_devicelist = []
gc_backup_grouplist = [GC_DeviceGroup("All WLED Gates", 1, 0)]

gc_devicelist: list[GC_Device] = []
gc_grouplist: list[GC_DeviceGroup] = []
