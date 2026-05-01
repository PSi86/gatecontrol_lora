"""Scene runner — sequential dispatcher for scenes persisted via SceneService.

The runner walks the scene's ``actions`` list in order. Each action is
dispatched by ``kind`` to either an existing :class:`ControlService` method,
to :class:`SyncService.send_sync` (for ``sync`` actions), or to
``time.sleep`` (for ``delay`` actions). One action runs at a time; choreographed
simultaneity is achieved by setting ``arm_on_sync=True`` on multiple
dispatchable actions and inserting a ``sync`` action after them.

Per-action results are collected into a :class:`SceneRunResult`. v1 policy:
**continue on error** — a failed action records its error and the next action
runs. This matches the way real-world scene playback degrades gracefully when
a single device drops off (we still want the rest of the show to happen).

The runner is sequential and synchronous on the calling thread. The REST
endpoint that exposes ``run`` will spawn a background thread and stream
per-action progress over SSE; that wiring lives in the API layer (Phase B).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..domain.flags import USER_FLAG_KEYS
from .offset_dispatch_optimizer import plan_offset_setup
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


# ---- result types --------------------------------------------------------


@dataclass
class ActionResult:
    index: int
    kind: str
    ok: bool
    error: Optional[str] = None
    degraded: bool = False
    duration_ms: int = 0
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        out = {
            "index": self.index,
            "kind": self.kind,
            "ok": self.ok,
            "duration_ms": self.duration_ms,
        }
        if self.error is not None:
            out["error"] = self.error
        if self.degraded:
            out["degraded"] = True
        if self.detail:
            out["detail"] = dict(self.detail)
        return out


@dataclass
class SceneRunResult:
    scene_key: str
    ok: bool
    error: Optional[str] = None
    actions: List[ActionResult] = field(default_factory=list)
    # Batch A (2026-04-28): when ``stop_on_error`` aborts a run mid-
    # sequence, ``aborted_at_index`` carries the 0-based index of the
    # action that triggered the abort. Subsequent action indices are
    # represented by ``ActionResult`` entries with ``error="skipped:
    # aborted"`` so the UI can show why they didn't run. ``None`` when
    # the run completed every action (whether ok or not).
    aborted_at_index: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        out = {
            "scene_key": self.scene_key,
            "ok": self.ok,
            "actions": [a.to_dict() for a in self.actions],
        }
        if self.error is not None:
            out["error"] = self.error
        if self.aborted_at_index is not None:
            out["aborted_at_index"] = int(self.aborted_at_index)
        return out


# ---- runner --------------------------------------------------------------


class SceneRunnerService:
    """Sequential executor for scenes."""

    def __init__(
        self,
        *,
        controller,
        scenes_service,
        control_service,
        sync_service,
        rl_presets_service=None,
        sleep: Callable[[float], None] = time.sleep,
        clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
    ):
        self.controller = controller
        self.scenes_service = scenes_service
        self.control_service = control_service
        self.sync_service = sync_service
        # rl_presets_service is optional at construction; we resolve it
        # lazily so test harnesses without it can still drive the runner
        # for non-rl_preset action kinds.
        self._rl_presets_service = rl_presets_service
        self._sleep = sleep
        self._clock_ms = clock_ms

    @property
    def rl_presets_service(self):
        if self._rl_presets_service is not None:
            return self._rl_presets_service
        return getattr(self.controller, "rl_presets_service", None)

    # ---- public API ------------------------------------------------------

    def run(
        self,
        scene_key: str,
        *,
        progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
        scene: Optional[Dict[str, Any]] = None,
    ) -> SceneRunResult:
        """Run a scene and optionally emit per-action progress events.

        ``progress_cb`` (when supplied) is invoked twice per action:
        - once with ``status="running"`` before the action dispatches, and
        - once with ``status="ok" | "error" | "degraded"`` after it returns.

        Callback exceptions are swallowed so an SSE outage on the consumer
        side cannot abort the run. The synchronous ``SceneRunResult`` is
        unchanged — the callback is purely additive observability.

        ``scene`` (optional): when supplied, the runner uses this dict as
        the source-of-truth instead of looking ``scene_key`` up in storage.
        Used by the editor's "Run executes the displayed draft without
        overwriting the saved scene" path. ``scene_key`` is still required
        — it identifies the broadcast key on ``scene_progress`` SSE events
        so listeners (the editor's ``activeRunKey`` filter) match the run
        the operator just kicked off. The supplied dict must already be in
        canonical form (validated via ``_canonical_actions``); the runner
        does not re-validate.
        """
        if scene is None:
            scene = self.scenes_service.get(scene_key)
            if scene is None:
                return SceneRunResult(scene_key=scene_key, ok=False, error="scene_not_found")

        # Batch A (2026-04-28): per-scene stop-on-error gate. Default
        # True for both legacy scenes (loaded without the field) and
        # newly-created ones — matches the editor checkbox default.
        # Operators who want the legacy "play through every action"
        # semantic explicitly opt out per scene.
        stop_on_error = bool(scene.get("stop_on_error", True))

        scene_actions = list(scene["actions"])
        results: List[ActionResult] = []
        aborted_at_index: Optional[int] = None
        for index, action in enumerate(scene_actions):
            self._emit_progress(progress_cb, {
                "scene_key": scene_key,
                "index": index,
                "kind": action.get("kind"),
                "status": "running",
            })
            result = self._dispatch(index, action)
            results.append(result)
            if result.degraded:
                terminal = "degraded"
            elif result.ok:
                terminal = "ok"
            else:
                terminal = "error"
            self._emit_progress(progress_cb, {
                "scene_key": scene_key,
                "index": index,
                "kind": result.kind,
                "status": terminal,
                "error": result.error,
                "duration_ms": result.duration_ms,
            })

            # Abort the rest of the scene on first failure when
            # stop_on_error is on. ``degraded`` does NOT trigger abort
            # — degraded means "ran with caveats" (e.g. unknown device
            # target collapsed to a no-op); the runner saw a sensible
            # outcome. Only outright ``ok=False`` terminates.
            if stop_on_error and not result.ok and not result.degraded:
                aborted_at_index = index
                # Append "skipped" placeholders for every remaining
                # action so the UI / SSE consumer can render the abort
                # cleanly without needing to reason about the absence.
                for skipped_idx in range(index + 1, len(scene_actions)):
                    skipped_action = scene_actions[skipped_idx]
                    skipped_result = ActionResult(
                        index=skipped_idx,
                        kind=skipped_action.get("kind") or "unknown",
                        ok=False,
                        error="skipped: aborted",
                        duration_ms=0,
                    )
                    results.append(skipped_result)
                    self._emit_progress(progress_cb, {
                        "scene_key": scene_key,
                        "index": skipped_idx,
                        "kind": skipped_result.kind,
                        "status": "skipped",
                        "error": skipped_result.error,
                        "duration_ms": 0,
                    })
                break

        # ``ok`` reflects whether *every* action succeeded — an aborted
        # run with skipped placeholders is not ``ok``.
        ok = all(r.ok for r in results)
        return SceneRunResult(
            scene_key=scene_key,
            ok=ok,
            actions=results,
            aborted_at_index=aborted_at_index,
        )

    @staticmethod
    def _emit_progress(progress_cb, payload):
        if progress_cb is None:
            return
        try:
            progress_cb(payload)
        except Exception:
            # swallow-ok: SSE listener crash must not undo a scene run
            logger.exception("scene runner: progress_cb raised")

    # ---- dispatch --------------------------------------------------------

    def _dispatch(self, index: int, action: dict) -> ActionResult:
        kind = action.get("kind")
        started = self._clock_ms()
        try:
            if kind == KIND_RL_PRESET:
                return self._run_rl_preset(index, action, started)
            if kind == KIND_WLED_PRESET:
                return self._run_wled_preset(index, action, started)
            if kind == KIND_WLED_CONTROL:
                return self._run_wled_control(index, action, started)
            if kind == KIND_STARTBLOCK:
                return self._run_startblock(index, action, started)
            if kind == KIND_SYNC:
                return self._run_sync(index, started)
            if kind == KIND_DELAY:
                return self._run_delay(index, action, started)
            if kind == KIND_OFFSET_GROUP:
                return self._run_offset_group(index, action, started)
            return ActionResult(
                index=index, kind=str(kind), ok=False,
                error="unknown_kind",
                duration_ms=self._clock_ms() - started,
            )
        except Exception as exc:
            logger.exception("scene runner: action %d (%s) raised", index, kind)
            return ActionResult(
                index=index, kind=str(kind), ok=False,
                error=f"exception: {exc}",
                duration_ms=self._clock_ms() - started,
            )

    # ---- per-kind handlers ----------------------------------------------

    def _resolve_target(self, target: dict, kind: str, index: int) -> Optional[dict]:
        """Return ``{targetDevice: …}`` or ``{targetGroup: …}`` kwargs, or
        ``None`` if the target no longer exists (degraded action)."""
        tk = target.get("kind")
        tv = target.get("value")
        if tk == "group":
            return {"targetGroup": int(tv)}
        if tk == "device":
            getter = getattr(self.controller, "getDeviceFromAddress", None)
            if getter is None:
                return None
            device = getter(str(tv))
            if device is None:
                return None
            return {"targetDevice": device}
        return None

    def _merge_flags_into_params(self, params: dict, persisted_flags: dict, override: dict) -> dict:
        """Apply flag override on top of persisted preset flags, write the
        boolean flags into ``params`` so the underlying ControlService can pick
        them up. Override key wins; absent override key keeps persisted value.
        """
        merged = dict(params)
        for key in USER_FLAG_KEYS:
            if key in override:
                value = bool(override[key])
            else:
                value = bool(persisted_flags.get(key, False))
            if value:
                merged[key] = True
            else:
                # Strip a possibly-set flag so the False override actually wins.
                merged.pop(key, None)
        return merged

    def _run_rl_preset(self, index: int, action: dict, started: int) -> ActionResult:
        params = dict(action.get("params") or {})
        preset_ref = params.pop("presetId", None)
        if preset_ref is None:
            return ActionResult(
                index=index, kind=KIND_RL_PRESET, ok=False,
                error="missing_preset_id",
                duration_ms=self._clock_ms() - started,
            )

        rl_service = self.rl_presets_service
        if rl_service is None:
            return ActionResult(
                index=index, kind=KIND_RL_PRESET, ok=False,
                error="rl_presets_service_unavailable",
                duration_ms=self._clock_ms() - started,
            )

        preset = self._lookup_rl_preset(rl_service, preset_ref)
        if preset is None:
            return ActionResult(
                index=index, kind=KIND_RL_PRESET, ok=False,
                error=f"preset_not_found: {preset_ref!r}",
                duration_ms=self._clock_ms() - started,
            )

        base_params = dict(preset.get("params") or {})
        if "brightness" in params and params["brightness"] is not None:
            base_params["brightness"] = int(params["brightness"])
        persisted_flags = preset.get("flags") or {}
        preset_detail = {"preset_key": preset.get("key"), "preset_id": preset.get("id")}

        target = action["target"]
        target_kwargs = self._resolve_target(target, KIND_RL_PRESET, index)
        if target_kwargs is None:
            return ActionResult(
                index=index, kind=KIND_RL_PRESET, ok=False,
                error="target_not_found", degraded=True,
                duration_ms=self._clock_ms() - started,
                detail={"target": dict(target)},
            )

        merged_params = self._merge_flags_into_params(
            base_params,
            persisted_flags,
            action.get("flags_override") or {},
        )

        ok = bool(self.control_service.send_wled_control(params=merged_params, **target_kwargs))
        return ActionResult(
            index=index, kind=KIND_RL_PRESET, ok=ok,
            error=None if ok else "send_failed",
            duration_ms=self._clock_ms() - started,
            detail=preset_detail,
        )

    def _lookup_rl_preset(self, rl_service, preset_ref) -> Optional[dict]:
        """Resolve an RL preset reference. Accepts:
        - bare slug ``"start_red"``
        - stable cross-system key ``"RL:start_red"``
        - integer id (or its stringified form ``"42"``)
        """
        # Stable key form
        if isinstance(preset_ref, str) and preset_ref.startswith("RL:"):
            return rl_service.get(preset_ref[3:])
        # Integer id
        if isinstance(preset_ref, int):
            return rl_service.get_by_id(preset_ref)
        if isinstance(preset_ref, str):
            stripped = preset_ref.strip()
            if stripped.isdigit():
                return rl_service.get_by_id(int(stripped))
            return rl_service.get(stripped)
        return None

    def _run_wled_preset(self, index: int, action: dict, started: int) -> ActionResult:
        base_params = dict(action.get("params") or {})
        target = action["target"]
        target_kwargs = self._resolve_target(target, KIND_WLED_PRESET, index)
        if target_kwargs is None:
            return ActionResult(
                index=index, kind=KIND_WLED_PRESET, ok=False,
                error="target_not_found", degraded=True,
                duration_ms=self._clock_ms() - started,
                detail={"target": dict(target)},
            )

        merged_params = self._merge_flags_into_params(
            base_params,
            persisted_flags={},  # WLED-preset has no persisted flags
            override=action.get("flags_override") or {},
        )
        ok = bool(self.control_service.send_wled_preset(params=merged_params, **target_kwargs))
        return ActionResult(
            index=index, kind=KIND_WLED_PRESET, ok=ok,
            error=None if ok else "send_failed",
            duration_ms=self._clock_ms() - started,
        )

    def _run_wled_control(self, index: int, action: dict, started: int) -> ActionResult:
        base_params = dict(action.get("params") or {})
        target = action["target"]
        target_kwargs = self._resolve_target(target, KIND_WLED_CONTROL, index)
        if target_kwargs is None:
            return ActionResult(
                index=index, kind=KIND_WLED_CONTROL, ok=False,
                error="target_not_found", degraded=True,
                duration_ms=self._clock_ms() - started,
                detail={"target": dict(target)},
            )

        merged_params = self._merge_flags_into_params(
            base_params,
            persisted_flags={},
            override=action.get("flags_override") or {},
        )
        ok = bool(self.control_service.send_wled_control(params=merged_params, **target_kwargs))
        return ActionResult(
            index=index, kind=KIND_WLED_CONTROL, ok=ok,
            error=None if ok else "send_failed",
            duration_ms=self._clock_ms() - started,
        )

    # ---- offset_group container dispatch --------------------------------

    def _run_offset_group(self, index: int, action: dict, started: int) -> ActionResult:
        """Dispatch an ``offset_group`` container action.

        Phase 1 — the wire-path optimizer (see ``offset_dispatch_optimizer``)
        plans the cheapest OPC_OFFSET sequence: one broadcast formula packet
        when participation covers all groups, or per-group EXPLICIT
        otherwise. The runner emits each WireOp via ``ControlService``.

        Phase 2 — for each child action, dispatch the effect via the matching
        ControlService method (``send_wled_control`` /
        ``send_wled_preset`` / ``send_rl_preset_by_id``). The OFFSET_MODE
        flag is *forced* on regardless of the child's flags_override, so
        the wire-level acceptance gate selects exactly the offset-configured
        devices. The runner never auto-emits OPC_SYNC — scenes opt into sync
        via an explicit ``sync`` top-level action after the container.

        ActionResult.detail carries:
            * ``wire_path``: the optimizer strategy chosen (e.g. ``A_broadcast_formula``).
            * ``offset_packets``: per-OPC_OFFSET ok/fail map keyed by target group.
            * ``children``: list of {kind, ok, [error], [stage]} per child.
        """
        target_groups = action.get("groups")
        offset_spec = action.get("offset") or {"mode": "none"}
        offset_mode = (offset_spec.get("mode") or "none").lower()
        children = action.get("actions") or []

        known_group_ids = self._known_group_ids()

        # Phase 1: optimizer-planned OPC_OFFSET sequence.
        plan = plan_offset_setup(
            participant_groups=target_groups,
            offset=offset_spec,
            known_group_ids=known_group_ids,
        )

        logger.info(
            "offset_group dispatch: groups=%s offset.mode=%s strategy=%s "
            "offset_packets=%d children=%d",
            target_groups, offset_mode, plan.strategy,
            plan.packet_count, len(children),
        )

        offset_results: Dict[str, Dict[str, Any]] = {}
        any_offset_failed = False
        for op in plan.ops:
            payload = dict(op.payload)
            mode = payload.pop("mode")
            ok = bool(
                self.control_service.send_offset(
                    targetGroup=op.target_group, mode=mode, **payload,
                )
            )
            row: Dict[str, Any] = {"ok": ok, "mode": mode}
            if op.target_group != 255:
                row["target_group"] = op.target_group
            if not ok:
                row["stage"] = "offset"
                row["error"] = "send_offset_failed"
                any_offset_failed = True
            offset_results[str(op.target_group)] = row

        # Phase 2: dispatch each child action.
        #
        # The OFFSET_MODE flag forced on each child depends on the parent's
        # offset.mode. For real formula modes (linear/explicit/vshape/modulo)
        # we force F=1 so the firmware-side gate (asymmetric, Option C)
        # accepts on offset-configured devices and drops on the rest. For
        # mode="none" we force F=0 — the OPC_OFFSET(NONE) Phase-1 packet
        # has cleared pending; sending children with F=1 would still be
        # gate-dropped (F=1 + eff.mode=NONE drops). With F=0 the children
        # are always accepted, and the operator gets "clear AND play"
        # behaviour from a single offset_group(mode=none) scene.
        child_results: List[Dict[str, Any]] = []
        any_child_failed = False
        force_offset_flag = (offset_mode != "none")
        for child_idx, child in enumerate(children):
            child_outcome = self._dispatch_offset_group_child(
                child, force_offset_flag=force_offset_flag,
            )
            child_results.append({"index": child_idx, **child_outcome})
            if not child_outcome["ok"]:
                any_child_failed = True

        all_ok = (
            (not any_offset_failed)
            and (not any_child_failed)
            and (bool(plan.ops) or bool(children))
        )
        detail: Dict[str, Any] = {
            "wire_path": plan.strategy,
            "offset_mode": offset_mode,
            "offset_packets": offset_results,
            "offset_packet_count": plan.packet_count,
            "offset_total_bytes": plan.total_bytes,
            "children": child_results,
        }
        return ActionResult(
            index=index, kind=KIND_OFFSET_GROUP, ok=all_ok,
            error=None if all_ok else "offset_group_partial_failure",
            duration_ms=self._clock_ms() - started,
            detail=detail,
        )

    def _dispatch_offset_group_child(
        self, child: dict, *, force_offset_flag: bool = True,
    ) -> Dict[str, Any]:
        """Dispatch a single child action inside an offset_group container.

        ``force_offset_flag`` controls the OFFSET_MODE wire flag the child's
        OPC_CONTROL/OPC_PRESET packet carries. The parent
        ``_run_offset_group`` derives this from ``offset.mode``:

        * ``mode != "none"`` → ``True`` (the gate's F=1 + E=1 path).
        * ``mode == "none"`` → ``False`` (children apply immediately,
          gate's F=0 always-accept path; pending=NONE state on devices
          would otherwise drop F=1 children).

        ARM_ON_SYNC stays driven by the child's flags_override / persisted
        preset flags. Target translation per kind:

            * ``target.kind == "scope"``  → broadcast (groupId=255)
            * ``target.kind == "group"``  → unicast to group
            * ``target.kind == "device"`` → unicast to device's last3-MAC
              (degraded if device unknown)
        """
        kind = child.get("kind")
        child_target = child.get("target") or {"kind": "scope"}
        target_kwargs = self._resolve_offset_group_child_target(child_target)
        if target_kwargs is None:
            return {
                "kind": kind, "ok": False, "degraded": True,
                "error": "target_not_found",
                "target": dict(child_target),
            }

        forced = dict(child.get("flags_override") or {})
        forced["offset_mode"] = bool(force_offset_flag)

        if kind == KIND_RL_PRESET:
            params = dict(child.get("params") or {})
            preset_ref = params.pop("presetId", None)
            if preset_ref is None:
                return {"kind": kind, "ok": False, "error": "missing_preset_id"}
            rl_service = self.rl_presets_service
            if rl_service is None:
                return {"kind": kind, "ok": False, "error": "rl_presets_service_unavailable"}
            preset = self._lookup_rl_preset(rl_service, preset_ref)
            if preset is None:
                return {"kind": kind, "ok": False,
                        "error": f"preset_not_found: {preset_ref!r}"}
            base_params = dict(preset.get("params") or {})
            if "brightness" in params and params["brightness"] is not None:
                base_params["brightness"] = int(params["brightness"])
            merged = self._merge_flags_into_params(
                base_params, preset.get("flags") or {}, forced,
            )
            ok = bool(self.control_service.send_wled_control(params=merged, **target_kwargs))
            return {"kind": kind, "ok": ok,
                    **({} if ok else {"error": "send_failed"})}

        if kind == KIND_WLED_CONTROL:
            merged = self._merge_flags_into_params(
                dict(child.get("params") or {}), {}, forced,
            )
            ok = bool(self.control_service.send_wled_control(params=merged, **target_kwargs))
            return {"kind": kind, "ok": ok,
                    **({} if ok else {"error": "send_failed"})}

        if kind == KIND_WLED_PRESET:
            merged = self._merge_flags_into_params(
                dict(child.get("params") or {}), {}, forced,
            )
            ok = bool(self.control_service.send_wled_preset(params=merged, **target_kwargs))
            return {"kind": kind, "ok": ok,
                    **({} if ok else {"error": "send_failed"})}

        return {"kind": kind, "ok": False, "error": "unknown_child_kind"}

    def _resolve_offset_group_child_target(self, target: dict) -> Optional[dict]:
        """Translate an offset_group child target into ControlService kwargs.

        Returns ``None`` only when a device target cannot be resolved
        (degraded path). For ``scope`` and ``group`` we always return a
        valid kwargs dict — the wire-level acceptance gate handles
        per-device filtering on the firmware side.
        """
        tk = target.get("kind")
        if tk == "scope":
            return {"targetGroup": 255}
        if tk == "group":
            return {"targetGroup": int(target.get("value"))}
        if tk == "device":
            getter = getattr(self.controller, "getDeviceFromAddress", None)
            if getter is None:
                return None
            device = getter(str(target.get("value")))
            if device is None:
                return None
            return {"targetDevice": device}
        return None

    def _known_group_ids(self) -> List[int]:
        """Best-effort list of currently-configured group ids.

        Used by the optimizer to decide between broadcast-formula and
        per-group strategies. Falls back to an empty list when no device
        repository is wired (test environments) — Strategy A still works
        because the broadcast doesn't need a device list.
        """
        repo = getattr(self.controller, "device_repository", None)
        if repo is None:
            return []
        try:
            devices = list(repo.list())
        except Exception:
            # swallow-ok: optimizer falls back to no-known-devices (Strategy A
            # broadcast still works without a device list); a flaky repository
            # call must not break scene playback. Debug-log so a recurring
            # repository regression is diagnosable.
            logger.debug(
                "scene runner: device_repository.list() failed; "
                "optimizer falls back to no-known-devices",
                exc_info=True,
            )
            return []
        ids: set[int] = set()
        for d in devices:
            gid = getattr(d, "groupId", None)
            if isinstance(gid, int) and 0 <= gid <= 254:
                ids.add(gid)
        return sorted(ids)

    def _run_startblock(self, index: int, action: dict, started: int) -> ActionResult:
        target_kwargs = self._resolve_target(action["target"], KIND_STARTBLOCK, index)
        if target_kwargs is None:
            return ActionResult(
                index=index, kind=KIND_STARTBLOCK, ok=False,
                error="target_not_found", degraded=True,
                duration_ms=self._clock_ms() - started,
                detail={"target": dict(action["target"])},
            )
        sender = getattr(self.controller, "sendStartblockControl", None)
        if sender is None:
            return ActionResult(
                index=index, kind=KIND_STARTBLOCK, ok=False,
                error="sendStartblockControl_unavailable",
                duration_ms=self._clock_ms() - started,
            )
        params = dict(action.get("params") or {})
        ok = bool(sender(params=params, **target_kwargs))
        return ActionResult(
            index=index, kind=KIND_STARTBLOCK, ok=ok,
            error=None if ok else "send_failed",
            duration_ms=self._clock_ms() - started,
        )

    def _run_sync(self, index: int, started: int) -> ActionResult:
        # ts24 is the lower 24 bits of millis-since-epoch; the WLED node
        # unwraps it to a monotonic 32-bit timebase. brightness=0 is ignored
        # by nodes whose flags carry HAS_BRI; it's only consumed as live
        # brightness when HAS_BRI=0, which is fine here.
        # ``trigger_armed=True`` writes SYNC_FLAG_TRIGGER_ARMED on the wire so
        # the device materialises any pending arm-on-sync state. Autosync
        # (gateway- or future host-driven) leaves the flag unset so the
        # interval pulse cannot fire armed effects ahead of this packet.
        ts24 = int(self._clock_ms()) & 0xFFFFFF
        self.sync_service.send_sync(ts24, 0, trigger_armed=True)
        return ActionResult(
            index=index, kind=KIND_SYNC, ok=True,
            duration_ms=self._clock_ms() - started,
            detail={"ts24": ts24},
        )

    def _run_delay(self, index: int, action: dict, started: int) -> ActionResult:
        duration_ms = int(action.get("duration_ms", 0))
        self._sleep(duration_ms / 1000.0)
        return ActionResult(
            index=index, kind=KIND_DELAY, ok=True,
            duration_ms=self._clock_ms() - started,
            detail={"requested_ms": duration_ms},
        )
