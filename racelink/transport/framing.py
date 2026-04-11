"""Framing helpers for gateway serial transport."""

from __future__ import annotations


def u16le(b2: bytes) -> int:
    return b2[0] | (b2[1] << 8)


def mac_last3_from_hex(mac12: str) -> bytes:
    mac = (mac12 or "").strip().replace(":", "").upper()
    if len(mac) < 6:
        mac = "FFFFFF"
    return bytes.fromhex(mac[-6:])
