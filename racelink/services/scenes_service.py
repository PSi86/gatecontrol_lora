"""Scene store — ordered playlists of typed actions persisted host-side.

A *scene* is a named, ordered sequence of up to 20 actions. Each action is a
typed item drawn from a closed set: dispatchable items (``rl_preset``,
``wled_preset``, ``wled_control``, ``startblock``) plus two control-flow items
(``sync``, ``delay``). The runner (``SceneRunnerService``) plays the list back
in order; simultaneity is achieved by giving multiple dispatchable actions
``arm_on_sync`` flag overrides and inserting an explicit ``sync`` action after
them.

Storage: a single JSON file ``~/.racelink/scenes.json`` written atomically
(temp file + ``os.replace``). Schema is versioned for forward-compat. The
service handles structural validation only — runtime concerns (does the
target group/device still exist?) live in ``SceneRunnerService``.

Mirrors the public shape of :class:`RLPresetsService` so the WebUI and RH
plugin can reuse the same patterns.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from ..domain.flags import USER_FLAG_KEYS

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# ---- action kinds (closed enum) -----------------------------------------

KIND_RL_PRESET = "rl_preset"
KIND_WLED_PRESET = "wled_preset"
KIND_WLED_CONTROL = "wled_control"
KIND_STARTBLOCK = "startblock"
KIND_SYNC = "sync"
KIND_DELAY = "delay"
# offset_group is a *container* action: it sets up offset config for a
# scope of devices, then runs its own list of effect children inside that
# scope. See ``_canonical_offset_group_action`` for the persisted shape.
KIND_OFFSET_GROUP = "offset_group"

ALL_KINDS = (
    KIND_RL_PRESET,
    KIND_WLED_PRESET,
    KIND_WLED_CONTROL,
    KIND_STARTBLOCK,
    KIND_SYNC,
    KIND_DELAY,
    KIND_OFFSET_GROUP,
)

# Kinds that target a single group or device.
KINDS_WITH_TARGET = (
    KIND_RL_PRESET,
    KIND_WLED_PRESET,
    KIND_WLED_CONTROL,
    KIND_STARTBLOCK,
)

# Kinds whose dispatch consumes a flag byte (the four user-intent flags).
# Startblock has no flag concept; sync/delay don't dispatch a packet.
KINDS_WITH_FLAGS = (
    KIND_RL_PRESET,
    KIND_WLED_PRESET,
    KIND_WLED_CONTROL,
)

# Kinds allowed as children inside an ``offset_group`` container. The plan
# explicitly excludes startblock (no offset semantics), sync/delay (top-level
# control flow), and another offset_group (no nesting). ``rl_preset`` is
# included alongside the two literal "CONTROL"/"WLED PRESET" action types so
# legacy ``groups_offset`` actions backed by RL presets migrate cleanly —
# all three emit OPC_PRESET / OPC_CONTROL on the wire and respect the
# OFFSET_MODE acceptance gate.
OFFSET_GROUP_CHILD_KINDS = (
    KIND_RL_PRESET,
    KIND_WLED_PRESET,
    KIND_WLED_CONTROL,
)

MAX_ACTIONS_PER_SCENE = 20
MAX_DELAY_MS = 60_000
GROUP_ID_MAX = 254  # 255 is reserved for broadcast and not a valid scene target

# offset_group: max participating groups per container. 64 keeps per-action
# wire cost under ~3 * 64 = 192 B for OPC_OFFSET fan-out plus the
# OPC_CONTROL bodies, well within practical radio budget.
MAX_GROUPS_OFFSET_ENTRIES = 64
# Max effect children per offset_group container. Plenty for realistic
# scenes (typical operator: 1-3 effects per scope) while keeping the editor
# scrollable.
MAX_OFFSET_GROUP_CHILDREN = 16
OFFSET_MS_MIN = 0
OFFSET_MS_MAX = 0xFFFF  # uint16, must match P_Offset.offsetMs in racelink_proto.h


def get_action_kinds_metadata() -> List[Dict[str, Any]]:
    """Static metadata for the scene-action editor.

    Returns one entry per action ``kind`` with the fields the UI needs to
    render a per-kind sub-form. ``vars`` is the ordered list of variable
    keys that belong on the action (excluding the universal ``target`` and
    ``flags_override`` keys, which the UI renders generically). The actual
    widget options for variables that depend on live state (preset lists)
    are resolved by the API layer at request time, not baked in here.
    """
    return [
        {
            "kind": KIND_RL_PRESET,
            "label": "Apply RL Preset",
            "supports_target": True,
            "supports_flags_override": True,
            "vars": ["presetId", "brightness"],
        },
        {
            "kind": KIND_WLED_PRESET,
            "label": "Apply WLED Preset",
            "supports_target": True,
            "supports_flags_override": True,
            "vars": ["presetId", "brightness"],
        },
        {
            "kind": KIND_WLED_CONTROL,
            "label": "Apply WLED Control (RL preset via OPC_CONTROL)",
            "supports_target": True,
            "supports_flags_override": True,
            "vars": ["presetId", "brightness"],
        },
        {
            "kind": KIND_STARTBLOCK,
            "label": "Startblock Control",
            "supports_target": True,
            "supports_flags_override": False,
            "vars": ["fn_key"],
        },
        {
            "kind": KIND_SYNC,
            "label": "SYNC (fire armed actions)",
            "supports_target": False,
            "supports_flags_override": False,
            "vars": [],
        },
        {
            "kind": KIND_DELAY,
            "label": "Delay",
            "supports_target": False,
            "supports_flags_override": False,
            "vars": ["duration_ms"],
        },
        {
            # Container action: holds its own ``actions`` list (children).
            # Renders very differently from the flat kinds; the editor uses
            # the ``container`` flag to switch templates. Children inherit
            # the offset scope and have a restricted target picker.
            "kind": KIND_OFFSET_GROUP,
            "label": "Offset Group",
            "supports_target": False,
            "supports_flags_override": False,
            "container": True,
            "child_kinds": list(OFFSET_GROUP_CHILD_KINDS),
            "vars": [],
        },
    ]

# ---- helpers ------------------------------------------------------------

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_MAC12_RE = re.compile(r"^[0-9A-F]{12}$")


def _slugify(text: str) -> str:
    base = _SLUG_RE.sub("_", (text or "").strip().lower()).strip("_")
    return base or "scene"


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _coerce_bool(raw: Any, *, default: bool) -> bool:
    """Lenient bool parser for persisted JSON / API request payloads.

    Accepts the canonical Python bools, plus the JSON-y string forms
    (``"true"`` / ``"false"`` case-insensitive) and integers (0/non-zero).
    Falls back to ``default`` for anything else, including missing/None
    keys. Used for the scene-level ``stop_on_error`` field.
    """
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int):
        return raw != 0
    if isinstance(raw, str):
        s = raw.strip().lower()
        if s in ("true", "1", "yes", "on"):
            return True
        if s in ("false", "0", "no", "off"):
            return False
    return default


def _migrate_legacy_target(raw: Any) -> Any:
    """Migrate legacy target shapes to the unified canonical shape.

    Two legacy forms are accepted at the boundary and quietly rewritten;
    everything else passes through for the validator to accept or reject.

    Migrations:
      * ``{"kind": "scope"}`` → ``{"kind": "broadcast"}``
      * ``{"kind": "group", "value": <int>}`` → ``{"kind": "groups", "value": [<int>]}``

    See `docs/reference/broadcast-ruleset.md` for the design rationale —
    "scope" was overloaded with the SSE scope concept, and the singular
    "group" form is just a length-1 special case of the unified "groups".
    """
    if not isinstance(raw, dict):
        return raw
    kind = raw.get("kind")
    if kind == "scope":
        return {"kind": "broadcast"}
    if kind == "group":
        value = raw.get("value")
        try:
            v = int(value)
        except (TypeError, ValueError):
            # Pass through — validator will reject with a clear message.
            return raw
        return {"kind": "groups", "value": [v]}
    return raw


def _canonical_target(raw: Any) -> Dict[str, Any]:
    """Validate an action target in the unified canonical shape.

    Three shapes are accepted post-migration:

      * ``{"kind": "broadcast"}`` — every device (wire: recv3=FFFFFF,
        groupId=255).
      * ``{"kind": "groups", "value": [<int>, ...]}`` — one or more
        specific groups; the runner fans out one packet per group when
        ``len(value) > 1`` (group-scoped broadcast at the wire).
      * ``{"kind": "device", "value": "<12-char MAC>"}`` — single device
        addressed by MAC; the wire emission keeps the device's stored
        ``groupId`` (see the [Single-device pinned rule] in the broadcast
        ruleset doc — groupId=255 is **not** used as a fallback).

    Legacy ``"scope"`` and singular ``"group"`` shapes are migrated by
    :func:`_migrate_legacy_target` before this validator runs.
    """
    raw = _migrate_legacy_target(raw)
    if not isinstance(raw, dict):
        raise ValueError("target must be a dict with 'kind'")
    kind = raw.get("kind")
    if kind == "broadcast":
        return {"kind": "broadcast"}
    if kind == "groups":
        value = raw.get("value")
        if not isinstance(value, list) or not value:
            raise ValueError(
                "target.value for groups must be a non-empty list of int group ids"
            )
        seen: set[int] = set()
        ids: List[int] = []
        for entry in value:
            try:
                v = int(entry)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"target.value[*] for groups must be int: {entry!r}"
                ) from exc
            if v < 0 or v > GROUP_ID_MAX:
                raise ValueError(f"group id {v} out of range [0..{GROUP_ID_MAX}]")
            if v in seen:
                raise ValueError(f"target.value duplicate group id {v}")
            seen.add(v)
            ids.append(v)
        ids.sort()
        return {"kind": "groups", "value": ids}
    if kind == "device":
        value = raw.get("value")
        if not isinstance(value, str):
            raise ValueError("target.value for device must be a 12-char MAC hex string")
        v = value.strip().upper()
        if not _MAC12_RE.match(v):
            raise ValueError(f"invalid device address {value!r}: expected 12-char hex")
        return {"kind": "device", "value": v}
    raise ValueError(
        f"invalid target kind {kind!r} "
        f"(expected 'broadcast', 'groups', or 'device')"
    )


def _canonical_offset_group_container_target(raw: Any) -> Dict[str, Any]:
    """Validate an offset_group **container** target.

    Same shape as :func:`_canonical_target` except ``device`` is invalid —
    an offset_group's offset formula is per-group, so a single-device
    target is meaningless at the container level. Children of the
    container can still use ``device`` targets via
    :func:`_canonical_offset_group_child_target`.
    """
    target = _canonical_target(raw)
    if target["kind"] == "device":
        raise ValueError(
            "offset_group container target cannot be 'device' "
            "(offset is per-group); use 'groups' or 'broadcast'"
        )
    return target


def _canonical_offset_group_child_target(raw: Any, *, parent_groups: Any) -> Dict[str, Any]:
    """Validate the target for a child action *inside* an offset_group container.

    Canonical shapes (same as top-level — see :func:`_canonical_target`):

      * ``{"kind": "broadcast"}`` — every container participant.
      * ``{"kind": "groups", "value": [<int>, ...]}`` — must be a subset
        of the parent's participating groups (skipped when
        ``parent_groups == "all"``).
      * ``{"kind": "device", "value": "<MAC>"}`` — single device; group
        membership is checked at runtime (degraded result on mismatch).

    Legacy ``"scope"`` / singular ``"group"`` shapes are migrated by
    :func:`_migrate_legacy_target`.
    """
    target = _canonical_target(raw)
    if target["kind"] == "groups" and isinstance(parent_groups, list):
        for v in target["value"]:
            if v not in parent_groups:
                raise ValueError(
                    f"child target group {v} is not in the offset_group's "
                    f"participating groups {parent_groups}"
                )
    return target


def _canonical_offset_group_child(raw: Any, *, parent_groups: Any) -> Dict[str, Any]:
    """Validate one child action inside an offset_group container.

    Children are restricted to ``OFFSET_GROUP_CHILD_KINDS`` (the flag-bearing
    effect kinds). Their target picker is filtered to the parent's groups.
    The runner *forces* the OFFSET_MODE flag at dispatch time regardless of
    flags_override; we keep the override block for the other user flags
    (arm_on_sync, force_tt0, force_reapply).
    """
    if not isinstance(raw, dict):
        raise ValueError("offset_group child must be a dict")
    kind = raw.get("kind")
    if kind not in OFFSET_GROUP_CHILD_KINDS:
        raise ValueError(
            f"offset_group child kind {kind!r} not in {OFFSET_GROUP_CHILD_KINDS}"
        )
    if "target" not in raw:
        raise ValueError(f"offset_group child {kind!r} requires a target")
    target = _canonical_offset_group_child_target(
        raw.get("target"), parent_groups=parent_groups,
    )
    out: Dict[str, Any] = {"kind": kind, "target": target}
    params_raw = raw.get("params")
    if params_raw is not None:
        if not isinstance(params_raw, dict):
            raise ValueError("offset_group child params must be a dict or absent")
        out["params"] = dict(params_raw)
    else:
        out["params"] = {}
    out["flags_override"] = _canonical_flags_override(raw.get("flags_override"))
    return out


def _canonical_offset_group_action(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a top-level ``offset_group`` container action.

    Persisted shape::

        {
          "kind": "offset_group",
          "target": {"kind": "broadcast"}
                    | {"kind": "groups", "value": [<int>, ...]},
          "offset": { "mode": ..., ...mode-params },
          "actions": [<child_action>, ...]
        }

    Legacy shape with the standalone ``groups`` field
    (``"all"`` or ``[<int>, ...]``) is migrated to the unified ``target``
    shape on read; see :func:`_migrate_legacy_offset_group_groups_field`.

    Children are validated via :func:`_canonical_offset_group_child` which
    restricts both the action kind (only ``OFFSET_GROUP_CHILD_KINDS``) and
    the target picker (broadcast / groups / device, with group membership
    filtered to the parent's participating groups). Nesting is forbidden —
    a child of kind ``offset_group`` raises here via the kind-check.
    """
    target_raw = raw.get("target")
    if target_raw is None:
        # Legacy: derive `target` from the standalone `groups` field.
        legacy_groups = raw.get("groups")
        if legacy_groups == "all" or legacy_groups == 255:
            target_raw = {"kind": "broadcast"}
        elif isinstance(legacy_groups, list) and legacy_groups:
            target_raw = {"kind": "groups", "value": list(legacy_groups)}
        else:
            raise ValueError(
                'offset_group requires a "target" field; legacy "groups" '
                f'is missing or invalid: {legacy_groups!r}'
            )

    target = _canonical_offset_group_container_target(target_raw)
    target_is_broadcast = (target["kind"] == "broadcast")
    canonical_ids: Optional[List[int]] = (
        None if target_is_broadcast else list(target["value"])
    )

    if not target_is_broadcast and len(canonical_ids or []) > MAX_GROUPS_OFFSET_ENTRIES:
        raise ValueError(
            f"offset_group has {len(canonical_ids)} entries; "
            f"max is {MAX_GROUPS_OFFSET_ENTRIES}"
        )

    offset = _canonical_offset_block(
        raw.get("offset"),
        groups_is_all=target_is_broadcast,
        group_ids=canonical_ids,
    )

    raw_children = raw.get("actions") or []
    if not isinstance(raw_children, list):
        raise ValueError("offset_group.actions must be a list")
    if len(raw_children) > MAX_OFFSET_GROUP_CHILDREN:
        raise ValueError(
            f"offset_group has {len(raw_children)} children; "
            f"max is {MAX_OFFSET_GROUP_CHILDREN}"
        )
    parent_groups_for_children: Any = "all" if target_is_broadcast else canonical_ids
    canonical_children = [
        _canonical_offset_group_child(child, parent_groups=parent_groups_for_children)
        for child in raw_children
    ]

    return {
        "kind": KIND_OFFSET_GROUP,
        "target": target,
        "offset": offset,
        "actions": canonical_children,
    }


OFFSET_FORMULA_MODES = ("none", "explicit", "linear", "vshape", "modulo")
OFFSET_BASE_MIN   = -32768
OFFSET_BASE_MAX   = 32767
OFFSET_STEP_MIN   = -32768
OFFSET_STEP_MAX   = 32767
OFFSET_CYCLE_MIN  = 1
OFFSET_CYCLE_MAX  = 255


def _validate_int(name: str, raw: Any, lo: int, hi: int) -> int:
    try:
        v = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be int") from exc
    if v < lo or v > hi:
        raise ValueError(f"{name} {v} out of [{lo}..{hi}]")
    return v


def _canonical_offset_block(raw: Any, *, groups_is_all: bool,
                            group_ids: Optional[List[int]]) -> Dict[str, Any]:
    """Validate and normalise the ``target.offset`` formula block.

    Shape per mode (mirrors the wire spec in ``racelink_proto.h``):

        {"mode": "none"}
        {"mode": "explicit", "values": [{"id": <int>, "offset_ms": <int>}, ...]}
        {"mode": "linear",   "base_ms": <int>, "step_ms": <int>}
        {"mode": "vshape",   "base_ms": <int>, "step_ms": <int>, "center": <int>}
        {"mode": "modulo",   "base_ms": <int>, "step_ms": <int>, "cycle": <int>}

    ``groups_is_all`` and ``group_ids`` come from the parent target so we
    can reject combinations the runner can't dispatch (``mode=explicit``
    needs a concrete group list to evaluate ``values`` against).
    """
    if not isinstance(raw, dict):
        raise ValueError("groups_offset.offset must be a dict")
    mode = raw.get("mode")
    if mode not in OFFSET_FORMULA_MODES:
        raise ValueError(
            f"groups_offset.offset.mode {mode!r} not in {OFFSET_FORMULA_MODES}"
        )

    if mode == "none":
        return {"mode": "none"}

    if mode == "explicit":
        if groups_is_all or group_ids is None:
            raise ValueError(
                'groups_offset.offset.mode="explicit" requires a concrete groups list'
            )
        raw_values = raw.get("values")
        if not isinstance(raw_values, list) or len(raw_values) != len(group_ids):
            raise ValueError(
                "groups_offset.offset.values must be a list of "
                "{id, offset_ms} entries, one per group"
            )
        seen_ids = set(group_ids)
        canonical_values: List[Dict[str, int]] = []
        for entry in raw_values:
            if not isinstance(entry, dict):
                raise ValueError("groups_offset.offset.values entries must be dicts")
            gid = _validate_int("groups_offset.offset.values[].id",
                                entry.get("id"), 0, GROUP_ID_MAX)
            if gid not in seen_ids:
                raise ValueError(
                    f"groups_offset.offset.values references id {gid} not in groups"
                )
            ms = _validate_int("groups_offset.offset.values[].offset_ms",
                               entry.get("offset_ms"), OFFSET_MS_MIN, OFFSET_MS_MAX)
            canonical_values.append({"id": gid, "offset_ms": ms})
        canonical_values.sort(key=lambda e: e["id"])
        return {"mode": "explicit", "values": canonical_values}

    # Formula modes share base_ms / step_ms.
    base = _validate_int("groups_offset.offset.base_ms",
                         raw.get("base_ms", 0), OFFSET_BASE_MIN, OFFSET_BASE_MAX)
    step = _validate_int("groups_offset.offset.step_ms",
                         raw.get("step_ms", 0), OFFSET_STEP_MIN, OFFSET_STEP_MAX)
    if mode == "linear":
        return {"mode": "linear", "base_ms": base, "step_ms": step}
    if mode == "vshape":
        center = _validate_int("groups_offset.offset.center",
                               raw.get("center", 0), 0, GROUP_ID_MAX)
        return {"mode": "vshape", "base_ms": base, "step_ms": step, "center": center}
    if mode == "modulo":
        cycle = _validate_int("groups_offset.offset.cycle",
                              raw.get("cycle", 1), OFFSET_CYCLE_MIN, OFFSET_CYCLE_MAX)
        return {"mode": "modulo", "base_ms": base, "step_ms": step, "cycle": cycle}

    raise AssertionError(f"unhandled offset mode {mode!r}")


# ===== Legacy ``groups_offset`` migration shim =============================
#
# B6 sunset note (2026-04-27). This shim translates pre-hierarchy
# scene actions (``target.kind == "groups_offset"``) into the
# post-2026-04 ``offset_group`` container shape on load. The project
# is pre-1.0 and no production deployments use the legacy persisted
# format any more — the only consumers are the two regression tests
# in ``tests/test_scenes_service.py`` (``TestLegacyMigration``).
#
# **Removal target: 2026-Q3.** When removed, also delete the matching
# tests, the call site at ``_canonical_action``'s migration check,
# and the ``_is_legacy_groups_offset_target`` /
# ``_migrate_legacy_groups_offset_action`` helpers below.
# ===========================================================================
def _is_legacy_groups_offset_target(raw: Any) -> bool:
    """Detect a persisted action that uses the now-removed
    ``target.kind == "groups_offset"`` shape (any of its sub-shapes).

    These actions are auto-migrated to ``offset_group`` containers before
    validation; see :func:`_migrate_legacy_groups_offset_action`.
    """
    if not isinstance(raw, dict):
        return False
    target = raw.get("target")
    return isinstance(target, dict) and target.get("kind") == "groups_offset"


def _migrate_legacy_groups_offset_action(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Translate a pre-hierarchy ``groups_offset``-target action into the
    new ``offset_group`` container action with a single child.

    Two legacy sub-shapes exist:

    1. **Pre-formula** (oldest):
       ``target.groups = [{id, offset_ms}, ...]`` plus optional ``ui_hints``.
    2. **Post-formula** (just-shipped):
       ``target.groups = "all" | [<int>, ...]`` plus ``target.offset = {mode, ...}``.

    Both collapse into:
    ``{kind: offset_group, groups, offset, actions: [<one child>]}``
    where the single child uses ``target.kind = "scope"`` and inherits the
    original action's effect kind / params / flags_override.
    """
    legacy_target = raw.get("target") or {}

    # Normalise legacy shape (1) into shape (2).
    legacy_groups = legacy_target.get("groups")
    if (
        isinstance(legacy_groups, list)
        and legacy_groups
        and isinstance(legacy_groups[0], dict)
    ):
        # pre-formula: derive groups + offset from the {id, offset_ms} list
        ids = [int(d.get("id")) for d in legacy_groups]
        hints = legacy_target.get("ui_hints") or {}
        if (
            hints.get("mode") == "linear"
            and hints.get("base_ms") is not None
            and hints.get("step_ms") is not None
        ):
            offset = {
                "mode": "linear",
                "base_ms": int(hints["base_ms"]),
                "step_ms": int(hints["step_ms"]),
            }
        else:
            offset = {
                "mode": "explicit",
                "values": [
                    {"id": int(d.get("id")), "offset_ms": int(d.get("offset_ms", 0))}
                    for d in legacy_groups
                ],
            }
        groups: Any = ids
    else:
        # post-formula: pass groups + offset through verbatim
        if legacy_groups == "all" or legacy_groups == 255:
            groups = "all"
        else:
            groups = legacy_groups
        offset = legacy_target.get("offset") or {"mode": "none"}

    # The single child mirrors the legacy effect action. Emit the unified
    # canonical shapes directly (broadcast / groups) so the post-migration
    # dict round-trips through `_canonical_offset_group_action` without
    # any further legacy rewrites.
    child: Dict[str, Any] = {
        "kind": raw.get("kind"),
        "target": {"kind": "broadcast"},
        "params": dict(raw.get("params") or {}),
        "flags_override": dict(raw.get("flags_override") or {}),
    }
    if groups == "all":
        container_target: Dict[str, Any] = {"kind": "broadcast"}
    else:
        container_target = {"kind": "groups", "value": list(groups)}
    return {
        "kind": KIND_OFFSET_GROUP,
        "target": container_target,
        "offset": offset,
        "actions": [child],
    }


def _canonical_flags_override(raw: Any) -> Dict[str, bool]:
    """Keep only explicitly-set USER_FLAG_KEYS booleans; drop unknown keys.

    An empty dict means "no override" — dispatch falls back to the persisted
    preset flags (or default-False for kinds without persisted flags).
    """
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ValueError("flags_override must be a dict or absent")
    out: Dict[str, bool] = {}
    for key in USER_FLAG_KEYS:
        if key in raw:
            out[key] = bool(raw[key])
    return out


def _canonical_action(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("action must be a dict")

    # Migration: persisted actions with the removed ``groups_offset`` target
    # are auto-rewritten to ``offset_group`` containers with a single child
    # before validation. This keeps pre-hierarchy scenes loadable.
    if _is_legacy_groups_offset_target(raw):
        raw = _migrate_legacy_groups_offset_action(raw)

    kind = raw.get("kind")
    if kind not in ALL_KINDS:
        raise ValueError(f"invalid action kind {kind!r}; expected one of {ALL_KINDS}")

    if kind == KIND_OFFSET_GROUP:
        # Stray top-level fields are not tolerated on container actions —
        # the children carry their own params/flags. ``target`` IS valid on
        # the container in the unified shape (see
        # :func:`_canonical_offset_group_container_target`); only the legacy
        # ``groups`` field is also accepted as input and rewritten to
        # ``target`` on the canonical output.
        for stray in ("params", "flags_override", "duration_ms"):
            if stray in raw:
                raise ValueError(f"offset_group action must not carry '{stray}'")
        return _canonical_offset_group_action(raw)

    if kind == KIND_DELAY:
        try:
            dur = int(raw.get("duration_ms", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("delay.duration_ms must be int") from exc
        if dur < 0 or dur > MAX_DELAY_MS:
            raise ValueError(f"delay duration_ms {dur} out of [0..{MAX_DELAY_MS}]")
        # Reject stray fields to keep the shape strict.
        for stray in ("target", "params", "flags_override"):
            if stray in raw:
                raise ValueError(f"delay action must not carry '{stray}'")
        return {"kind": KIND_DELAY, "duration_ms": dur}

    if kind == KIND_SYNC:
        for stray in ("target", "params", "flags_override", "duration_ms"):
            if stray in raw:
                raise ValueError(f"sync action must not carry '{stray}'")
        return {"kind": KIND_SYNC}

    # KINDS_WITH_TARGET (rl_preset / wled_preset / wled_control / startblock)
    if "target" not in raw:
        raise ValueError(f"action kind {kind!r} requires a 'target'")
    target = _canonical_target(raw.get("target"))

    out: Dict[str, Any] = {"kind": kind, "target": target}

    params_raw = raw.get("params")
    if params_raw is not None:
        if not isinstance(params_raw, dict):
            raise ValueError("params must be a dict or absent")
        # Shallow copy; deep validation per-kind happens at dispatch time
        # (SpecialsService.coerce_action_params on the runner side).
        out["params"] = dict(params_raw)
    else:
        out["params"] = {}

    if kind in KINDS_WITH_FLAGS:
        out["flags_override"] = _canonical_flags_override(raw.get("flags_override"))
    elif "flags_override" in raw:
        # Allow but ignore for forward-compat — startblock could grow flags
        # later. Strict reject would break the cache after such a change.
        logger.debug("scenes: ignoring flags_override on action kind %r", kind)
    return out


def _canonical_actions(raw: Any) -> List[Dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("actions must be a list")
    if len(raw) > MAX_ACTIONS_PER_SCENE:
        raise ValueError(f"too many actions: {len(raw)} > {MAX_ACTIONS_PER_SCENE}")
    return [_canonical_action(item) for item in raw]


def _collapse_groups_target(target: Dict[str, Any],
                            known_set: set) -> Dict[str, Any]:
    """If ``target.kind == "groups"`` and the value covers every currently
    known group id, rewrite to ``{"kind": "broadcast"}``.

    No-op for ``broadcast`` / ``device`` targets and for ``groups`` lists
    that are a strict subset. The collapse makes the runtime / estimator
    pair unambiguous (both pick optimizer Strategy A) and matches the
    operator's intent: "I selected every group" means broadcast, including
    groups added later. See `docs/reference/broadcast-ruleset.md`.
    """
    if not isinstance(target, dict) or target.get("kind") != "groups":
        return target
    value = target.get("value")
    if not isinstance(value, list) or not value:
        return target
    if known_set and set(value).issuperset(known_set):
        return {"kind": "broadcast"}
    return target


def collapse_actions_to_broadcast(
    actions: List[Dict[str, Any]],
    known_group_ids: List[int],
) -> List[Dict[str, Any]]:
    """Walk a list of canonical actions and apply the
    "selected-equals-known-all → broadcast" collapse to every target.

    Returns a new list when any action was rewritten, otherwise returns
    ``actions`` unchanged. Touches:
      * top-level action ``target`` (any kind that supports targets);
      * offset_group container ``target``;
      * offset_group child ``target``.
    """
    if not known_group_ids:
        return actions
    known_set = set(known_group_ids)
    new_list: List[Dict[str, Any]] = []
    any_changed = False
    for action in actions:
        new_action, changed = _collapse_action(action, known_set)
        if changed:
            any_changed = True
        new_list.append(new_action)
    return new_list if any_changed else actions


def _collapse_action(action: Dict[str, Any],
                      known_set: set) -> tuple[Dict[str, Any], bool]:
    """Apply the broadcast collapse to one action. Returns ``(new_action,
    changed)``. Recurses into offset_group children."""
    changed = False
    new_action = action

    target = action.get("target")
    if isinstance(target, dict):
        new_target = _collapse_groups_target(target, known_set)
        if new_target is not target:
            new_action = dict(new_action)
            new_action["target"] = new_target
            changed = True

    if action.get("kind") == KIND_OFFSET_GROUP:
        children = action.get("actions") or []
        new_children: List[Dict[str, Any]] = []
        children_changed = False
        for child in children:
            nc, c_changed = _collapse_action(child, known_set)
            if c_changed:
                children_changed = True
            new_children.append(nc)
        if children_changed:
            if new_action is action:
                new_action = dict(action)
            new_action["actions"] = new_children
            changed = True

    return new_action, changed


# ---- service -------------------------------------------------------------


class SceneService:
    """CRUD for scenes (``~/.racelink/scenes.json``).

    Public shape mirrors :class:`RLPresetsService` so the wiring patterns
    (``on_changed`` callback, ``state_scope.SCENES`` SSE refresh, etc.) carry
    over unchanged.
    """

    def __init__(self, *, storage_path: Optional[str] = None,
                 known_group_ids_getter: Optional[Callable[[], List[int]]] = None):
        self._path = storage_path or os.path.join(
            os.path.expanduser("~"), ".racelink", "scenes.json"
        )
        self._lock = threading.RLock()
        self._cache: Optional[List[dict]] = None
        # Monotone-increasing scene id counter; persisted across writes so
        # ids never recycle (keeps RH bindings stable through delete+create).
        self._next_id: Optional[int] = None
        self.on_changed: Optional[Callable[[], None]] = None
        # Optional callable returning the currently-known group ids. When
        # set, save-time canonicalisation collapses ``target.kind="groups"``
        # whose value covers every known group → ``"broadcast"`` so the
        # editor and runner agree on optimizer Strategy A. Tests / standalone
        # CRUD don't need this.
        self._known_group_ids_getter = known_group_ids_getter

    # ---- paths / persistence --------------------------------------------

    @property
    def path(self) -> str:
        return self._path

    def _ensure_dir(self) -> None:
        os.makedirs(os.path.dirname(self._path), exist_ok=True)

    def _fire_changed(self) -> None:
        cb = self.on_changed
        if cb is None:
            return
        try:
            cb()
        except Exception:
            # swallow-ok: listener crash must not undo a persisted write
            logger.exception("scenes: on_changed listener raised")

    def _load(self) -> List[dict]:
        if not os.path.isfile(self._path):
            self._next_id = 0
            return []
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("scenes: failed to load %s (%s); starting empty", self._path, exc)
            self._next_id = 0
            return []
        if not isinstance(data, dict):
            logger.warning("scenes: %s is not a dict; ignoring", self._path)
            self._next_id = 0
            return []
        schema = int(data.get("schema_version", 0) or 0)
        if schema > SCHEMA_VERSION:
            logger.warning(
                "scenes: schema_version=%s newer than expected %s; loading best-effort",
                schema, SCHEMA_VERSION,
            )
        raw_scenes = data.get("scenes")
        if not isinstance(raw_scenes, list):
            self._next_id = 0
            return []

        out: List[dict] = []
        used_ids: set[int] = set()
        for index, entry in enumerate(raw_scenes):
            if not isinstance(entry, dict):
                continue
            key = entry.get("key")
            label = entry.get("label")
            if not isinstance(key, str) or not isinstance(label, str):
                continue
            raw_id = entry.get("id")
            try:
                scene_id = int(raw_id) if raw_id is not None else index
            except (TypeError, ValueError):
                scene_id = index
            while scene_id in used_ids:
                scene_id += 1
            used_ids.add(scene_id)
            try:
                actions = _canonical_actions(entry.get("actions"))
            except ValueError as exc:
                logger.warning(
                    "scenes: dropping malformed actions in scene %r: %s",
                    key, exc,
                )
                actions = []
            out.append({
                "id": scene_id,
                "key": key,
                "label": label,
                "created": entry.get("created") or _now_iso(),
                "updated": entry.get("updated") or entry.get("created") or _now_iso(),
                "actions": actions,
                # Batch A (2026-04-28): stop_on_error gates the runner's
                # behaviour after an action fails. Default True is the
                # conservative choice — a half-failed sequence usually
                # leaves devices in a state that doesn't match operator
                # intent, so further sends just waste air-time. Operators
                # who want the legacy "play through every action" behaviour
                # uncheck the editor checkbox per scene. Existing scenes
                # without the field default to True on load.
                "stop_on_error": _coerce_bool(entry.get("stop_on_error"), default=True),
            })

        persisted_next = data.get("next_id")
        if isinstance(persisted_next, int) and persisted_next > (max(used_ids) if used_ids else -1):
            self._next_id = persisted_next
        else:
            self._next_id = (max(used_ids) + 1) if used_ids else 0
        return out

    def _write_atomic(self, scenes: List[dict]) -> None:
        self._ensure_dir()
        next_id = self._next_id if self._next_id is not None else (
            (max((int(s["id"]) for s in scenes if "id" in s), default=-1) + 1)
        )
        payload = {
            "schema_version": SCHEMA_VERSION,
            "next_id": int(next_id),
            "scenes": scenes,
        }
        tmp = f"{self._path}.tmp.{os.getpid()}.{int(time.time()*1000)}"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp, self._path)

    def _items(self) -> List[dict]:
        if self._cache is None:
            self._cache = self._load()
        return self._cache

    def _invalidate(self) -> None:
        self._cache = None

    # ---- public read API -------------------------------------------------

    def list(self) -> List[dict]:
        with self._lock:
            return [_clone_scene(s) for s in self._items()]

    def get(self, key: str) -> Optional[dict]:
        with self._lock:
            for scene in self._items():
                if scene["key"] == key:
                    return _clone_scene(scene)
            return None

    def get_by_id(self, scene_id: int) -> Optional[dict]:
        try:
            sid = int(scene_id)
        except (TypeError, ValueError):
            return None
        with self._lock:
            for scene in self._items():
                if int(scene.get("id", -1)) == sid:
                    return _clone_scene(scene)
            return None

    # ---- mutation helpers -----------------------------------------------

    def _unique_key(self, desired: str, existing: set, *, exclude_key: Optional[str] = None) -> str:
        taken = set(existing)
        if exclude_key and exclude_key in taken:
            taken.discard(exclude_key)
        if desired not in taken:
            return desired
        for idx in range(2, 1000):
            candidate = f"{desired}_{idx}"
            if candidate not in taken:
                return candidate
        raise RuntimeError(f"could not derive a unique key from {desired!r}")

    # ---- public write API -----------------------------------------------

    def _apply_broadcast_collapse(
        self, actions: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Apply the save-time "selected-equals-known-all → broadcast"
        canonicalisation. No-op when no group-id getter was injected (test
        / standalone CRUD path) or it raises / returns empty.
        """
        getter = self._known_group_ids_getter
        if getter is None:
            return actions
        try:
            known = list(getter() or [])
        except Exception:  # pragma: no cover - getter is operator-supplied
            logger.debug(
                "scenes: known_group_ids_getter raised; "
                "skipping broadcast collapse",
                exc_info=True,
            )
            return actions
        return collapse_actions_to_broadcast(actions, known)

    def create(self, *, label: str, actions: Optional[list] = None,
               key: Optional[str] = None,
               stop_on_error: Optional[bool] = None) -> dict:
        label_clean = (label or "").strip()
        if not label_clean:
            raise ValueError("label is required")
        canonical_actions = self._apply_broadcast_collapse(
            _canonical_actions(actions)
        )
        # Default True if the caller didn't specify. None vs False is
        # meaningful: None -> use default; False -> explicit opt-out.
        stop_on_error_resolved = (
            True if stop_on_error is None else _coerce_bool(stop_on_error, default=True)
        )
        with self._lock:
            items = self._items()
            existing_keys = {s["key"] for s in items}
            desired = _slugify(key or label_clean)
            final_key = self._unique_key(desired, existing_keys)
            now = _now_iso()
            new_id = int(self._next_id or 0)
            self._next_id = new_id + 1
            scene = {
                "id": new_id,
                "key": final_key,
                "label": label_clean,
                "created": now,
                "updated": now,
                "actions": canonical_actions,
                "stop_on_error": stop_on_error_resolved,
            }
            items.append(scene)
            self._write_atomic(items)
            self._invalidate()
        self._fire_changed()
        return _clone_scene(scene)

    def update(self, key: str, *, label: Optional[str] = None,
               actions: Optional[list] = None,
               stop_on_error: Optional[bool] = None) -> Optional[dict]:
        if actions is not None:
            canonical_actions = self._apply_broadcast_collapse(
                _canonical_actions(actions)
            )
        else:
            canonical_actions = None
        updated: Optional[dict] = None
        with self._lock:
            items = list(self._items())
            for idx, scene in enumerate(items):
                if scene["key"] != key:
                    continue
                new_entry = dict(scene)
                if label is not None:
                    label_clean = label.strip()
                    if not label_clean:
                        raise ValueError("label must not be empty")
                    new_entry["label"] = label_clean
                if canonical_actions is not None:
                    new_entry["actions"] = canonical_actions
                if stop_on_error is not None:
                    new_entry["stop_on_error"] = _coerce_bool(
                        stop_on_error, default=True,
                    )
                new_entry["updated"] = _now_iso()
                items[idx] = new_entry
                self._write_atomic(items)
                self._invalidate()
                updated = new_entry
                break
        if updated is None:
            return None
        self._fire_changed()
        return _clone_scene(updated)

    def delete(self, key: str) -> bool:
        with self._lock:
            items = [s for s in self._items() if s["key"] != key]
            if len(items) == len(self._items()):
                return False
            self._write_atomic(items)
            self._invalidate()
        self._fire_changed()
        return True

    def duplicate(self, key: str, *, new_label: Optional[str] = None) -> Optional[dict]:
        src = self.get(key)
        if src is None:
            return None
        label = (new_label or f"{src['label']} copy").strip()
        return self.create(
            label=label,
            actions=src["actions"],
            stop_on_error=src.get("stop_on_error", True),
        )

    def renumber_group_references(self, deleted_gid: int) -> int:
        """Rewrite every scene's group references after a group deletion.

        When a group at index ``deleted_gid`` is removed, every device
        and every persisted reference to a higher-indexed group must
        shift down by 1. This method handles the *scene* side of that
        bookkeeping (the API route handles the *device* side).

        Per-reference rule:

        * ``value == deleted_gid`` → 0  (the device is now in
          Unconfigured; the scene action's target follows it).
        * ``value > deleted_gid``  → ``value - 1`` (the higher-indexed
          group's id shifted down by one).
        * ``value < deleted_gid``  → unchanged.

        Touches every group reference in every action: top-level
        ``target.value`` for ``kind == "group"``, ``offset_group``
        container ``groups`` lists, and ``offset_group`` child
        ``target.value`` for ``kind == "group"``. ``"all"`` and
        ``"scope"`` references aren't tied to a specific id and
        pass through untouched.

        Returns the number of scenes whose actions were modified
        (caller can surface this in the operator-facing response).
        """
        changed = 0
        with self._lock:
            items = list(self._items())
            new_items = []
            for scene in items:
                new_actions, was_changed = _renumber_actions_for_deleted_group(
                    scene["actions"], deleted_gid,
                )
                if was_changed:
                    changed += 1
                    # Re-canonicalise so the stored order matches what
                    # the validator emits on a fresh load (the
                    # offset_group ``groups`` list is sorted; the
                    # explicit-mode ``values`` list is sorted by id).
                    # Without this the in-memory cache would diverge
                    # from the post-load state.
                    new_entry = dict(scene)
                    new_entry["actions"] = _canonical_actions(new_actions)
                    new_entry["updated"] = _now_iso()
                    new_items.append(new_entry)
                else:
                    new_items.append(scene)
            if changed:
                self._write_atomic(new_items)
                self._invalidate()
        if changed:
            self._fire_changed()
        return changed

    def replace_all(self, scenes: List[dict]) -> None:
        """Bulk replace (used by tests / future import flows). Assigns fresh
        monotonic ids so ids never accidentally recycle."""
        with self._lock:
            self._items()  # ensure _next_id is seeded
            canonical = []
            seen_keys: set[str] = set()
            for entry in scenes:
                key = _slugify(entry.get("key") or entry.get("label") or "")
                if key in seen_keys:
                    key = self._unique_key(key, seen_keys)
                seen_keys.add(key)
                new_id = int(self._next_id or 0)
                self._next_id = new_id + 1
                canonical.append({
                    "id": new_id,
                    "key": key,
                    "label": (entry.get("label") or key).strip(),
                    # NOTE: replace_all is bulk-import; we don't apply the
                    # save-time broadcast collapse here because the caller
                    # owns the canonical shape (e.g. an export round-trip).
                    # _canonical_actions still migrates legacy shapes.
                    "created": entry.get("created") or _now_iso(),
                    "updated": entry.get("updated") or _now_iso(),
                    "actions": _canonical_actions(entry.get("actions")),
                })
            self._write_atomic(canonical)
            self._invalidate()
        self._fire_changed()


def _shift_group_value(value: int, deleted_gid: int) -> int:
    """Apply the post-deletion shift rule to a single group id.

    * value == deleted_gid → 0 (the group is gone; references collapse to Unconfigured)
    * value >  deleted_gid → value - 1 (higher indices shifted down)
    * value <  deleted_gid → unchanged
    """
    if value == deleted_gid:
        return 0
    if value > deleted_gid:
        return value - 1
    return value


def _renumber_actions_for_deleted_group(
    actions: List[Dict[str, Any]],
    deleted_gid: int,
) -> tuple[List[Dict[str, Any]], bool]:
    """Walk a scene's actions and rewrite every group reference per
    :func:`_shift_group_value`. Returns ``(new_actions, changed)``.

    ``new_actions`` is a defensive deep-copy when ``changed`` is True,
    or the original list reference when no rewrite happened (so the
    caller can short-circuit the persist write).
    """
    changed = False
    new_actions: List[Dict[str, Any]] = []
    for action in actions:
        new_action, action_changed = _renumber_action(action, deleted_gid)
        if action_changed:
            changed = True
        new_actions.append(new_action)
    return (new_actions if changed else actions), changed


def _shift_target_groups_list(
    target: Dict[str, Any], deleted_gid: int,
) -> tuple[Dict[str, Any], bool]:
    """Apply :func:`_shift_group_value` to every id in a
    ``target.kind == "groups"`` list, deduping any collapses to 0.

    Returns ``(new_target, changed)`` where ``changed`` is False when
    nothing moved (caller can short-circuit). The returned target is a
    fresh dict only when changed.
    """
    if target.get("kind") != "groups":
        return target, False
    raw_value = target.get("value")
    if not isinstance(raw_value, list):
        return target, False
    shifted_list = [_shift_group_value(int(g), deleted_gid) for g in raw_value]
    seen: set[int] = set()
    deduped: List[int] = []
    for g in shifted_list:
        if g not in seen:
            seen.add(g)
            deduped.append(g)
    if deduped == list(raw_value):
        return target, False
    new_target = dict(target)
    new_target["value"] = deduped
    return new_target, True


def _renumber_action(
    action: Dict[str, Any],
    deleted_gid: int,
) -> tuple[Dict[str, Any], bool]:
    """Rewrite group references in one action. See
    :func:`_renumber_actions_for_deleted_group` for the contract.

    Touches every group reference in the unified target shape:
      * top-level ``target`` with ``kind == "groups"``;
      * offset_group container ``target`` with ``kind == "groups"``;
      * offset_group child ``target.kind == "groups"`` (recursed via
        :func:`_renumber_actions_for_deleted_group`);
      * offset_group ``offset.values[].id`` (explicit mode).

    ``broadcast`` and ``device`` targets aren't tied to a specific group
    id and pass through untouched.
    """
    kind = action.get("kind")
    changed = False

    # Top-level / offset_group container target: kind == "groups" with
    # list value. (broadcast / device targets are id-agnostic.)
    target = action.get("target")
    new_target = target
    if isinstance(target, dict):
        new_target, t_changed = _shift_target_groups_list(target, deleted_gid)
        if t_changed:
            changed = True

    # offset_group child actions recurse via the same logic — children
    # are themselves actions with their own ``target``.
    new_children = action.get("actions")
    if kind == KIND_OFFSET_GROUP and isinstance(new_children, list):
        rewritten_children, children_changed = _renumber_actions_for_deleted_group(
            new_children, deleted_gid,
        )
        if children_changed:
            new_children = rewritten_children
            changed = True

    # offset.values entries (per-group offset_ms map) — same shift rule
    # on each ``id``. Drop the entry when its id collapses to 0 (unless
    # 0 wasn't already present), to keep the explicit-mode list valid.
    offset_block = action.get("offset")
    new_offset = offset_block
    if (
        kind == KIND_OFFSET_GROUP
        and isinstance(offset_block, dict)
        and offset_block.get("mode") == "explicit"
        and isinstance(offset_block.get("values"), list)
    ):
        rewritten_values: List[Dict[str, Any]] = []
        seen_ids: set[int] = set()
        offset_changed = False
        for entry in offset_block["values"]:
            try:
                old = int(entry.get("id"))
            except (TypeError, ValueError):
                rewritten_values.append(entry)
                continue
            shifted = _shift_group_value(old, deleted_gid)
            if shifted != old:
                offset_changed = True
            if shifted in seen_ids:
                offset_changed = True
                continue
            seen_ids.add(shifted)
            new_entry = dict(entry)
            new_entry["id"] = shifted
            rewritten_values.append(new_entry)
        if offset_changed:
            new_offset = dict(offset_block)
            new_offset["values"] = rewritten_values
            changed = True

    if not changed:
        return action, False

    new_action = dict(action)
    if new_target is not action.get("target"):
        new_action["target"] = new_target
    if kind == KIND_OFFSET_GROUP:
        if new_children is not action.get("actions"):
            new_action["actions"] = new_children
        if new_offset is not action.get("offset"):
            new_action["offset"] = new_offset
    return new_action, True


def _clone_scene(scene: dict) -> dict:
    """Return a defensive shallow-deep copy: scene dict + nested actions list
    + per-action shallow copies. Callers can iterate / mutate freely."""
    return {
        "id": scene["id"],
        "key": scene["key"],
        "label": scene["label"],
        "created": scene["created"],
        "updated": scene["updated"],
        "actions": [_clone_action(a) for a in scene.get("actions") or []],
        # Default True so a clone of a legacy scene (loaded before the
        # field existed) carries the safer abort-on-error behaviour.
        "stop_on_error": bool(scene.get("stop_on_error", True)),
    }


def _clone_action(action: dict) -> dict:
    """Defensive copy of an action. Handles all kinds, including the
    ``offset_group`` container which carries its own children list."""
    kind = action["kind"]
    out: Dict[str, Any] = {"kind": kind}
    if kind == KIND_DELAY:
        out["duration_ms"] = action["duration_ms"]
        return out
    if kind == KIND_SYNC:
        return out
    if kind == KIND_OFFSET_GROUP:
        out["target"] = _clone_target(action["target"])
        out["offset"] = _clone_offset_block(action.get("offset") or {})
        out["actions"] = [
            _clone_action(child) for child in action.get("actions") or []
        ]
        return out
    out["target"] = _clone_target(action["target"])
    out["params"] = dict(action.get("params") or {})
    if "flags_override" in action:
        out["flags_override"] = dict(action["flags_override"])
    return out


def _clone_offset_block(offset: Dict[str, Any]) -> Dict[str, Any]:
    """Deep-copy the nested ``offset`` block for a container action — the
    only nested mutable field is ``values`` (explicit-mode list)."""
    cloned: Dict[str, Any] = dict(offset)
    if "values" in cloned and isinstance(cloned["values"], list):
        cloned["values"] = [dict(v) for v in cloned["values"]]
    return cloned


def _clone_target(target: Dict[str, Any]) -> Dict[str, Any]:
    """Defensive copy of an action target in the unified canonical shape.

    Handles ``{"kind": "broadcast"}`` (no value), ``{"kind": "groups",
    "value": [...]}`` (deep-copy the list so the caller can mutate
    freely), and ``{"kind": "device", "value": "<MAC>"}`` (string value
    is immutable; shallow copy is enough).
    """
    out: Dict[str, Any] = dict(target)
    if isinstance(out.get("value"), list):
        out["value"] = list(out["value"])
    return out
