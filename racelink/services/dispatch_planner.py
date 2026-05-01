"""Pure dispatch planner — single source of truth for "what packets
would the runner emit for this action" used by both the scene runner
(which dispatches each op) and the cost estimator (which sums byte
counts).

Background
----------

Pre-2026-05-02 the runner and estimator each had their own per-kind
logic for resolving targets, fanning out to multiple groups, and
sizing OPC_CONTROL / OPC_PRESET bodies. They shared
:func:`plan_offset_setup` for the offset_group container's Phase 1
(OPC_OFFSET sequence) but Phase 2 (children) plus all top-level
kinds were duplicated implementations — same intent, two
copies. A subtle drift surfaced as
[`api.py:_known_group_ids_from_ctx`](../web/api.py)
silently returning ``[]`` and closing the optimizer's Strategy-C
gate for the estimator only; the runner kept emitting Strategy C
on the wire while the cost badge reported Strategy B.

This module collapses both code paths onto a single
:func:`plan_action_dispatch` function:

* The runner consumes ``plan.ops`` and dispatches each via
  ``getattr(adapter, op.sender)(**op.payload)``.
* The estimator consumes the same ``plan.ops`` and sums
  ``op.body_bytes`` per packet via the LoRa-physics layer in
  :mod:`scene_cost_estimator`.

Any future divergence between predicted and emitted wire cost is
caught by the parity tests in
``tests/test_dispatch_parity.py``.

Pure / side-effect-free
-----------------------

The planner does not call the transport, the gateway, the SSE
bridge, or anything stateful. Its only inputs are:

* The canonical action dict (validated upstream in
  :mod:`scenes_service`).
* ``known_group_ids`` — host's view of the device fleet,
  injected by the caller.
* ``rl_preset_lookup(ref) -> dict | None`` — closure over the
  preset store; resolves the merged params + persisted flags.
* ``device_lookup(addr) -> device | None`` — closure over the
  device repository; resolves a MAC to a device object so the
  runner's send path can use it directly. ``None`` means
  "unresolvable → degrade".

Logging is ``debug``-level only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from ..domain.flags import USER_FLAG_KEYS
from ..protocol.packets import (
    SYNC_FLAG_TRIGGER_ARMED,
    build_control_body,
    build_preset_body,
    build_sync_body,
)
from .offset_dispatch_optimizer import WireOp, plan_offset_setup
from .scenes_service import (
    KIND_DELAY,
    KIND_OFFSET_GROUP,
    KIND_RL_PRESET,
    KIND_STARTBLOCK,
    KIND_SYNC,
    KIND_WLED_CONTROL,
    KIND_WLED_PRESET,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ActionDispatchPlan:
    """Side-effect-free description of every wire packet a single
    action would emit when run.

    The runner iterates ``ops`` in order, dispatching each via the
    sender named on the op (see ``WireOp.sender``). The estimator
    iterates the same list and sums ``body_bytes`` to compute the
    cost-badge prediction. ``detail`` is forwarded to
    ``ActionResult.detail`` (runner) and ``ActionCost.detail``
    (estimator) so the WebUI's per-action info row shows identical
    values regardless of which path produced it.
    """

    kind: str
    ops: List[WireOp] = field(default_factory=list)
    degraded: bool = False
    error: Optional[str] = None
    detail: Mapping[str, Any] = field(default_factory=dict)

    @property
    def packet_count(self) -> int:
        return len(self.ops)

    @property
    def total_bytes(self) -> int:
        return sum(op.body_bytes for op in self.ops)


# Type aliases for the lookup callables.
RlPresetLookup = Callable[[Any], Optional[Mapping[str, Any]]]
DeviceLookup = Callable[[str], Optional[Any]]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def plan_action_dispatch(
    action: Mapping[str, Any],
    *,
    known_group_ids: Sequence[int],
    rl_preset_lookup: Optional[RlPresetLookup] = None,
    device_lookup: Optional[DeviceLookup] = None,
    force_offset_flag: Optional[bool] = None,
) -> ActionDispatchPlan:
    """Plan every wire packet ``action`` would emit at runtime.

    Returns an empty plan for ``delay`` (host-side wait — no wire
    traffic). Returns ``degraded=True`` with an ``error`` when a
    target cannot be resolved (e.g. ``target.kind == "device"`` for
    a MAC the host has never seen).

    ``force_offset_flag`` is set by the offset_group container
    recursion: when ``parent.offset.mode != "none"`` the container
    forces ``offset_mode=True`` on every child packet so the
    firmware-side gate selects offset-configured devices. ``False``
    forces ``offset_mode=False`` so a parent ``mode="none"``
    container's children apply immediately. ``None`` means the
    child / top-level action carries its own flag override
    unmodified.
    """
    kind = action.get("kind")
    if kind == KIND_DELAY:
        # No wire packets; the runner sleeps and the estimator
        # records the duration as wall-clock airtime.
        ms = float(action.get("duration_ms") or 0)
        return ActionDispatchPlan(
            kind=KIND_DELAY,
            ops=[],
            detail={"duration_ms": ms},
        )
    if kind == KIND_SYNC:
        return _plan_sync()
    if kind == KIND_OFFSET_GROUP:
        return _plan_offset_group(
            action,
            known_group_ids=known_group_ids,
            rl_preset_lookup=rl_preset_lookup,
            device_lookup=device_lookup,
        )
    if kind in (KIND_RL_PRESET, KIND_WLED_PRESET, KIND_WLED_CONTROL,
                KIND_STARTBLOCK):
        return _plan_effect(
            action,
            kind=kind,
            rl_preset_lookup=rl_preset_lookup,
            device_lookup=device_lookup,
            force_offset_flag=force_offset_flag,
        )
    # Forward-compat: an unknown kind lands here. Validator should
    # already have rejected it, but the planner stays safe.
    return ActionDispatchPlan(
        kind=str(kind or ""),
        ops=[],
        degraded=True,
        error=f"unknown_kind: {kind!r}",
    )


# ---------------------------------------------------------------------------
# Per-kind planners
# ---------------------------------------------------------------------------

def _plan_sync() -> ActionDispatchPlan:
    """OPC_SYNC with the trigger-armed flag — matches what the
    runner emits via :meth:`SyncService.send_sync(trigger_armed=True)`.

    The body length must be sized with ``flags=SYNC_FLAG_TRIGGER_ARMED``
    so the cost badge reflects the 5-byte form the runner actually
    transmits (not the 4-byte legacy form used by autosync). The
    runner injects ``ts24`` at dispatch time; the planner leaves it
    out of the payload (the adapter fills it in).
    """
    body = build_sync_body(0, 0, flags=SYNC_FLAG_TRIGGER_ARMED)
    return ActionDispatchPlan(
        kind=KIND_SYNC,
        ops=[WireOp(
            opcode="OPC_SYNC",
            target_group=255,
            payload={"brightness": 0, "trigger_armed": True},
            body_bytes=len(body),
            sender="send_sync",
        )],
    )


def _plan_offset_group(
    action: Mapping[str, Any], *,
    known_group_ids: Sequence[int],
    rl_preset_lookup: Optional[RlPresetLookup],
    device_lookup: Optional[DeviceLookup],
) -> ActionDispatchPlan:
    """Phase 1 (OPC_OFFSET sequence via the optimizer) + Phase 2
    (each child via recursive :func:`plan_action_dispatch`)."""
    container_target = action.get("target") or {"kind": "broadcast"}
    offset_spec = action.get("offset") or {"mode": "none"}
    offset_mode = (offset_spec.get("mode") or "none").lower()
    children = action.get("actions") or []

    optimizer_plan = plan_offset_setup(
        target=container_target,
        offset=offset_spec,
        known_group_ids=known_group_ids,
    )

    ops: List[WireOp] = []
    # Tag each Phase 1 op with a ``phase`` detail so the runner can
    # rebuild ActionResult.detail.offset_packets vs detail.children.
    for op in optimizer_plan.ops:
        ops.append(WireOp(
            opcode=op.opcode,
            target_group=op.target_group,
            payload=op.payload,
            body_bytes=op.body_bytes,
            sender=op.sender or "send_offset",
            detail={"phase": "offset", "mode": op.payload.get("mode")},
        ))

    # Phase 2: children. The offset-mode flag is forced on each
    # child by the parent's mode (formula → True; "none" → False).
    force_offset_flag = (offset_mode != "none")
    child_plans: List[ActionDispatchPlan] = []
    any_degraded = False
    for child_idx, child in enumerate(children):
        child_plan = plan_action_dispatch(
            child,
            known_group_ids=known_group_ids,
            rl_preset_lookup=rl_preset_lookup,
            device_lookup=device_lookup,
            force_offset_flag=force_offset_flag,
        )
        child_plans.append(child_plan)
        if child_plan.degraded:
            any_degraded = True
        for op in child_plan.ops:
            # Tag with phase=child + child_idx so the runner can
            # group per-child results without re-iterating.
            tagged_detail = dict(op.detail or {})
            tagged_detail["phase"] = "child"
            tagged_detail["child_index"] = child_idx
            tagged_detail["child_kind"] = child_plan.kind
            ops.append(WireOp(
                opcode=op.opcode,
                target_group=op.target_group,
                payload=op.payload,
                body_bytes=op.body_bytes,
                sender=op.sender,
                detail=tagged_detail,
            ))

    detail: Dict[str, Any] = {
        "wire_path": optimizer_plan.strategy,
        "offset_mode": offset_mode,
        "offset_packets": optimizer_plan.packet_count,
        "offset_total_bytes": optimizer_plan.total_bytes,
        "child_count": len(children),
        # Child plans for the runner / detail roll-up. Estimator
        # ignores it.
        "child_plans": child_plans,
    }
    return ActionDispatchPlan(
        kind=KIND_OFFSET_GROUP,
        ops=ops,
        degraded=any_degraded,
        detail=detail,
    )


def _plan_effect(
    action: Mapping[str, Any], *,
    kind: str,
    rl_preset_lookup: Optional[RlPresetLookup],
    device_lookup: Optional[DeviceLookup],
    force_offset_flag: Optional[bool],
) -> ActionDispatchPlan:
    """Top-level / offset_group-child plan for the four per-group
    effect kinds (rl_preset / wled_preset / wled_control /
    startblock). Each emits one WireOp per resolved target."""
    target = action.get("target") or {}
    target_kwargs_list = _resolve_target(target, device_lookup=device_lookup)
    if target_kwargs_list is None:
        return ActionDispatchPlan(
            kind=kind, ops=[], degraded=True,
            error="target_not_found",
            detail={"target": dict(target)},
        )

    # Materialise params + flags once per action.
    if kind == KIND_RL_PRESET:
        materialised = _materialize_rl_preset(action, rl_preset_lookup)
        if materialised is None:
            preset_ref = (action.get("params") or {}).get("presetId")
            if preset_ref is None:
                return ActionDispatchPlan(
                    kind=kind, ops=[], degraded=True,
                    error="missing_preset_id",
                )
            return ActionDispatchPlan(
                kind=kind, ops=[], degraded=True,
                error=f"preset_not_found: {preset_ref!r}",
            )
        base_params, persisted_flags, preset_detail = materialised
    elif kind in (KIND_WLED_PRESET, KIND_WLED_CONTROL):
        base_params = dict(action.get("params") or {})
        persisted_flags = {}
        preset_detail = {}
    else:  # KIND_STARTBLOCK
        base_params = dict(action.get("params") or {})
        persisted_flags = {}
        preset_detail = {}

    override = dict(action.get("flags_override") or {})
    if force_offset_flag is not None:
        override["offset_mode"] = bool(force_offset_flag)

    merged_params = _merge_flags(base_params, persisted_flags, override)

    # Build one op per target. Body sizing uses ``group_id=0`` as a
    # neutral placeholder — the byte count is independent of the
    # actual gid (a single byte either way), and the runner sets
    # the real gid via the transport when it spreads the kwargs.
    ops: List[WireOp] = []
    for target_kwargs in target_kwargs_list:
        op = _build_effect_op(
            kind=kind,
            target_kwargs=target_kwargs,
            merged_params=merged_params,
            preset_detail=preset_detail,
        )
        ops.append(op)

    return ActionDispatchPlan(
        kind=kind,
        ops=ops,
        detail=dict(preset_detail),
    )


def _build_effect_op(
    *,
    kind: str,
    target_kwargs: Mapping[str, Any],
    merged_params: Mapping[str, Any],
    preset_detail: Mapping[str, Any],
) -> WireOp:
    """Construct a single WireOp for a per-group effect kind.

    ``target_kwargs`` is one of ``{"targetGroup": <int>}`` or
    ``{"targetDevice": <device_obj>}``. The returned op's
    ``payload`` is ready-to-spread into the named sender —
    ``params=`` plus exactly one of ``targetGroup`` / ``targetDevice``.
    """
    payload: Dict[str, Any] = {
        "params": dict(merged_params),
        **target_kwargs,
    }
    if kind == KIND_WLED_PRESET:
        body = build_preset_body(0, 0, 0, 0)
        return WireOp(
            opcode="OPC_PRESET",
            target_group=_target_group_for_op(target_kwargs),
            payload=payload,
            body_bytes=len(body),
            sender="send_wled_preset",
            detail=dict(preset_detail),
        )
    # rl_preset / wled_control / startblock → OPC_CONTROL-shaped
    body_len = _control_body_len(merged_params)
    sender = "send_startblock" if kind == KIND_STARTBLOCK else "send_wled_control"
    op_detail = dict(preset_detail)
    if kind == KIND_STARTBLOCK:
        # Body sizing for startblock is approximate — we treat it
        # as OPC_CONTROL-shaped, but the controller's
        # ``sendStartblockControl`` emits a custom body. Flag the
        # approximation so a future tightening is greppable.
        op_detail["approximate"] = True
    return WireOp(
        opcode="OPC_CONTROL",
        target_group=_target_group_for_op(target_kwargs),
        payload=payload,
        body_bytes=body_len,
        sender=sender,
        detail=op_detail,
    )


def _target_group_for_op(target_kwargs: Mapping[str, Any]) -> int:
    """``WireOp.target_group`` is the numeric gid the wire would
    carry. For device targets that's the device's stored gid (the
    "single-device pinned rule" — see the broadcast ruleset). For
    group targets it's the gid in the kwargs. Defaults to 255 when
    unknowable (broadcast)."""
    if "targetGroup" in target_kwargs:
        return int(target_kwargs["targetGroup"])
    dev = target_kwargs.get("targetDevice")
    if dev is not None:
        gid = getattr(dev, "groupId", None)
        if isinstance(gid, int) and 0 <= gid <= 254:
            return gid
    return 255


# ---------------------------------------------------------------------------
# Helpers: target resolution, flag merge, preset materialise, body sizing
# ---------------------------------------------------------------------------

def _resolve_target(
    target: Mapping[str, Any], *,
    device_lookup: Optional[DeviceLookup],
) -> Optional[List[Dict[str, Any]]]:
    """Translate the unified ``target`` shape into a list of partial
    sender kwargs. Returns ``None`` when the target cannot be
    resolved (degraded action). Identical logic for top-level and
    offset_group-child targets — the broadcast ruleset's "Stage 2"
    group filtering is enforced device-side, not here.
    """
    tk = target.get("kind")
    if tk == "broadcast":
        return [{"targetGroup": 255}]
    if tk == "groups":
        value = target.get("value") or []
        if not value:
            return None
        return [{"targetGroup": int(g)} for g in value]
    if tk == "device":
        if device_lookup is None:
            return None
        addr = str(target.get("value") or "")
        device = device_lookup(addr)
        if device is None:
            return None
        return [{"targetDevice": device}]
    return None


def _merge_flags(
    base_params: Mapping[str, Any],
    persisted_flags: Mapping[str, Any],
    override: Mapping[str, Any],
) -> Dict[str, Any]:
    """Apply flag override on top of persisted preset flags; write
    every True flag into ``params`` so :class:`ControlService` picks
    them up. False flags are stripped (an explicit False override
    wins over a True persisted value). Mirrors what
    ``SceneRunnerService._merge_flags_into_params`` does today.
    """
    merged: Dict[str, Any] = dict(base_params)
    for key in USER_FLAG_KEYS:
        if key in override:
            value = bool(override[key])
        else:
            value = bool(persisted_flags.get(key, False))
        if value:
            merged[key] = True
        else:
            merged.pop(key, None)
    return merged


def _materialize_rl_preset(
    action: Mapping[str, Any],
    lookup: Optional[RlPresetLookup],
) -> Optional[tuple]:
    """Resolve the action's ``presetId`` reference into the merged
    params + persisted flags + preset_detail tuple. Returns ``None``
    when the lookup cannot be performed (no service / preset not
    found / missing presetId) — the caller turns this into a
    degraded plan with the appropriate error tag.

    Mirrors the runner's ``_lookup_rl_preset`` flow + the
    estimator's ``_materialize_rl_preset_params`` so both produce
    identical merged params for the same input.
    """
    base_params = dict(action.get("params") or {})
    preset_ref = base_params.pop("presetId", None)
    if preset_ref is None:
        return None
    if lookup is None:
        # Estimator path with no rl-preset service wired — fall
        # back to whatever params the action carries (typically
        # just a brightness override). Under-reports OPC_CONTROL
        # body size for action defaults, but never crashes. This
        # matches today's estimator behaviour.
        return base_params, {}, {"preset_ref": preset_ref}
    try:
        preset = lookup(preset_ref)
    except Exception:  # pragma: no cover - swallow-ok
        logger.debug("rl_preset lookup failed for ref=%r", preset_ref, exc_info=True)
        preset = None
    if preset is None:
        return None
    merged_params: Dict[str, Any] = dict(preset.get("params") or {})
    if "brightness" in base_params and base_params["brightness"] is not None:
        merged_params["brightness"] = int(base_params["brightness"])
    persisted_flags = dict(preset.get("flags") or {})
    detail = {
        "preset_key": preset.get("key"),
        "preset_id": preset.get("id"),
    }
    return merged_params, persisted_flags, detail


def _control_body_len(params: Mapping[str, Any]) -> int:
    """Approximate body length of an OPC_CONTROL with the given
    params dict — uses the canonical builder so the prediction
    matches what ``ControlService.send_wled_control`` would emit.
    Single source of truth (today this lives in both the estimator
    and is mirrored by the runner via the actual builder); now
    here only.
    """
    kwargs: Dict[str, Any] = {}
    if "brightness" in params and params["brightness"] is not None:
        kwargs["brightness"] = int(params["brightness"]) & 0xFF
    for key in ("mode", "speed", "intensity", "custom1", "custom2", "custom3", "palette"):
        if key in params and params[key] is not None:
            kwargs[key] = int(params[key]) & 0xFF
    for key in ("check1", "check2", "check3"):
        if key in params and params[key] is not None:
            kwargs[key] = bool(params[key])
    for key in ("color1", "color2", "color3"):
        if key in params and params[key] is not None:
            kwargs[key] = tuple(int(c) & 0xFF for c in params[key])
    body = build_control_body(group_id=0, flags=0, **kwargs)
    return len(body)
