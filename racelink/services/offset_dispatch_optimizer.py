"""Wire-path optimizer for ``offset_group`` container actions.

The runner uses this module to plan the OPC_OFFSET sequence that sets up
a container's per-device state with the smallest possible packet count.
The same module is consumed by the cost estimator to predict bandwidth
without dispatching anything.

Three strategies are evaluated; the cheapest wins (tie-break: bytes):

* **A — Broadcast formula** (1 packet)
    Possible only when ``groups == "all"`` and the formula mode is one of
    ``linear`` / ``vshape`` / ``modulo`` / ``none``. Sends one OPC_OFFSET
    with groupId=255 carrying the formula; the firmware evaluates it
    per-device.

* **B — Per-group EXPLICIT** (N packets)
    Always available. Evaluates the formula host-side via the shared
    ``evaluate_offset_ms`` and emits one OPC_OFFSET (mode=EXPLICIT) per
    participating group.

* **C — Broadcast formula + NONE overrides** (1 + L packets, where
    L = |non-participants|)
    Possible only when ``groups`` is a concrete sparse list AND the mode
    is a formula (linear/vshape/modulo). Wins when more groups participate
    than don't. Emits one OPC_OFFSET (formula, broadcast) followed by an
    OPC_OFFSET (mode=NONE) for each non-participating group to reset them
    back to NORMAL acceptance — without these, the formula would also
    activate offset mode on the non-participants.

The unconditional "Broadcast NONE then set" strategy stays manual: the
operator inserts an explicit clear container action upstream when they
want a clean slate.

The optimizer is **pure** — no side effects, no I/O. The runner consumes
the returned ops list and calls the actual transport.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from ..domain.offset_formula import evaluate_offset_ms
from ..protocol.packets import build_offset_body

# Modes the firmware can evaluate by itself (broadcast-friendly).
FORMULA_MODES = ("linear", "vshape", "modulo", "none")


@dataclass(frozen=True)
class WireOp:
    """One planned wire send. Side-effect-free; the runner converts these
    to actual transport calls.

    Used by both the cost estimator and the runner. The estimator sums
    ``body_bytes`` across the plan; the runner looks up ``sender`` in
    its own service-method adapter, then dispatches via
    ``getattr(adapter, op.sender)(**op.payload)``. This shared shape is
    the structural sync point between the two — see
    ``racelink/services/dispatch_planner.py`` for the entry point that
    produces these ops for every action kind, and the parity tests in
    ``tests/test_dispatch_parity.py`` for the contract.
    """

    opcode: str                                   # "OPC_OFFSET"|"OPC_CONTROL"|"OPC_PRESET"|"OPC_SYNC"
    target_group: int                             # 0..254 unicast filter; 255 = broadcast all groups
    payload: Dict[str, Any]                       # kwargs ready to spread into the sender
    body_bytes: int                               # length of the body on the wire (post-build)
    sender: Optional[str] = None                  # symbolic dispatch key — runner maps to a service method
    detail: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OptimizerPlan:
    """Result of :func:`plan_offset_setup`: the chosen strategy plus the
    ordered list of wire ops to emit before the container's children fire."""

    strategy: str        # "A_broadcast_formula" | "B_per_group_explicit" | "C_formula_plus_overrides"
    ops: List[WireOp] = field(default_factory=list)

    @property
    def packet_count(self) -> int:
        return len(self.ops)

    @property
    def total_bytes(self) -> int:
        return sum(op.body_bytes for op in self.ops)


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

def plan_offset_setup(
    *,
    target: Mapping[str, Any],
    offset: Mapping[str, Any],
    known_group_ids,
) -> OptimizerPlan:
    """Plan the OPC_OFFSET phase for an offset_group container action.

    Parameters
    ----------
    target
        The unified container target dict (see
        ``scenes_service._canonical_offset_group_container_target``). Either
        ``{"kind": "broadcast"}`` or
        ``{"kind": "groups", "value": [<int>, ...]}``. Strategy A is
        eligible only for the broadcast shape; concrete-list shapes go
        through Strategy B / C.
    offset
        The persisted ``offset`` block. Mode + mode-specific params as
        validated by ``scenes_service``.
    known_group_ids
        The group ids the host currently knows about (from
        ``device_repository`` or similar). Used by Strategy C to count
        non-participants. May be empty if the host has no device list yet.

    Returns
    -------
    OptimizerPlan
        Ordered ops list. Empty when ``offset.mode == "none"`` and there
        is nothing meaningful to send (e.g. clearing all on an empty
        device universe).
    """
    target_kind = target.get("kind")
    is_all = (target_kind == "broadcast")
    if is_all:
        participant_groups: List[int] = []  # not used in the all-broadcast path
    elif target_kind == "groups":
        raw_value = target.get("value") or []
        participant_groups = list(raw_value)
    else:
        raise ValueError(
            f"plan_offset_setup: unsupported target kind {target_kind!r} "
            "(expected 'broadcast' or 'groups')"
        )
    mode = (offset.get("mode") or "none").lower()

    # Strategy A — broadcast formula. Only meaningful when broadcast + formula.
    a: Optional[OptimizerPlan] = None
    if is_all and mode in FORMULA_MODES:
        op = _make_offset_op(target_group=255, mode=mode, params=offset)
        a = OptimizerPlan(strategy="A_broadcast_formula", ops=[op])

    # Strategy B — per-group EXPLICIT. Always available when groups is a
    # concrete list (or, for the broadcast case, when the host knows the
    # device universe).
    b: Optional[OptimizerPlan] = None
    concrete_groups = (
        list(known_group_ids) if is_all else list(participant_groups)
    )
    if concrete_groups:
        if mode == "explicit":
            # Use the persisted per-group values directly; ignore groups
            # without a value (shouldn't happen for valid inputs but be safe).
            values = {int(v["id"]): int(v["offset_ms"])
                      for v in offset.get("values") or []}
            ops = [
                _make_offset_op(
                    target_group=gid, mode="explicit",
                    params={"offset_ms": values.get(gid, 0)},
                )
                for gid in concrete_groups
            ]
        else:
            # Evaluate the formula host-side per group.
            ops = [
                _make_offset_op(
                    target_group=gid, mode="explicit",
                    params={"offset_ms": evaluate_offset_ms(offset, gid)},
                )
                for gid in concrete_groups
            ]
        b = OptimizerPlan(strategy="B_per_group_explicit", ops=ops)

    # Strategy C — broadcast formula + NONE overrides for non-participants.
    # Only when groups is a concrete sparse list AND the mode is a formula
    # (so the broadcast OPC_OFFSET can compute per-device offsets, and we
    # only need to override the devices we don't want activated).
    c: Optional[OptimizerPlan] = None
    if (
        not is_all
        and mode in FORMULA_MODES
        and mode != "none"
        and known_group_ids
    ):
        non_participants = [
            gid for gid in known_group_ids
            if gid not in participant_groups
        ]
        # Worth considering only when this strategy can actually be cheaper
        # than B (1 + |non-participants| < |participants|).
        ops = [_make_offset_op(target_group=255, mode=mode, params=offset)]
        ops.extend(
            _make_offset_op(target_group=gid, mode="none", params={})
            for gid in non_participants
        )
        c = OptimizerPlan(strategy="C_formula_plus_overrides", ops=ops)

    # Special case: mode == "none" with concrete groups → Strategy B
    # collapses to "send NONE to each", which is correct: we deactivate
    # exactly the listed groups. Strategy C is meaningless (can't broadcast
    # NONE-formula against participants vs non-participants).
    if mode == "none" and not is_all:
        # Override B so the explicit per-group payload is mode=none, not
        # mode=explicit (the formula evaluator returns 0 for "none" but we
        # want to send a NONE clear, not an EXPLICIT 0).
        ops = [
            _make_offset_op(target_group=gid, mode="none", params={})
            for gid in concrete_groups
        ]
        b = OptimizerPlan(strategy="B_per_group_explicit", ops=ops)

    candidates = [p for p in (a, b, c) if p is not None]
    if not candidates:
        # Nothing to send (e.g. groups: "all" + mode: "explicit" is a
        # validation error upstream; or all=true + none + no known ids).
        return OptimizerPlan(strategy="A_broadcast_formula", ops=[])

    # Pick fewest packets; tie-break by bytes; stable order on further ties.
    candidates.sort(key=lambda p: (p.packet_count, p.total_bytes))
    return candidates[0]


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _make_offset_op(*, target_group: int, mode: str, params: Mapping[str, Any]) -> WireOp:
    """Build one WireOp + measure its on-wire body length via the canonical
    builder. Mode-specific kwargs are extracted so the WireOp.payload only
    carries what ``ControlService.send_offset`` needs."""
    kw = _offset_payload_kwargs(mode, params)
    body = build_offset_body(group_id=target_group, mode=mode, **kw)
    return WireOp(
        opcode="OPC_OFFSET",
        target_group=target_group,
        payload={"mode": mode, **kw},
        body_bytes=len(body),
        sender="send_offset",
    )


def _offset_payload_kwargs(mode: str, params: Mapping[str, Any]) -> Dict[str, Any]:
    """Extract the kwargs ``ControlService.send_offset`` expects for a given
    mode from a persisted ``target.offset`` block (or an explicit-built dict
    such as ``{"offset_ms": 100}``). Unknown sub-fields are dropped."""
    if mode == "none":
        return {}
    if mode == "explicit":
        return {"offset_ms": int(params.get("offset_ms", 0))}
    if mode == "linear":
        return {
            "base_ms": int(params.get("base_ms", 0)),
            "step_ms": int(params.get("step_ms", 0)),
        }
    if mode == "vshape":
        return {
            "base_ms": int(params.get("base_ms", 0)),
            "step_ms": int(params.get("step_ms", 0)),
            "center":  int(params.get("center", 0)),
        }
    if mode == "modulo":
        return {
            "base_ms": int(params.get("base_ms", 0)),
            "step_ms": int(params.get("step_ms", 0)),
            "cycle":   max(1, int(params.get("cycle", 1))),
        }
    raise ValueError(f"unknown offset mode: {mode!r}")
