"""Startblock-specific payload assembly and dispatch.

Builds the binary startblock-program payload (versioned by
``_STARTBLOCK_VER``) from the WebUI form, hands it to
:class:`StreamService` for the per-device fragmentation, and
records the outcome in the device's specials state.

Public API:

* ``send_startblock(targetDevice=None, targetGroup=None,
  params=...) -> bool`` — entry-point used by both the WebUI
  Specials path and the scene runner's ``startblock`` action
  kind.

Notes:

* The payload includes operator-supplied text fields (driver
  names) which can carry diacritics — the helper map at the
  top transliterates the most common German umlauts so the
  startblock firmware (ASCII-only display) doesn't render
  garbage.
* Returns ``False`` if the target device has no STARTBLOCK
  capability (caps filter introduced in C5 prevents most
  mis-targets at the editor).
"""

from __future__ import annotations

import re

from ..domain import RL_Device, get_dev_type_info, state_scope
from ..transport import mac_last3_from_hex

_STARTBLOCK_VER = 0x01

_DE_UMLAUT_MAP = {
    "Ã¤": "ae",
    "Ã¶": "oe",
    "Ã¼": "ue",
    "ÃŸ": "ss",
    "Ã„": "AE",
    "Ã–": "OE",
    "Ãœ": "UE",
}

_ALLOWED_NAME_RE = re.compile(r"[^A-Z0-9 _\-\.\+]", re.IGNORECASE)


def sanitize_pilot_name(name: str, max_len: int = 32) -> str:
    if not name:
        return ""
    for old, new in _DE_UMLAUT_MAP.items():
        name = name.replace(old, new)
    name = name.strip().upper()
    name = _ALLOWED_NAME_RE.sub(" ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:max_len] if max_len > 0 else name


def encode_channel_fixed2(label: str) -> bytes:
    normalized = (label or "").strip().upper()
    return (normalized + "--")[:2].encode("ascii", errors="replace")


def build_startblock_payload_v1(
    slot: int,
    channel_label: str,
    pilot_name: str,
    max_name_len: int = 32,
    name_encoding: str = "ascii",
) -> bytes:
    slot_b = max(0, min(255, int(slot)))
    chan2 = encode_channel_fixed2(channel_label)
    clean_name = sanitize_pilot_name(pilot_name, max_len=max_name_len)

    if name_encoding.lower() == "utf-8":
        name_bytes = clean_name.encode("utf-8", errors="replace")
    else:
        name_bytes = clean_name.encode("ascii", errors="replace")

    if len(name_bytes) > 255:
        name_bytes = name_bytes[:255]

    out = bytearray()
    out.append(_STARTBLOCK_VER)
    out.append(slot_b)
    out.extend(chan2)
    out.append(len(name_bytes))
    out.extend(name_bytes)
    return bytes(out)


class StartblockService:
    def __init__(self, controller, stream_service):
        self.controller = controller
        self.stream_service = stream_service

    def send_startblock_config(self, *, target_device=None, target_group=None, params=None):
        if target_group is not None:
            return False
        if not target_device or not self.controller._require_transport("sendStartblockConfig"):
            return False

        params = params or {}
        slots = int(params.get("startblock_slots", 1))
        first_slot = int(params.get("startblock_first_slot", 1))
        recv3 = mac_last3_from_hex(target_device.addr)
        if not recv3:
            return False

        ok_slots = self.controller.sendConfig(option=0x8C, data0=slots, recv3=recv3, wait_for_ack=True)
        if not ok_slots:
            return False
        ok_first = self.controller.sendConfig(option=0x8D, data0=first_slot, recv3=recv3, wait_for_ack=True)
        if not ok_first:
            return False

        target_device.specials["startblock_slots"] = slots & 0xFF
        target_device.specials["startblock_first_slot"] = first_slot & 0xFF
        try:
            self.controller.save_to_db(
                {"manual": True}, scopes={state_scope.DEVICE_SPECIALS}
            )
        except Exception:
            # swallow-ok: best-effort fallback; caller proceeds with safe default
            pass
        return True

    def is_startblock_device(self, dev: RL_Device) -> bool:
        try:
            type_id = getattr(dev, "caps", getattr(dev, "dev_type", 0))
            info = get_dev_type_info(type_id)
            return bool(info.get("STARTBLOCK"))
        except Exception:
            # swallow-ok: best-effort fallback; caller proceeds with safe default
            return False

    def iter_startblock_devices(self, *, target_device=None, target_group=None) -> list[RL_Device]:
        if target_device is not None:
            return [target_device] if self.is_startblock_device(target_device) else []

        if target_group is not None:
            gid = int(target_group)
            return [
                dev
                for dev in self.controller.device_repository.list()
                if self.is_startblock_device(dev) and int(getattr(dev, "groupId", 0) or 0) == gid
            ]

        return [
            dev
            for dev in self.controller.device_repository.list()
            if self.is_startblock_device(dev)
        ]

    def get_current_heat_slot_list(self):
        runtime = getattr(self.controller, "_host_api", None)
        source = getattr(runtime, "event_source", None)
        if source:
            return source.get_current_heat_slot_list()
        return []

    def normalize_slot_list(self, slot_list):
        out = []
        for item in (slot_list or []):
            if isinstance(item, (list, tuple)) and len(item) >= 3:
                out.append((int(item[0]), str(item[1] or ""), str(item[2] or "--")))
            elif isinstance(item, dict):
                out.append(
                    (
                        int(item.get("slot", 0)),
                        str(item.get("callsign", "") or ""),
                        str(item.get("racechannel", "--") or "--"),
                    )
                )
        return out

    def _build_device_slot_mapping(self, devices):
        slot_to_dev = {}
        dev_ranges = []
        for dev in devices:
            try:
                slot_count = int(dev.specials["startblock_slots"])
                first_slot = int(dev.specials["startblock_first_slot"])
            except Exception:
                # swallow-ok: best-effort fallback; caller proceeds with safe default
                slot_count = 8
                first_slot = 1

            slot_count = max(1, min(8, slot_count))
            first_slot = max(1, min(8, first_slot))
            last_slot = min(8, first_slot + slot_count - 1)
            dev_ranges.append((dev, first_slot, last_slot))

            for slot in range(first_slot, last_slot + 1):
                slot_to_dev.setdefault(slot, dev)

        return slot_to_dev, dev_ranges

    def send_startblock_control(self, *, target_device=None, target_group=None, params=None):
        if not self.controller._require_transport("sendStartblockControl"):
            return {}

        params = params or {}
        use_heat = params.get("startblock_use_current_heat")
        if use_heat is None:
            use_heat = True

        if use_heat:
            slot_list = self.get_current_heat_slot_list()
        else:
            slot_list = self.normalize_slot_list(params.get("startblock_slot_list") or [])

        slot_map = {int(slot): (callsign or "", racechannel or "--") for (slot, callsign, racechannel) in slot_list}
        slots_0based = [(slot, *slot_map.get(slot, ("", "--"))) for slot in range(8)]

        if target_group is not None:
            group_id = int(target_group)
            sent = []
            for slot0, callsign, racechannel in slots_0based:
                payload = build_startblock_payload_v1(slot0 + 1, racechannel, callsign)
                sent.append(
                    {
                        "slot": slot0 + 1,
                        "result": self.stream_service.send_stream(payload, groupId=group_id),
                    }
                )
            return {"mode": "group", "groupId": group_id, "sent": sent}

        if target_device is not None:
            if not self.is_startblock_device(target_device):
                return {"error": "targetDevice has no STARTBLOCK capability"}
            devices = [target_device]
        else:
            devices = self.iter_startblock_devices()

        if not devices:
            return {"mode": "unicast", "sent": []}

        slot_to_dev, dev_ranges = self._build_device_slot_mapping(devices)
        sent = []
        for slot0, callsign, racechannel in slots_0based:
            slot1 = slot0 + 1
            dev = slot_to_dev.get(slot1)
            if dev is None:
                continue

            payload = build_startblock_payload_v1(slot1, racechannel, callsign)
            sent.append(
                {
                    "slot": slot1,
                    "device": getattr(dev, "deviceId", getattr(dev, "mac", None)),
                    "result": self.stream_service.send_stream(payload, device=dev),
                }
            )

        return {
            "mode": "unicast",
            "devices": [
                {
                    "device": getattr(dev, "deviceId", getattr(dev, "mac", None)),
                    "first": first_slot,
                    "last": last_slot,
                }
                for (dev, first_slot, last_slot) in dev_ranges
            ],
            "sent": sent,
        }
