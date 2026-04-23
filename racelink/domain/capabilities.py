"""Capability helpers for RaceLink domain metadata."""

from __future__ import annotations

from .device_types import get_dev_type_info
from .specials import RL_SPECIALS


def get_special_keys_for_caps(caps: list[str]) -> list[str]:
    keys = []
    for cap in caps:
        spec = RL_SPECIALS.get(cap, {})
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
        spec = RL_SPECIALS.get(cap, {})
        for opt in spec.get("options", []):
            key = opt.get("key")
            if not key:
                continue
            default_val = opt.get("min", 0)
            try:
                state[key] = int(stored.get(key, default_val))
            except Exception:
                # swallow-ok: best-effort fallback; caller proceeds with safe default
                state[key] = int(default_val)
    return state
