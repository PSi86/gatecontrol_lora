"""Host-side evaluator for ``OPC_OFFSET`` formulas.

The wire protocol carries a per-device formula (``mode`` + parameters);
the WLED firmware evaluates it against its own ``current.groupId`` at arm
time. This module mirrors that evaluator on the host so:

* the scene runner can expand a sparse-selection ``groups_offset`` action
  into N per-group ``EXPLICIT`` offsets (one per participating group),
* the UI preview can show the resulting offsets per selected group
  without round-tripping through the gateway.

The C++ counterpart lives in ``RaceLink_WLED/racelink_wled.cpp``
(``computeOffsetMs``); both must produce byte-identical results for any
``(mode, params, group_id)`` triple. Tests in
``tests/test_offset_formula.py`` lock the contract down.

All multi-byte parameters use the same bounds as the wire builder; see
``racelink/protocol/packets.py`` for the canonical limits.
"""

from __future__ import annotations

from typing import Any, Mapping

OFFSET_MS_MIN = 0
OFFSET_MS_MAX = 0xFFFF


def evaluate_offset_ms(spec: Mapping[str, Any], group_id: int) -> int:
    """Evaluate a formula spec for a single ``group_id``.

    ``spec`` is the dict shape persisted on a scene action's
    ``target.offset`` block (see ``scenes_service``). Recognised modes:

    * ``none``     → returns 0.
    * ``explicit`` → returns ``spec["offset_ms"]`` clamped to [0, 65535].
    * ``linear``   → ``base_ms + group_id * step_ms``, clamped.
    * ``vshape``   → ``base_ms + |group_id - center| * step_ms``, clamped.
    * ``modulo``   → ``base_ms + (group_id % cycle) * step_ms``, clamped.
                     ``cycle == 0`` is treated as 1 (mirrors firmware).

    Unknown modes raise ``ValueError`` so a typo in the schema fails loudly.
    """
    mode = (spec.get("mode") or "none").lower()
    gid = int(group_id) & 0xFF

    if mode == "none":
        return 0
    if mode == "explicit":
        return _clamp(int(spec.get("offset_ms", 0)))

    base = int(spec.get("base_ms", 0))
    step = int(spec.get("step_ms", 0))

    if mode == "linear":
        return _clamp(base + gid * step)
    if mode == "vshape":
        center = int(spec.get("center", 0)) & 0xFF
        return _clamp(base + abs(gid - center) * step)
    if mode == "modulo":
        raw_cycle = int(spec.get("cycle", 1))
        cycle = raw_cycle if raw_cycle > 0 else 1
        return _clamp(base + (gid % cycle) * step)

    raise ValueError(f"unknown offset formula mode: {mode!r}")


def evaluate_for_groups(spec: Mapping[str, Any], group_ids) -> list[dict]:
    """Convenience: evaluate a formula across a list of group ids.

    Returns a list of ``{"id": <int>, "offset_ms": <int 0..65535>}`` dicts
    in the same order as the input — handy for building EXPLICIT
    OPC_OFFSET fan-out from a host-side formula.
    """
    return [
        {"id": int(g) & 0xFF, "offset_ms": evaluate_offset_ms(spec, g)}
        for g in group_ids
    ]


def _clamp(v: int) -> int:
    return max(OFFSET_MS_MIN, min(OFFSET_MS_MAX, int(v)))
