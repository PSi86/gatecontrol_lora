"""Packet body helpers for the RaceLink protocol."""

from __future__ import annotations

import struct


def build_get_devices_body(group_id=0, flags=0) -> bytes:
    return struct.pack("<BB", int(group_id) & 0xFF, int(flags) & 0xFF)


def build_set_group_body(group_id: int) -> bytes:
    return struct.pack("<B", int(group_id) & 0xFF)


def build_preset_body(group_id: int, flags: int, preset_id: int, brightness: int) -> bytes:
    """Serialize a fixed-length OPC_PRESET body (4 B).

    Matches ``P_Preset`` in ``racelink_proto.h`` (pre-rename: ``P_Control``).
    """
    return struct.pack(
        "<BBBB",
        int(group_id) & 0xFF,
        int(flags) & 0xFF,
        int(preset_id) & 0xFF,
        int(brightness) & 0xFF,
    )


def build_config_body(option: int = 0, data0: int = 0, data1: int = 0, data2: int = 0, data3: int = 0) -> bytes:
    return struct.pack("<BBBBB", int(option) & 0xFF, int(data0) & 0xFF, int(data1) & 0xFF, int(data2) & 0xFF, int(data3) & 0xFF)


# OPC_SYNC flags. Mirrors ``SYNC_FLAG_TRIGGER_ARMED`` in ``racelink_proto.h``.
# Bit 0 gates pending arm-on-sync materialisation device-side; without it,
# OPC_SYNC only adjusts the device timebase. Autosync (gateway- or
# host-driven) MUST emit the 4-byte legacy form (flags omitted) so it
# never accidentally fires armed effects ahead of a deliberate sync.
SYNC_FLAG_TRIGGER_ARMED = 0x01


def build_sync_body(ts24: int = 0, brightness: int = 0, flags: int = 0) -> bytes:
    """Serialize an OPC_SYNC body. 4 B legacy when ``flags == 0`` (clock
    tick), 5 B with the trailing flags byte otherwise. Device firmware
    has ``req_len = 0`` for OPC_SYNC so both lengths are accepted; old
    firmware that still has ``req_len = 4`` will reject the 5 B form —
    do not deploy a new host against unflashed nodes.
    """
    ts = int(ts24) & 0xFFFFFF
    base = bytes([(ts & 0xFF), ((ts >> 8) & 0xFF), ((ts >> 16) & 0xFF), (int(brightness) & 0xFF)])
    f = int(flags) & 0xFF
    if f == 0:
        return base
    return base + bytes([f])


# OPC_OFFSET modes — mirrors the OffsetMode enum in racelink_proto.h.
OFFSET_MODE_NONE     = 0x00
OFFSET_MODE_EXPLICIT = 0x01
OFFSET_MODE_LINEAR   = 0x02
OFFSET_MODE_VSHAPE   = 0x03
OFFSET_MODE_MODULO   = 0x04

OFFSET_MODE_NAMES = {
    OFFSET_MODE_NONE:     "none",
    OFFSET_MODE_EXPLICIT: "explicit",
    OFFSET_MODE_LINEAR:   "linear",
    OFFSET_MODE_VSHAPE:   "vshape",
    OFFSET_MODE_MODULO:   "modulo",
}
OFFSET_MODE_VALUES = {v: k for k, v in OFFSET_MODE_NAMES.items()}

# Bounds on the formula parameters. ``base_ms`` and ``step_ms`` are signed
# 16-bit so reverse cascades work via negative ``step_ms``; the device
# clamps the computed offset to [0, 65535] before snapshotting.
OFFSET_MS_MIN     = 0
OFFSET_MS_MAX     = 0xFFFF
OFFSET_BASE_MIN   = -32768
OFFSET_BASE_MAX   = 32767
OFFSET_STEP_MIN   = -32768
OFFSET_STEP_MAX   = 32767
OFFSET_CENTER_MIN = 0
OFFSET_CENTER_MAX = 254
OFFSET_CYCLE_MIN  = 1
OFFSET_CYCLE_MAX  = 255


OffsetModeArg = "int | str"  # cosmetic alias for the docstring; real type below


def _coerce_mode(mode: int | str) -> int:
    """Accept either the int enum value or its lowercase string name."""
    if isinstance(mode, int):
        if mode in OFFSET_MODE_NAMES:
            return mode
        raise ValueError(f"unknown offset mode int: {mode}")
    if isinstance(mode, str):
        m = OFFSET_MODE_VALUES.get(mode.lower())
        if m is None:
            raise ValueError(
                f"unknown offset mode {mode!r}; expected one of {sorted(OFFSET_MODE_VALUES)}"
            )
        return m
    raise TypeError(f"offset mode must be int or str, got {type(mode).__name__}")


def build_offset_body(
    group_id: int,
    mode: int | str = OFFSET_MODE_NONE,
    *,
    offset_ms: int = 0,
    base_ms: int = 0,
    step_ms: int = 0,
    center: int = 0,
    cycle: int = 1,
) -> bytes:
    """Serialize a variable-length OPC_OFFSET body (2..7 B).

    Mirrors the variable-length wire format documented in ``racelink_proto.h``:
    every body starts with ``groupId`` and ``mode``; the remaining payload
    depends on ``mode``. ``mode`` may be passed either as the int enum value
    (``OFFSET_MODE_LINEAR``) or its lowercase name (``"linear"``).

    Per-mode payload contract:
      * ``NONE``     — no extra fields. Receiver clears stored config.
      * ``EXPLICIT`` — ``offset_ms`` (uint16 LE; clamped 0..65535).
      * ``LINEAR``   — ``base_ms`` (int16 LE) + ``step_ms`` (int16 LE).
                       Receiver computes ``base + groupId * step`` per device.
      * ``VSHAPE``   — ``base_ms`` (int16) + ``step_ms`` (int16) + ``center`` (uint8).
                       Receiver computes ``base + |groupId - center| * step``.
      * ``MODULO``   — ``base_ms`` (int16) + ``step_ms`` (int16) + ``cycle`` (uint8 1..255).
                       Receiver computes ``base + (groupId % cycle) * step``.

    Use ``group_id=255`` to broadcast: every device matches the body filter,
    and the wire-level acceptance gate on subsequent OPC_CONTROLs picks the
    participating subset by their offset_mode flag matching the new state.
    """
    g = int(group_id) & 0xFF
    m = _coerce_mode(mode)

    if m == OFFSET_MODE_NONE:
        return struct.pack("<BB", g, m)
    if m == OFFSET_MODE_EXPLICIT:
        ms = max(OFFSET_MS_MIN, min(OFFSET_MS_MAX, int(offset_ms)))
        return struct.pack("<BBH", g, m, ms)
    if m == OFFSET_MODE_LINEAR:
        b = max(OFFSET_BASE_MIN, min(OFFSET_BASE_MAX, int(base_ms)))
        s = max(OFFSET_STEP_MIN, min(OFFSET_STEP_MAX, int(step_ms)))
        return struct.pack("<BBhh", g, m, b, s)
    if m == OFFSET_MODE_VSHAPE:
        b = max(OFFSET_BASE_MIN, min(OFFSET_BASE_MAX, int(base_ms)))
        s = max(OFFSET_STEP_MIN, min(OFFSET_STEP_MAX, int(step_ms)))
        c = max(OFFSET_CENTER_MIN, min(OFFSET_CENTER_MAX, int(center))) & 0xFF
        return struct.pack("<BBhhB", g, m, b, s, c)
    if m == OFFSET_MODE_MODULO:
        b = max(OFFSET_BASE_MIN, min(OFFSET_BASE_MAX, int(base_ms)))
        s = max(OFFSET_STEP_MIN, min(OFFSET_STEP_MAX, int(step_ms)))
        cy = max(OFFSET_CYCLE_MIN, min(OFFSET_CYCLE_MAX, int(cycle))) & 0xFF
        return struct.pack("<BBhhB", g, m, b, s, cy)
    raise AssertionError(f"unhandled mode {m!r}")  # unreachable


def parse_offset_body(body: bytes) -> dict:
    """Inverse of :func:`build_offset_body`. Used by tests; the WLED firmware
    has its own equivalent. Raises ``ValueError`` on malformed bodies."""
    if len(body) < 2:
        raise ValueError(f"OPC_OFFSET body too short: {len(body)} B")
    g = body[0]
    m = body[1]
    if m == OFFSET_MODE_NONE:
        if len(body) != 2:
            raise ValueError("OPC_OFFSET (NONE) body must be 2 B")
        return {"group_id": g, "mode": "none"}
    if m == OFFSET_MODE_EXPLICIT:
        if len(body) != 4:
            raise ValueError(f"OPC_OFFSET (EXPLICIT) body must be 4 B, got {len(body)}")
        (ms,) = struct.unpack("<H", body[2:4])
        return {"group_id": g, "mode": "explicit", "offset_ms": ms}
    if m == OFFSET_MODE_LINEAR:
        if len(body) != 6:
            raise ValueError(f"OPC_OFFSET (LINEAR) body must be 6 B, got {len(body)}")
        b, s = struct.unpack("<hh", body[2:6])
        return {"group_id": g, "mode": "linear", "base_ms": b, "step_ms": s}
    if m == OFFSET_MODE_VSHAPE:
        if len(body) != 7:
            raise ValueError(f"OPC_OFFSET (VSHAPE) body must be 7 B, got {len(body)}")
        b, s, c = struct.unpack("<hhB", body[2:7])
        return {"group_id": g, "mode": "vshape", "base_ms": b, "step_ms": s, "center": c}
    if m == OFFSET_MODE_MODULO:
        if len(body) != 7:
            raise ValueError(f"OPC_OFFSET (MODULO) body must be 7 B, got {len(body)}")
        b, s, cy = struct.unpack("<hhB", body[2:7])
        return {"group_id": g, "mode": "modulo", "base_ms": b, "step_ms": s, "cycle": cy}
    raise ValueError(f"unknown OPC_OFFSET mode byte 0x{m:02X}")


# -------------------- OPC_CONTROL (variable-length, 3..21 B) --------------------
# Phase-D rename: this is the packet formerly known as OPC_CONTROL_ADV /
# P_ControlAdv. Wire format is byte-identical; only the identifiers changed.
# Keep in sync with ``racelink_proto.h``:
#   Byte 0: groupId
#   Byte 1: flags (RL_FLAG_* identical to OPC_PRESET; bits 5-7 reserved)
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

RL_CTRL_F_BRIGHTNESS     = 0x01
RL_CTRL_F_MODE           = 0x02
RL_CTRL_F_SPEED          = 0x04
RL_CTRL_F_INTENSITY      = 0x08
RL_CTRL_F_CUSTOM1        = 0x10
RL_CTRL_F_CUSTOM2        = 0x20
RL_CTRL_F_CUSTOM3_CHECKS = 0x40
RL_CTRL_F_EXT            = 0x80

RL_CTRL_E_PALETTE = 0x01
RL_CTRL_E_COLOR1  = 0x02
RL_CTRL_E_COLOR2  = 0x04
RL_CTRL_E_COLOR3  = 0x08

RL_CTRL_BODY_MAX = 22  # mirrors BODY_MAX in racelink_proto.h


def _rgb_bytes(color) -> bytes:
    r, g, b = color
    return bytes([int(r) & 0xFF, int(g) & 0xFF, int(b) & 0xFF])


def build_control_body(
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
    """Serialize a variable-length OPC_CONTROL body (pre-rename: CONTROL_ADV).

    Every kwarg defaults to ``None``, meaning "not present" (corresponding
    fieldMask/extMask bit stays 0, no bytes are emitted for that field).
    ``custom3`` occupies bits 0-4 of the custom3_checks byte (0..31); the
    checks flags occupy bits 5-7. The custom3_checks byte is emitted if any
    of ``custom3`` / ``check1`` / ``check2`` / ``check3`` is provided; absent
    values default to 0 in their respective bit slots.

    Body length is variable (3..21 B) and asserted to be <= RL_CTRL_BODY_MAX.
    """

    field_mask = 0
    tail_main = bytearray()

    if brightness is not None:
        field_mask |= RL_CTRL_F_BRIGHTNESS
        tail_main.append(int(brightness) & 0xFF)
    if mode is not None:
        field_mask |= RL_CTRL_F_MODE
        tail_main.append(int(mode) & 0xFF)
    if speed is not None:
        field_mask |= RL_CTRL_F_SPEED
        tail_main.append(int(speed) & 0xFF)
    if intensity is not None:
        field_mask |= RL_CTRL_F_INTENSITY
        tail_main.append(int(intensity) & 0xFF)
    if custom1 is not None:
        field_mask |= RL_CTRL_F_CUSTOM1
        tail_main.append(int(custom1) & 0xFF)
    if custom2 is not None:
        field_mask |= RL_CTRL_F_CUSTOM2
        tail_main.append(int(custom2) & 0xFF)

    has_c3_or_checks = any(v is not None for v in (custom3, check1, check2, check3))
    if has_c3_or_checks:
        field_mask |= RL_CTRL_F_CUSTOM3_CHECKS
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
        ext_mask |= RL_CTRL_E_PALETTE
        tail_ext.append(int(palette) & 0xFF)
    if color1 is not None:
        ext_mask |= RL_CTRL_E_COLOR1
        tail_ext += _rgb_bytes(color1)
    if color2 is not None:
        ext_mask |= RL_CTRL_E_COLOR2
        tail_ext += _rgb_bytes(color2)
    if color3 is not None:
        ext_mask |= RL_CTRL_E_COLOR3
        tail_ext += _rgb_bytes(color3)

    body = bytearray()
    body.append(int(group_id) & 0xFF)
    body.append(int(flags) & 0xFF)

    if ext_mask:
        field_mask |= RL_CTRL_F_EXT
        body.append(field_mask)
        body += tail_main
        body.append(ext_mask)
        body += tail_ext
    else:
        body.append(field_mask)
        body += tail_main

    assert len(body) <= RL_CTRL_BODY_MAX, (
        f"CONTROL body {len(body)} B exceeds BODY_MAX={RL_CTRL_BODY_MAX}"
    )
    return bytes(body)
