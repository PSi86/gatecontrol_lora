"""Packet body helpers for the RaceLink protocol."""

from __future__ import annotations

import struct


def build_get_devices_body(group_id=0, flags=0) -> bytes:
    return struct.pack("<BB", int(group_id) & 0xFF, int(flags) & 0xFF)


def build_set_group_body(group_id: int) -> bytes:
    return struct.pack("<B", int(group_id) & 0xFF)


def build_control_body(group_id: int, flags: int, preset_id: int, brightness: int) -> bytes:
    return struct.pack("<BBBB", int(group_id) & 0xFF, int(flags) & 0xFF, int(preset_id) & 0xFF, int(brightness) & 0xFF)


def build_config_body(option: int = 0, data0: int = 0, data1: int = 0, data2: int = 0, data3: int = 0) -> bytes:
    return struct.pack("<BBBBB", int(option) & 0xFF, int(data0) & 0xFF, int(data1) & 0xFF, int(data2) & 0xFF, int(data3) & 0xFF)


def build_sync_body(ts24: int = 0, brightness: int = 0) -> bytes:
    ts = int(ts24) & 0xFFFFFF
    return bytes([(ts & 0xFF), ((ts >> 8) & 0xFF), ((ts >> 16) & 0xFF), (int(brightness) & 0xFF)])
