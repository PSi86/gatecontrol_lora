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


def _canonical_target(raw: Any) -> Dict[str, Any]:
    """Validate a *top-level* action target: ``group`` or ``device``.

    The legacy ``groups_offset`` target kind was removed when offset playback
    moved into a dedicated ``offset_group`` container action. Persisted
    scenes with the old shape are auto-migrated by ``_migrate_legacy_action``
    before they reach this validator.
    """
    if not isinstance(raw, dict):
        raise ValueError("target must be a dict with 'kind' and 'value'")
    kind = raw.get("kind")
    if kind == "group":
        value = raw.get("value")
        try:
            v = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"target.value for group must be int: {value!r}") from exc
        if v < 0 or v > GROUP_ID_MAX:
            raise ValueError(f"group id {v} out of range [0..{GROUP_ID_MAX}]")
        return {"kind": "group", "value": v}
    if kind == "device":
        value = raw.get("value")
        if not isinstance(value, str):
            raise ValueError("target.value for device must be a 12-char MAC hex string")
        v = value.strip().upper()
        if not _MAC12_RE.match(v):
            raise ValueError(f"invalid device address {value!r}: expected 12-char hex")
        return {"kind": "device", "value": v}
    raise ValueError(
        f"invalid target kind {kind!r} (expected 'group' or 'device')"
    )


def _canonical_offset_group_child_target(raw: Any, *, parent_groups: Any) -> Dict[str, Any]:
    """Validate the target for a child action *inside* an offset_group container.

    Three shapes:
      * ``{"kind": "scope"}`` — broadcast to every container participant.
      * ``{"kind": "group", "value": <int>}`` — single group out of
        ``parent_groups`` (or any group when ``parent_groups == "all"``).
      * ``{"kind": "device", "value": "<MAC>"}`` — single device. Membership
        in the parent's groups is checked at runtime (degraded result on
        mismatch); only format is validated here.

    Scope is the default and the cheapest wire path: one OPC_CONTROL with
    groupId=255 reaches every offset-configured device via the wire-level
    acceptance gate.
    """
    if not isinstance(raw, dict):
        raise ValueError("offset_group child target must be a dict with a 'kind'")
    kind = raw.get("kind")
    if kind == "scope":
        return {"kind": "scope"}
    if kind == "group":
        value = raw.get("value")
        try:
            v = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"child target.value for group must be int: {value!r}"
            ) from exc
        if v < 0 or v > GROUP_ID_MAX:
            raise ValueError(f"group id {v} out of range [0..{GROUP_ID_MAX}]")
        if isinstance(parent_groups, list) and v not in parent_groups:
            raise ValueError(
                f"child target group {v} is not in the offset_group's "
                f"participating groups {parent_groups}"
            )
        return {"kind": "group", "value": v}
    if kind == "device":
        value = raw.get("value")
        if not isinstance(value, str):
            raise ValueError("child target.value for device must be a 12-char MAC")
        mac = value.strip().upper()
        if not _MAC12_RE.match(mac):
            raise ValueError(f"invalid device address {value!r}: expected 12-char hex")
        return {"kind": "device", "value": mac}
    raise ValueError(
        f"invalid offset_group child target kind {kind!r} "
        f"(expected 'scope', 'group', or 'device')"
    )


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
          "groups": "all" | [<int 0..254>, ...],
          "offset": { "mode": ..., ...mode-params },
          "actions": [<child_action>, ...]
        }

    Children are validated via ``_canonical_offset_group_child`` which
    restricts both the action kind (only OFFSET_GROUP_CHILD_KINDS) and the
    target picker (scope/group/device, with group membership filtered to the
    parent's participating groups). Nesting is forbidden — a child of kind
    ``offset_group`` raises here via the kind-check.
    """
    raw_groups = raw.get("groups")
    groups_is_all = False
    canonical_ids: Optional[List[int]] = None

    if raw_groups == "all" or raw_groups == 255:
        groups_is_all = True
    elif isinstance(raw_groups, list) and raw_groups:
        if len(raw_groups) > MAX_GROUPS_OFFSET_ENTRIES:
            raise ValueError(
                f"offset_group has {len(raw_groups)} entries; "
                f"max is {MAX_GROUPS_OFFSET_ENTRIES}"
            )
        seen_ids: set[int] = set()
        ids: List[int] = []
        for entry in raw_groups:
            gid = _validate_int("offset_group.groups[]", entry, 0, GROUP_ID_MAX)
            if gid in seen_ids:
                raise ValueError(f"offset_group duplicate group id {gid}")
            seen_ids.add(gid)
            ids.append(gid)
        ids.sort()
        canonical_ids = ids
    else:
        raise ValueError(
            'offset_group.groups must be "all" or a non-empty list of group ids'
        )

    offset = _canonical_offset_block(
        raw.get("offset"),
        groups_is_all=groups_is_all,
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
    parent_groups_for_children: Any = "all" if groups_is_all else canonical_ids
    canonical_children = [
        _canonical_offset_group_child(child, parent_groups=parent_groups_for_children)
        for child in raw_children
    ]

    out: Dict[str, Any] = {
        "kind": KIND_OFFSET_GROUP,
        "groups": "all" if groups_is_all else canonical_ids,
        "offset": offset,
        "actions": canonical_children,
    }
    return out


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

    # The single child mirrors the legacy effect action.
    child: Dict[str, Any] = {
        "kind": raw.get("kind"),
        "target": {"kind": "scope"},
        "params": dict(raw.get("params") or {}),
        "flags_override": dict(raw.get("flags_override") or {}),
    }
    return {
        "kind": KIND_OFFSET_GROUP,
        "groups": groups,
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
        # the children carry their own targets/params.
        for stray in ("target", "params", "flags_override", "duration_ms"):
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


# ---- service -------------------------------------------------------------


class SceneService:
    """CRUD for scenes (``~/.racelink/scenes.json``).

    Public shape mirrors :class:`RLPresetsService` so the wiring patterns
    (``on_changed`` callback, ``state_scope.SCENES`` SSE refresh, etc.) carry
    over unchanged.
    """

    def __init__(self, *, storage_path: Optional[str] = None):
        self._path = storage_path or os.path.join(
            os.path.expanduser("~"), ".racelink", "scenes.json"
        )
        self._lock = threading.RLock()
        self._cache: Optional[List[dict]] = None
        # Monotone-increasing scene id counter; persisted across writes so
        # ids never recycle (keeps RH bindings stable through delete+create).
        self._next_id: Optional[int] = None
        self.on_changed: Optional[Callable[[], None]] = None

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

    def create(self, *, label: str, actions: Optional[list] = None,
               key: Optional[str] = None) -> dict:
        label_clean = (label or "").strip()
        if not label_clean:
            raise ValueError("label is required")
        canonical_actions = _canonical_actions(actions)
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
            }
            items.append(scene)
            self._write_atomic(items)
            self._invalidate()
        self._fire_changed()
        return _clone_scene(scene)

    def update(self, key: str, *, label: Optional[str] = None,
               actions: Optional[list] = None) -> Optional[dict]:
        if actions is not None:
            canonical_actions = _canonical_actions(actions)
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
        return self.create(label=label, actions=src["actions"])

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
                    "created": entry.get("created") or _now_iso(),
                    "updated": entry.get("updated") or _now_iso(),
                    "actions": _canonical_actions(entry.get("actions")),
                })
            self._write_atomic(canonical)
            self._invalidate()
        self._fire_changed()


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
        groups = action.get("groups")
        out["groups"] = list(groups) if isinstance(groups, list) else groups
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
    """Defensive copy of a top-level or child target. Top-level targets are
    flat ``{kind, value}`` dicts; child targets are ``{kind: "scope"}`` or
    similar with no nested mutable structures."""
    return dict(target)
