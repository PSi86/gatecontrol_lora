"""Canonical RaceLink flag definitions.

The six behaviour flags are carried at the same bit positions in both
``OPC_PRESET`` (0x04) and ``OPC_CONTROL`` (0x08) packets. This module is
the single source of truth for:

- the bit values (matching ``RACELINK_FLAG_*`` in the WLED usermod),
- which flags the user may persist on an RL preset
  (``USER_FLAG_KEYS``; the two derived flags POWER_ON / HAS_BRI are
  computed host-side from brightness and are excluded),
- the wire-byte builder used by every emission path
  (``build_flags_byte``),
- a loose-dict normalizer used by persistence + ``params`` input paths
  (``flags_from_mapping``).
"""

from __future__ import annotations

from typing import Any, Mapping

RL_FLAG_POWER_ON = 0x01
RL_FLAG_ARM_ON_SYNC = 0x02
RL_FLAG_HAS_BRI = 0x04
RL_FLAG_FORCE_TT0 = 0x08
RL_FLAG_FORCE_REAPPLY = 0x10
RL_FLAG_OFFSET_MODE = 0x20

FLAG_BITS: dict[str, int] = {
    "power_on":       RL_FLAG_POWER_ON,
    "arm_on_sync":    RL_FLAG_ARM_ON_SYNC,
    "has_bri":        RL_FLAG_HAS_BRI,
    "force_tt0":      RL_FLAG_FORCE_TT0,
    "force_reapply":  RL_FLAG_FORCE_REAPPLY,
    "offset_mode":    RL_FLAG_OFFSET_MODE,
}

# The four user-intent flags that are persisted on an RL preset and
# surfaced in the editor UI. POWER_ON and HAS_BRI are derived host-side
# from the brightness value and never stored.
USER_FLAG_KEYS: tuple[str, ...] = (
    "arm_on_sync",
    "force_tt0",
    "force_reapply",
    "offset_mode",
)


def build_flags_byte(
    *,
    power_on: bool = False,
    has_bri: bool = False,
    arm_on_sync: bool = False,
    force_tt0: bool = False,
    force_reapply: bool = False,
    offset_mode: bool = False,
) -> int:
    """Compose the 1-byte flags value used by OPC_PRESET and OPC_CONTROL.

    Identical for both opcodes by protocol contract — bit positions are
    fixed in ``racelink_proto.h``. Extra bits (6, 7) are reserved and
    left unset.
    """
    out = 0
    if power_on:      out |= RL_FLAG_POWER_ON
    if arm_on_sync:   out |= RL_FLAG_ARM_ON_SYNC
    if has_bri:       out |= RL_FLAG_HAS_BRI
    if force_tt0:     out |= RL_FLAG_FORCE_TT0
    if force_reapply: out |= RL_FLAG_FORCE_REAPPLY
    if offset_mode:   out |= RL_FLAG_OFFSET_MODE
    return out & 0xFF


def flags_from_mapping(src: Mapping[str, Any] | None) -> dict[str, bool]:
    """Normalize the four user-intent flags from a loose mapping.

    Unknown keys are ignored. Missing keys default to ``False``. Values
    are coerced to ``bool`` (accepts ``0``/``1``/``"true"`` etc. — the
    JSON shape on disk is strictly bool, but this is forgiving toward
    REST clients).
    """
    src = src or {}
    return {k: bool(src.get(k, False)) for k in USER_FLAG_KEYS}


__all__ = [
    "RL_FLAG_POWER_ON",
    "RL_FLAG_ARM_ON_SYNC",
    "RL_FLAG_HAS_BRI",
    "RL_FLAG_FORCE_TT0",
    "RL_FLAG_FORCE_REAPPLY",
    "RL_FLAG_OFFSET_MODE",
    "FLAG_BITS",
    "USER_FLAG_KEYS",
    "build_flags_byte",
    "flags_from_mapping",
]
