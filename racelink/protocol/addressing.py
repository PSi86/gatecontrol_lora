"""Protocol-facing address normalization helpers."""

from __future__ import annotations


def to_hex_str(addr) -> str:
    if addr is None:
        return ""
    if isinstance(addr, (bytes, bytearray)):
        return bytes(addr).hex().upper()
    return str(addr).strip().replace(":", "").replace(" ", "").upper()


def last3_hex(addr) -> str:
    value = to_hex_str(addr)
    return value[-6:] if len(value) >= 6 else ""
