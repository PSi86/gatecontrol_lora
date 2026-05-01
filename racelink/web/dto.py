"""Data-transfer helpers for the RaceLink web layer."""

from __future__ import annotations

from typing import Iterable

from ..domain import (
    get_dev_type_info,
    get_special_keys_for_caps,
    is_wled_dev_type,
)


def serialize_device(dev):
    """Make an RL_Device JSON-serializable for the UI table."""
    online = bool(getattr(dev, "link_online", False))
    dev_type = int(getattr(dev, "dev_type", getattr(dev, "caps", 0)) or 0)
    type_info = get_dev_type_info(dev_type)

    data = {
        "addr": getattr(dev, "addr", None),
        "name": getattr(dev, "name", None),
        "dev_type": dev_type,
        "groupId": int(getattr(dev, "groupId", 0) or 0),
        "flags": int(getattr(dev, "flags", 0) or 0),
        "configByte": int(getattr(dev, "configByte", 0) or 0),
        "presetId": int(getattr(dev, "presetId", 0) or 0),
        "effectId": int(getattr(dev, "effectId", 0) or 0),
        "brightness": int(getattr(dev, "brightness", 0) or 0),
        "specials": dict(getattr(dev, "specials", {}) or {}),
        "voltage_mV": int(getattr(dev, "voltage_mV", 0) or 0),
        "node_rssi": int(getattr(dev, "node_rssi", 0) or 0),
        "node_snr": int(getattr(dev, "node_snr", 0) or 0),
        "host_rssi": int(getattr(dev, "host_rssi", 0) or 0),
        "host_snr": int(getattr(dev, "host_snr", 0) or 0),
        "version": int(getattr(dev, "version", 0) or 0),
        "caps": int(getattr(dev, "caps", dev_type) or 0),
        "dev_type_name": type_info.get("name"),
        "dev_type_caps": type_info.get("caps", []),
        "last_seen_ts": float(getattr(dev, "last_seen_ts", 0.0) or 0.0),
        "last_ack": getattr(dev, "last_ack", None),
        "online": online,
    }
    special_keys = get_special_keys_for_caps(type_info.get("caps", []))
    specials = getattr(dev, "specials", {}) or {}
    for key in special_keys:
        if key in specials:
            data[key] = specials[key]
    return data


def group_counts(devices: Iterable) -> dict:
    counts = {}
    try:
        for dev in devices:
            gid = int(getattr(dev, "groupId", 0) or 0)
            counts[gid] = counts.get(gid, 0) + 1
    except Exception:
        # swallow-ok: best-effort fallback; caller proceeds with safe default
        pass
    return counts


def group_caps_counts(devices: Iterable) -> dict:
    """Map ``groupId -> {capability: count}`` for the given devices.

    The scene editor uses this to filter target dropdowns to groups
    that actually have devices with the capability the action needs
    (C5). Without it, picking a non-WLED group for a "WLED Preset"
    action would silently fail at runtime: the OPC_PRESET frame goes
    out, the non-WLED nodes ignore it, and the operator sees a green
    SSE pip with no effect.

    The output is keyed by capability string (``"WLED"`` /
    ``"STARTBLOCK"`` / etc., from :data:`RL_DEV_TYPE_CAPS`) so the
    frontend can pick whichever cap matches the chosen action kind
    without re-walking the device list.
    """
    out: dict[int, dict[str, int]] = {}
    try:
        for dev in devices:
            gid = int(getattr(dev, "groupId", 0) or 0)
            dtype = int(getattr(dev, "dev_type", getattr(dev, "caps", 0)) or 0)
            caps = get_dev_type_info(dtype).get("caps", []) or []
            slot = out.setdefault(gid, {})
            for cap in caps:
                slot[cap] = slot.get(cap, 0) + 1
    except Exception:
        # swallow-ok: best-effort fallback; missing/malformed devices
        # just don't contribute. The UI tolerates an empty mapping.
        pass
    return out


def wled_count(devices: Iterable) -> int:
    count = 0
    try:
        for dev in devices:
            dtype = int(getattr(dev, "dev_type", getattr(dev, "caps", 0)) or 0)
            if is_wled_dev_type(dtype):
                count += 1
    except Exception:
        # swallow-ok: best-effort fallback; caller proceeds with safe default
        pass
    return count
