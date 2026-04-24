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


# -------------------- P_ControlAdv (variable-length, 3..21 B) --------------------
# First variable-length body in RaceLink. Keep in sync with racelink_proto.h:
#   Byte 0: groupId
#   Byte 1: flags (RL_FLAG_* identical to OPC_CONTROL; bits 5-7 reserved)
#   Byte 2: fieldMask
#     bit 0 brightness     -> +1 B
#     bit 1 mode           -> +1 B
#     bit 2 speed          -> +1 B
#     bit 3 intensity      -> +1 B
#     bit 4 custom1        -> +1 B
#     bit 5 custom2        -> +1 B
#     bit 6 custom3+checks -> +1 B (c3: bits 0-4, check1/2/3: bits 5/6/7)
#     bit 7 ext            -> extMask byte + ext fields follow
#   Byte X (if ext): extMask
#     bit 0 palette  -> +1 B
#     bit 1 color1   -> +3 B RGB
#     bit 2 color2   -> +3 B RGB
#     bit 3 color3   -> +3 B RGB
# Body bounded to BODY_MAX (22); worst case is 21 B.
# Fallback to fixed-length struct: see plan doc, section "Fallback zu fixed-length".

RL_ADV_F_BRIGHTNESS     = 0x01
RL_ADV_F_MODE           = 0x02
RL_ADV_F_SPEED          = 0x04
RL_ADV_F_INTENSITY      = 0x08
RL_ADV_F_CUSTOM1        = 0x10
RL_ADV_F_CUSTOM2        = 0x20
RL_ADV_F_CUSTOM3_CHECKS = 0x40
RL_ADV_F_EXT            = 0x80

RL_ADV_E_PALETTE = 0x01
RL_ADV_E_COLOR1  = 0x02
RL_ADV_E_COLOR2  = 0x04
RL_ADV_E_COLOR3  = 0x08

RL_ADV_BODY_MAX = 22  # mirrors BODY_MAX in racelink_proto.h


def _rgb_bytes(color) -> bytes:
    r, g, b = color
    return bytes([int(r) & 0xFF, int(g) & 0xFF, int(b) & 0xFF])


def build_control_adv_body(
    group_id: int,
    flags: int,
    *,
    brightness: int | None = None,
    mode: int | None = None,
    speed: int | None = None,
    intensity: int | None = None,
    custom1: int | None = None,
    custom2: int | None = None,
    custom3: int | None = None,
    check1: bool | None = None,
    check2: bool | None = None,
    check3: bool | None = None,
    palette: int | None = None,
    color1: tuple[int, int, int] | None = None,
    color2: tuple[int, int, int] | None = None,
    color3: tuple[int, int, int] | None = None,
) -> bytes:
    """Serialize a variable-length CONTROL_ADV body.

    Every kwarg defaults to ``None``, meaning "not present" (corresponding
    fieldMask/extMask bit stays 0, no bytes are emitted for that field).
    ``custom3`` occupies bits 0-4 of the custom3_checks byte (0..31); the
    checks flags occupy bits 5-7. The custom3_checks byte is emitted if any
    of ``custom3`` / ``check1`` / ``check2`` / ``check3`` is provided; absent
    values default to 0 in their respective bit slots.

    Body length is variable (3..21 B) and asserted to be <= RL_ADV_BODY_MAX.
    """

    field_mask = 0
    tail_main = bytearray()

    if brightness is not None:
        field_mask |= RL_ADV_F_BRIGHTNESS
        tail_main.append(int(brightness) & 0xFF)
    if mode is not None:
        field_mask |= RL_ADV_F_MODE
        tail_main.append(int(mode) & 0xFF)
    if speed is not None:
        field_mask |= RL_ADV_F_SPEED
        tail_main.append(int(speed) & 0xFF)
    if intensity is not None:
        field_mask |= RL_ADV_F_INTENSITY
        tail_main.append(int(intensity) & 0xFF)
    if custom1 is not None:
        field_mask |= RL_ADV_F_CUSTOM1
        tail_main.append(int(custom1) & 0xFF)
    if custom2 is not None:
        field_mask |= RL_ADV_F_CUSTOM2
        tail_main.append(int(custom2) & 0xFF)

    has_c3_or_checks = any(v is not None for v in (custom3, check1, check2, check3))
    if has_c3_or_checks:
        field_mask |= RL_ADV_F_CUSTOM3_CHECKS
        c3 = (int(custom3) & 0x1F) if custom3 is not None else 0
        packed = c3
        if check1:
            packed |= 0x20
        if check2:
            packed |= 0x40
        if check3:
            packed |= 0x80
        tail_main.append(packed)

    # Extended block (palette + up to 3 colors)
    ext_mask = 0
    tail_ext = bytearray()
    if palette is not None:
        ext_mask |= RL_ADV_E_PALETTE
        tail_ext.append(int(palette) & 0xFF)
    if color1 is not None:
        ext_mask |= RL_ADV_E_COLOR1
        tail_ext += _rgb_bytes(color1)
    if color2 is not None:
        ext_mask |= RL_ADV_E_COLOR2
        tail_ext += _rgb_bytes(color2)
    if color3 is not None:
        ext_mask |= RL_ADV_E_COLOR3
        tail_ext += _rgb_bytes(color3)

    body = bytearray()
    body.append(int(group_id) & 0xFF)
    body.append(int(flags) & 0xFF)

    if ext_mask:
        field_mask |= RL_ADV_F_EXT
        body.append(field_mask)
        body += tail_main
        body.append(ext_mask)
        body += tail_ext
    else:
        body.append(field_mask)
        body += tail_main

    assert len(body) <= RL_ADV_BODY_MAX, (
        f"CONTROL_ADV body {len(body)} B exceeds BODY_MAX={RL_ADV_BODY_MAX}"
    )
    return bytes(body)
