from __future__ import annotations

import json
import re

from ...data import get_dev_type_info
from ...racelink_transport import _mac_last3_from_hex

_STARTBLOCK_VER = 0x01
_DE_UMLAUT_MAP = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "Ä": "AE", "Ö": "OE", "Ü": "UE"}
_ALLOWED_NAME_RE = re.compile(r"[^A-Z0-9 _\-\.\+]", re.IGNORECASE)


class StartblockService:
    def __init__(self, transport_coordinator, control_service, rhapi, save_to_db_fn, repository):
        self._transport = transport_coordinator
        self._control = control_service
        self._rhapi = rhapi
        self._save_to_db = save_to_db_fn
        self._repo = repository

    @staticmethod
    def _sanitize_pilot_name(name: str, max_len: int = 32) -> str:
        if not name:
            return ""
        for k, v in _DE_UMLAUT_MAP.items():
            name = name.replace(k, v)
        name = _ALLOWED_NAME_RE.sub(" ", name.strip().upper())
        name = re.sub(r"\s+", " ", name).strip()
        return name[:max_len] if max_len > 0 else name

    @staticmethod
    def _encode_channel_fixed2(label: str) -> bytes:
        lab2 = ((label or "").strip().upper() + "--")[:2]
        return lab2.encode("ascii", errors="replace")

    def build_startblock_payload_v1(self, slot: int, channel_label: str, pilot_name: str, max_name_len: int = 32, name_encoding: str = "ascii") -> bytes:
        slot_b = max(0, min(255, int(slot)))
        chan2 = self._encode_channel_fixed2(channel_label)
        clean_name = self._sanitize_pilot_name(pilot_name, max_len=max_name_len)
        name_bytes = clean_name.encode("utf-8" if name_encoding.lower() == "utf-8" else "ascii", errors="replace")
        if len(name_bytes) > 255:
            name_bytes = name_bytes[:255]
        out = bytearray([_STARTBLOCK_VER, slot_b])
        out.extend(chan2)
        out.append(len(name_bytes))
        out.extend(name_bytes)
        return bytes(out)

    def send_startblock_config(self, *, target_device=None, target_group=None, params=None, send_config_fn=None):
        if target_group is not None:
            return False
        if not target_device or not self._transport.ensure_ready("sendStartblockConfig"):
            return False
        params = params or {}
        slots = int(params.get("startblock_slots", 1))
        first_slot = int(params.get("startblock_first_slot", 1))
        recv3 = _mac_last3_from_hex(target_device.addr)
        if not recv3 or not callable(send_config_fn):
            return False
        if not send_config_fn(option=0x8C, data0=slots, recv3=recv3, wait_for_ack=True):
            return False
        if not send_config_fn(option=0x8D, data0=first_slot, recv3=recv3, wait_for_ack=True):
            return False
        target_device.specials["startblock_slots"] = slots & 0xFF
        target_device.specials["startblock_first_slot"] = first_slot & 0xFF
        self._save_to_db({"manual": True})
        return True

    def _is_startblock_device(self, dev) -> bool:
        type_id = getattr(dev, "caps", getattr(dev, "dev_type", 0))
        return bool(get_dev_type_info(type_id).get("STARTBLOCK"))

    def _iter_startblock_devices(self, *, target_device=None, target_group=None):
        if target_device is not None:
            return [target_device] if self._is_startblock_device(target_device) else []
        if target_group is not None:
            gid = int(target_group)
            return [dev for dev in self._repo.by_group(gid) if self._is_startblock_device(dev)]
        return [dev for dev in self._repo.all() if self._is_startblock_device(dev)]

    def _normalize_startblock_slot_list(self, slot_list):
        out = []
        for item in (slot_list or []):
            if isinstance(item, (list, tuple)) and len(item) >= 3:
                out.append((int(item[0]), str(item[1] or ""), str(item[2] or "--")))
            elif isinstance(item, dict):
                out.append((int(item.get("slot", 0)), str(item.get("callsign", "") or ""), str(item.get("racechannel", "--") or "--")))
        return out

    def get_current_heat_slot_list(self):
        freq = json.loads(self._rhapi.race.frequencyset.frequencies)
        racechannels = ["--" if band is None else f"{band}{freq['c'][i]}" for i, band in enumerate(freq["b"])]
        ctx = self._rhapi._racecontext
        heat_nodes = ctx.rhdata.get_heatNodes_by_heat(ctx.race.current_heat) or []
        callsign_by_slot = {}
        for hn in heat_nodes:
            slot = int(getattr(hn, "node_index"))
            pid = getattr(hn, "pilot_id", None)
            p = ctx.rhdata.get_pilot(pid) if pid else None
            callsign_by_slot[slot] = (p.callsign if p else "")
        n = min(len(racechannels), 8)
        return [(i, callsign_by_slot.get(i, ""), racechannels[i]) for i in range(n)]

    def send_startblock_control(self, *, target_device=None, target_group=None, params=None):
        if not self._transport.ensure_ready("sendStartblockControl"):
            return {}
        params = params or {}
        use_heat = params.get("startblock_use_current_heat")
        use_heat = True if use_heat is None else use_heat
        slot_list = self.get_current_heat_slot_list() if use_heat else self._normalize_startblock_slot_list(params.get("startblock_slot_list") or [])
        slot_map = {int(s): (cs or "", rc or "--") for (s, cs, rc) in slot_list}
        slots_0based = [(i, *slot_map.get(i, ("", "--"))) for i in range(8)]

        if target_group is not None:
            gid = int(target_group)
            sent = []
            for slot0, cs, rc in slots_0based:
                payload = self.build_startblock_payload_v1(slot0 + 1, rc, cs)
                sent.append({"slot": slot0 + 1, "result": self._control.send_stream(payload, group_id=gid)})
            return {"mode": "group", "groupId": gid, "sent": sent}

        devices = [target_device] if target_device is not None else self._iter_startblock_devices()
        if target_device is not None and not self._is_startblock_device(target_device):
            return {"error": "targetDevice has no STARTBLOCK capability"}
        if not devices:
            return {"mode": "unicast", "sent": []}

        slot_to_dev = {}
        dev_ranges = []
        for dev in devices:
            try:
                count = int(dev.specials["startblock_slots"])
                first = int(dev.specials["startblock_first_slot"])
            except Exception:
                count, first = 8, 1
            count = max(1, min(8, count))
            first = max(1, min(8, first))
            last = min(8, first + count - 1)
            dev_ranges.append((dev, first, last))
            for s in range(first, last + 1):
                slot_to_dev.setdefault(s, dev)

        sent = []
        for slot0, cs, rc in slots_0based:
            slot1 = slot0 + 1
            dev = slot_to_dev.get(slot1)
            if not dev:
                continue
            payload = self.build_startblock_payload_v1(slot1, rc, cs)
            sent.append({"slot": slot1, "device": getattr(dev, "deviceId", getattr(dev, "mac", None)), "result": self._control.send_stream(payload, device=dev)})

        return {"mode": "unicast", "devices": [{"device": getattr(d, "deviceId", getattr(d, "mac", None)), "first": a, "last": b} for (d, a, b) in dev_ranges], "sent": sent}
