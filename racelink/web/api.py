"""Flask API registration for the RaceLink web layer."""

from __future__ import annotations

import time

from flask import jsonify, request

from ..domain import (
    rl_preset_select_options,
    serialize_rl_preset_editor_schema,
    state_scope,
    wled_preset_select_options,
)
from ..domain.flags import USER_FLAG_KEYS
from ..services import OTAWorkflowService, SpecialsService
from ..services.scene_cost_estimator import estimate_scene, lora_parameters
from ..services.scenes_service import (
    GROUP_ID_MAX,
    KIND_OFFSET_GROUP,
    KIND_RL_PRESET,
    KIND_STARTBLOCK,
    KIND_WLED_CONTROL,
    KIND_WLED_PRESET,
    MAX_GROUPS_OFFSET_ENTRIES,
    MAX_OFFSET_GROUP_CHILDREN,
    OFFSET_FORMULA_MODES,
    OFFSET_GROUP_CHILD_KINDS,
    OFFSET_MS_MAX,
    OFFSET_MS_MIN,
    SceneService,
    get_action_kinds_metadata,
)
from .dto import group_counts, serialize_device
from .request_helpers import (
    RequestParseError,
    parse_recv3_from_addr,
    parse_wifi_options,
    require_int,
)


def _sse_refresh(ctx, scopes) -> None:
    """Broadcast an SSE ``refresh`` event derived from a state-scope set.

    Central helper so WebUI topics stay in sync with plugin-side scope tokens
    (one source of truth in :mod:`racelink.domain.state_scope`).
    """
    what = state_scope.sse_what_from_scopes(scopes)
    if not what:
        return
    ctx.sse.broadcast("refresh", {"what": what})


def _apply_device_meta_updates(
    ctx,
    *,
    macs: list,
    new_group,
    new_name,
) -> int:
    """Apply rename + regroup updates (plan P2-4, deadlock-fix).

    **Important locking rule:** we must NOT hold ``ctx.rl_lock`` across the
    blocking ``setNodeGroupId`` call. That lock is the same one
    ``GatewayService.handle_ack_event`` acquires when the reply comes back
    over USB. If we hold it while waiting for the ACK, the reader thread
    stalls in ``handle_ack_event`` for the previous device, USB frames for
    the current device stack up in pyserial's RX buffer, and the current
    device times out even though its ACK is sitting in the queue.

    So the lock scope here is limited to the in-memory mutations. The TX
    itself runs lock-free (the transport has its own thread safety).
    """
    changed = 0
    for mac in macs:
        with ctx.rl_lock:
            dev = ctx.rl_instance.getDeviceFromAddress(mac)
            if dev is None:
                continue
            if new_name and isinstance(new_name, str) and len(macs) == 1:
                dev.name = new_name
                changed += 1
            if new_group is None:
                continue
            dev.groupId = int(new_group)
        # Lock released -- the reader thread can now drain ACKs from the
        # previous iteration and complete matches for the *current* one.
        try:
            ctx.rl_instance.setNodeGroupId(dev)
            changed += 1
        except Exception as ex:
            # swallow-ok: best-effort fallback; caller proceeds with safe default
            ctx.log(f"RaceLink: setNodeGroupId failed for {mac}: {ex}")
    return changed


def _prepare_discover_target(ctx, *, target_gid, new_group_name):
    """Create a group if requested and return ``(target_gid, created_gid)``.

    Extracted from ``api_discover`` (plan P2-4) so the locking+group-creation
    logic can be unit-tested without a Flask request context.
    """
    created_gid = None
    with ctx.rl_lock:
        if new_group_name:
            group = ctx.RL_DeviceGroup(str(new_group_name), static_group=0, dev_type=0)
            if ctx.group_repo is not None:
                created_gid = ctx.group_repo.append(group)
            else:
                ctx.rl_grouplist.append(group)
                created_gid = len(ctx.rl_grouplist) - 1
            ctx.log(f"RaceLink: Created group '{new_group_name}' (id={created_gid})")
        if target_gid is None and created_gid is not None:
            target_gid = created_gid
    return target_gid, created_gid


def _resolve_special_config_request(ctx, body, specials_service):
    """Parse+validate a ``/api/specials/config`` body. Returns ``(ok, payload, status)``.

    On success, ``payload`` is a dict with the validated request data; on
    failure, ``payload`` is an error dict and ``status`` is the HTTP code.
    Extracted from ``api_specials_config`` (plan P2-4).
    """
    mac = body.get("mac", None)
    key = body.get("key", None)
    value = body.get("value", None)
    if not mac or not key:
        return False, {"ok": False, "error": "missing mac/key"}, 400

    recv3 = parse_recv3_from_addr(mac)
    if not recv3:
        return False, {"ok": False, "error": "invalid mac/address"}, 400
    if recv3 == b"\xFF\xFF\xFF":
        return False, {"ok": False, "error": "broadcast not allowed for config"}, 400

    try:
        value_int = int(value)
    except Exception:
        # swallow-ok: bad user input -> 400, not a bug
        return False, {"ok": False, "error": "invalid value"}, 400

    mac_str = str(mac).upper()
    with ctx.rl_lock:
        dev = ctx.rl_instance.getDeviceFromAddress(mac_str)
        if not dev:
            return False, {"ok": False, "error": "device not found"}, 404
        option_info = specials_service.resolve_option(dev, key)

    if not option_info:
        return False, {"ok": False, "error": "option not supported for device"}, 400
    option = option_info.get("option", None)
    if option is None:
        return False, {"ok": False, "error": "option not writable"}, 400
    try:
        specials_service.validate_option_value(option_info, value_int)
    except ValueError as ex:
        return False, {"ok": False, "error": str(ex)}, 400

    return True, {
        "mac_str": mac_str,
        "key": key,
        "recv3": recv3,
        "option": option,
        "value_int": value_int,
    }, 200


def _gateway_status(ctx) -> dict:
    """Return a UI-friendly gateway readiness snapshot (plan P1-1)."""
    rl = ctx.rl_instance
    getter = getattr(rl, "gateway_status", None)
    if callable(getter):
        try:
            return getter()
        except Exception:
            # swallow-ok: best-effort fallback; caller proceeds with safe default
            # Fall through to the synthetic snapshot below rather than 500-ing.
            pass
    return {
        "ready": bool(getattr(rl, "ready", False)),
        "last_error": None,
        "failure_count": 0,
    }


def register_api_routes(bp, ctx):
    host_wifi_service = ctx.services["host_wifi"]
    ota_service = ctx.services["ota"]
    presets_service = ctx.services["presets"]
    rl_presets_service = ctx.services.get("rl_presets")
    scenes_service = ctx.services.get("scenes")
    scene_runner_service = ctx.services.get("scene_runner")
    specials_service = SpecialsService(rl_instance=ctx.rl_instance)
    ota_workflows = OTAWorkflowService(
        host_wifi_service=host_wifi_service,
        ota_service=ota_service,
        presets_service=presets_service,
    )


    @bp.route("/api/devices", methods=["GET"])
    def api_devices():
        with ctx.rl_lock:
            rows = [serialize_device(device) for device in ctx.devices()]
        return jsonify({"ok": True, "devices": rows})

    @bp.route("/api/specials", methods=["GET"])
    def api_specials():
        return jsonify({"ok": True, "specials": specials_service.get_serialized_config()})

    @bp.route("/api/groups", methods=["GET"])
    def api_groups():
        with ctx.rl_lock:
            counts = group_counts(ctx.devices())
            rows = [{
                "id": 0,
                "name": "Unconfigured",
                "static": False,
                "dev_type": 0,
                "device_count": int(counts.get(0, 0)),
            }]
            for gid, group in enumerate(ctx.groups()):
                name = getattr(group, "name", f"Group {gid}")
                if str(name).strip().lower() in {"unconfigured", "all wled nodes", "all wled devices"}:
                    continue
                rows.append({
                    "id": gid,
                    "name": name,
                    "static": bool(getattr(group, "static_group", 0)),
                    "dev_type": int(getattr(group, "dev_type", 0) or 0),
                    "device_count": int(counts.get(gid, 0)),
                })
        return jsonify({"ok": True, "groups": rows})

    @bp.route("/api/master", methods=["GET"])
    def api_master():
        gateway = _gateway_status(ctx)
        return jsonify({
            "ok": True,
            "master": ctx.sse.master.snapshot(),
            "task": ctx.tasks.snapshot(),
            "gateway": gateway,
        })

    @bp.route("/api/gateway", methods=["GET"])
    def api_gateway_status():
        return jsonify({"ok": True, "gateway": _gateway_status(ctx)})

    @bp.route("/api/health", methods=["GET"])
    def api_health():
        """Cheap liveness probe for the WebUI's auto-reconnect path.

        Kept separate from ``/api/master`` so the browser can hammer it during
        reconnect without paying for the full state roundtrip.
        """
        rl = getattr(ctx, "rl_instance", None)
        startup_done = bool(getattr(rl, "_startup_done", False)) if rl else True
        return jsonify({
            "ok": True,
            "ts": time.time(),
            "phase": "ready" if startup_done else "booting",
        })

    @bp.route("/api/gateway/retry", methods=["POST"])
    def api_gateway_retry():
        rl = ctx.rl_instance
        retry = getattr(rl, "retry_gateway", None)
        if callable(retry):
            status = retry()
        else:  # pragma: no cover - legacy host without retry helper
            status = _gateway_status(ctx)
        return jsonify({"ok": bool(status.get("ready")), "gateway": status})

    @bp.route("/api/task", methods=["GET"])
    def api_task():
        return jsonify({"ok": True, "task": ctx.tasks.snapshot()})

    @bp.route("/api/options", methods=["GET"])
    def api_options():
        return jsonify({"ok": True, "presets": wled_preset_select_options(context={"rl_instance": ctx.rl_instance})})

    @bp.route("/api/discover", methods=["POST"])
    def api_discover():
        ctx.sse.ensure_transport_hooked(ctx.rl_instance)
        if ctx.tasks.is_running():
            return ctx.tasks.busy_response()

        body = request.get_json(silent=True) or {}
        target_gid, created_gid = _prepare_discover_target(
            ctx,
            target_gid=body.get("targetGroupId", None),
            new_group_name=body.get("newGroupName", None),
        )

        def do_discover():
            add_to_group = -1
            if target_gid not in (None, 0, "0"):
                add_to_group = int(target_gid)
            found = int(ctx.rl_instance.getDevices(groupFilter=0, addToGroup=add_to_group) or 0)
            return {"found": found, "createdGroupId": created_gid, "targetGroupId": target_gid}

        task = ctx.tasks.start("discover", do_discover, meta={"createdGroupId": created_gid, "targetGroupId": target_gid})
        if not task:
            return ctx.tasks.busy_response()
        return jsonify({"ok": True, "task": task})

    @bp.route("/api/status", methods=["POST"])
    def api_status():
        ctx.sse.ensure_transport_hooked(ctx.rl_instance)
        if ctx.tasks.is_running():
            return ctx.tasks.busy_response()

        body = request.get_json(silent=True) or {}
        selection = body.get("selection") or body.get("macs") or []
        group_id = body.get("groupId", None)

        def do_status():
            updated = 0
            if selection:
                if hasattr(ctx.rl_instance, "getStatusSelection"):
                    updated = int(ctx.rl_instance.getStatusSelection(selection) or 0)
                else:
                    for mac in selection:
                        dev = ctx.rl_instance.getDeviceFromAddress(mac)
                        if dev:
                            updated += int(ctx.rl_instance.getStatus(targetDevice=dev) or 0)
            elif group_id is not None:
                updated = int(ctx.rl_instance.getStatus(groupFilter=int(group_id)) or 0)
            else:
                updated = int(ctx.rl_instance.getStatus(groupFilter=255) or 0)
            return {"updated": updated, "groupId": group_id, "selectionCount": len(selection) if selection else 0}

        task = ctx.tasks.start("status", do_status, meta={"groupId": group_id, "selectionCount": len(selection) if selection else 0})
        if not task:
            return ctx.tasks.busy_response()
        return jsonify({"ok": True, "task": task})

    @bp.route("/api/devices/update-meta", methods=["POST"])
    def api_devices_update_meta():
        body = request.get_json(silent=True) or {}
        macs = body.get("macs") or []
        new_group = body.get("groupId", None)
        new_name = body.get("name", None)

        if new_group is not None:
            ctx.sse.ensure_transport_hooked(ctx.rl_instance)

        changed = _apply_device_meta_updates(
            ctx,
            macs=macs,
            new_group=new_group,
            new_name=new_name,
        )

        scopes: set[str] = set()
        if new_group is not None:
            scopes.add(state_scope.DEVICE_MEMBERSHIP)
        if new_name is not None:
            scopes.add(state_scope.DEVICES)
        if not scopes:
            scopes.add(state_scope.NONE)

        try:
            ctx.rl_instance.save_to_db({"manual": True}, scopes=scopes)
        except Exception:
            # swallow-ok: best-effort fallback; caller proceeds with safe default
            ctx.log("RaceLink: save_to_db after update-meta failed")

        _sse_refresh(ctx, scopes)
        return jsonify({"ok": True, "changed": changed})

    @bp.route("/api/groups/create", methods=["POST"])
    def api_groups_create():
        body = request.get_json(silent=True) or {}
        name = str(body.get("name", "")).strip()
        dev_type = int(body.get("dev_type", body.get("device_type", 0)) or 0)
        if not name:
            return jsonify({"ok": False, "error": "name required"}), 400
        with ctx.rl_lock:
            if ctx.group_repo is not None:
                gid = ctx.group_repo.append(ctx.RL_DeviceGroup(name, static_group=0, dev_type=dev_type))
            else:
                ctx.rl_grouplist.append(ctx.RL_DeviceGroup(name, static_group=0, dev_type=dev_type))
                gid = len(ctx.rl_grouplist) - 1
            try:
                ctx.rl_instance.save_to_db(
                    {"manual": True}, scopes={state_scope.GROUPS}
                )
            except Exception:
                # swallow-ok: best-effort fallback; caller proceeds with safe default
                pass
        _sse_refresh(ctx, {state_scope.GROUPS})
        return jsonify({"ok": True, "id": gid})

    @bp.route("/api/groups/rename", methods=["POST"])
    def api_groups_rename():
        body = request.get_json(silent=True) or {}
        # B1: was ``int(body.get("id"))`` which crashes the route with a
        # 500 on missing or null id; require_int returns a clean 400
        # validation error instead.
        try:
            gid = require_int(body, "id", label="group id")
        except RequestParseError as ex:
            return jsonify({"ok": False, "error": str(ex)}), 400
        name = str(body.get("name", "")).strip()
        with ctx.rl_lock:
            if gid < 0 or gid >= len(ctx.groups()):
                return jsonify({"ok": False, "error": "invalid group id"}), 400
            group = ctx.groups()[gid]
            if getattr(group, "static_group", 0):
                return jsonify({"ok": False, "error": "static group"}), 400
            group.name = name or group.name
            try:
                ctx.rl_instance.save_to_db(
                    {"manual": True}, scopes={state_scope.GROUPS}
                )
            except Exception:
                # swallow-ok: best-effort fallback; caller proceeds with safe default
                pass
        _sse_refresh(ctx, {state_scope.GROUPS})
        return jsonify({"ok": True})

    @bp.route("/api/groups/delete", methods=["POST"])
    def api_groups_delete():
        body = request.get_json(silent=True) or {}
        # B1: same fix as api_groups_rename — guard against
        # missing/null id with a clean 400 instead of a 500.
        try:
            gid = require_int(body, "id", label="group id")
        except RequestParseError as ex:
            return jsonify({"ok": False, "error": str(ex)}), 400
        with ctx.rl_lock:
            if gid < 0 or gid >= len(ctx.groups()):
                return jsonify({"ok": False, "error": "invalid group id"}), 400
            group = ctx.groups()[gid]
            if getattr(group, "static_group", 0):
                return jsonify({"ok": False, "error": "static group"}), 400
            for device in ctx.devices():
                if int(getattr(device, "groupId", -1)) == gid:
                    return jsonify({"ok": False, "error": "group not empty"}), 400
            if ctx.group_repo is not None:
                ctx.group_repo.remove(gid)
            else:
                del ctx.rl_grouplist[gid]
            try:
                ctx.rl_instance.save_to_db(
                    {"manual": True}, scopes={state_scope.GROUPS}
                )
            except Exception:
                # swallow-ok: best-effort fallback; caller proceeds with safe default
                pass
        _sse_refresh(ctx, {state_scope.GROUPS})
        return jsonify({"ok": True})

    @bp.route("/api/groups/force", methods=["POST"])
    def api_groups_force():
        if ctx.tasks.is_running():
            return ctx.tasks.busy_response()
        try:
            ctx.rl_instance.forceGroups(args=None, sanityCheck=True)
        except Exception as ex:
            # swallow-ok: best-effort fallback; caller proceeds with safe default
            ctx.log(f"RaceLink: forceGroups failed: {ex}")
            return jsonify({"ok": False, "error": str(ex)}), 500
        _sse_refresh(ctx, {state_scope.DEVICE_MEMBERSHIP})
        return jsonify({"ok": True})

    @bp.route("/api/save", methods=["POST"])
    def api_save():
        if ctx.tasks.is_running():
            return ctx.tasks.busy_response()
        try:
            ctx.rl_instance.save_to_db(
                {"manual": True}, scopes={state_scope.NONE}
            )
        except Exception as ex:
            # swallow-ok: best-effort fallback; caller proceeds with safe default
            return jsonify({"ok": False, "error": str(ex)}), 500
        return jsonify({"ok": True})

    @bp.route("/api/reload", methods=["POST"])
    def api_reload():
        if ctx.tasks.is_running():
            return ctx.tasks.busy_response()
        try:
            ctx.rl_instance.load_from_db()
        except Exception as ex:
            # swallow-ok: best-effort fallback; caller proceeds with safe default
            return jsonify({"ok": False, "error": str(ex)}), 500
        _sse_refresh(ctx, {state_scope.FULL})
        return jsonify({"ok": True})

    @bp.route("/api/config", methods=["POST"])
    def api_config():
        if ctx.tasks.is_running():
            return ctx.tasks.busy_response()

        body = request.get_json(silent=True) or {}
        macs = body.get("macs") or []
        mac = body.get("mac", None)
        if mac and not macs:
            macs = [mac]
        if len(macs) != 1:
            return jsonify({"ok": False, "error": "select exactly one device"}), 400

        recv3 = parse_recv3_from_addr(macs[0])
        if not recv3:
            return jsonify({"ok": False, "error": "invalid mac/address"}), 400
        if recv3 == b"\xFF\xFF\xFF":
            return jsonify({"ok": False, "error": "broadcast not allowed for config"}), 400

        try:
            option = int(body.get("option", 0)) & 0xFF
            data0 = int(body.get("data0", body.get("flags", 0))) & 0xFF
            data1 = int(body.get("data1", 0)) & 0xFF
            data2 = int(body.get("data2", 0)) & 0xFF
            data3 = int(body.get("data3", 0)) & 0xFF
        except Exception:
            # swallow-ok: best-effort fallback; caller proceeds with safe default
            return jsonify({"ok": False, "error": "invalid option/data"}), 400

        if option not in {0x01, 0x03, 0x04, 0x80, 0x81}:
            return jsonify({"ok": False, "error": "unknown config option"}), 400

        try:
            if hasattr(ctx.rl_instance, "sendConfig"):
                ctx.rl_instance.sendConfig(option=option, data0=data0, data1=data1, data2=data2, data3=data3, recv3=recv3)
            else:
                ctx.rl_instance.transport.send_config(recv3=recv3, option=option, data0=data0, data1=data1, data2=data2, data3=data3)
        except Exception as ex:
            # swallow-ok: best-effort fallback; caller proceeds with safe default
            ctx.log(f"RaceLink: config failed: {ex}")
            return jsonify({"ok": False, "error": str(ex)}), 500

        ctx.sse.master.set(state="TX", tx_pending=True, last_event="CONFIG_SENT")
        return jsonify({"ok": True, "sent": 1, "recv3": recv3.hex().upper(), "option": option, "data0": data0, "data1": data1, "data2": data2, "data3": data3})

    @bp.route("/api/specials/config", methods=["POST"])
    def api_specials_config():
        if ctx.tasks.is_running():
            return ctx.tasks.busy_response()

        body = request.get_json(silent=True) or {}
        ok, payload, status = _resolve_special_config_request(ctx, body, specials_service)
        if not ok:
            return jsonify(payload), status

        mac_str = payload["mac_str"]
        key = payload["key"]
        recv3 = payload["recv3"]
        option = payload["option"]
        value_int = payload["value_int"]

        ctx.sse.ensure_transport_hooked(ctx.rl_instance)

        def do_special_config():
            ctx.tasks.update(meta={"mac": mac_str, "key": key, "message": f"Sending {key} (0x{int(option):02X})"})
            ok = ctx.rl_instance.sendConfig(option=int(option) & 0xFF, data0=value_int, recv3=recv3, wait_for_ack=True, timeout_s=6.0)
            if not ok:
                raise RuntimeError(f"ACK timeout for option 0x{int(option):02X}")
            with ctx.rl_lock:
                dev2 = ctx.rl_instance.getDeviceFromAddress(mac_str)
                if not dev2:
                    raise RuntimeError("device not found")
                if not hasattr(dev2, "specials") or dev2.specials is None:
                    dev2.specials = {}
                dev2.specials[key] = int(value_int) & 0xFF
                try:
                    ctx.rl_instance.save_to_db(
                        {"manual": True}, scopes={state_scope.DEVICE_SPECIALS}
                    )
                except Exception:
                    # swallow-ok: best-effort fallback; caller proceeds with safe default
                    pass
            _sse_refresh(ctx, {state_scope.DEVICE_SPECIALS})
            return {"mac": mac_str, "key": key, "value": value_int}

        task = ctx.tasks.start("special_config", do_special_config, meta={"mac": mac_str, "key": key, "message": "Preparing special config"})
        if not task:
            return ctx.tasks.busy_response()
        return jsonify({"ok": True, "task": task})

    @bp.route("/api/specials/action", methods=["POST"])
    def api_specials_action():
        if ctx.tasks.is_running():
            return ctx.tasks.busy_response()

        body = request.get_json(silent=True) or {}
        mac = body.get("mac", None)
        fn_key = body.get("function", None) or body.get("fn", None)
        params = body.get("params", None) or {}
        if not mac or not fn_key:
            return jsonify({"ok": False, "error": "missing mac/function"}), 400

        recv3 = parse_recv3_from_addr(mac)
        if not recv3:
            return jsonify({"ok": False, "error": "invalid mac/address"}), 400
        if recv3 == b"\xFF\xFF\xFF":
            return jsonify({"ok": False, "error": "broadcast not allowed for action"}), 400

        mac_str = str(mac).upper()
        with ctx.rl_lock:
            dev = ctx.rl_instance.getDeviceFromAddress(mac_str)
            if not dev:
                return jsonify({"ok": False, "error": "device not found"}), 404
            fn_info, options_by_key = specials_service.resolve_action(dev, fn_key)

        if not fn_info:
            return jsonify({"ok": False, "error": "function not supported for device"}), 400
        if not fn_info.get("unicast", False):
            return jsonify({"ok": False, "error": "function does not support unicast"}), 400

        comm_name = fn_info.get("comm")
        if not comm_name:
            return jsonify({"ok": False, "error": "missing comm handler"}), 400
        comm_fn = getattr(ctx.rl_instance, comm_name, None)
        if not callable(comm_fn):
            return jsonify({"ok": False, "error": "comm handler not found"}), 400

        try:
            params_coerced = specials_service.coerce_action_params(fn_info, options_by_key, params)
        except ValueError as ex:
            return jsonify({"ok": False, "error": str(ex)}), 400

        ctx.sse.ensure_transport_hooked(ctx.rl_instance)
        with ctx.rl_lock:
            dev = ctx.rl_instance.getDeviceFromAddress(mac_str)
        if not dev:
            return jsonify({"ok": False, "error": "device not found"}), 404

        result = comm_fn(targetDevice=dev, targetGroup=None, params=params_coerced)
        if result is False:
            return jsonify({"ok": False, "error": "action failed"}), 500

        ctx.sse.master.set(state="TX", tx_pending=True, last_event="SPECIAL_SENT")
        return jsonify({"ok": True, "result": result, "function": fn_key, "params": params_coerced})

    @bp.route("/api/specials/get", methods=["POST"])
    def api_specials_get():
        return jsonify({"ok": False, "error": "not implemented"}), 501

    @bp.route("/api/devices/control", methods=["POST"])
    def api_devices_control():
        if ctx.tasks.is_running():
            return ctx.tasks.busy_response()
        body = request.get_json(silent=True) or {}
        macs = body.get("macs") or []
        group_id = body.get("groupId", None)

        def _toint(value, default=None):
            try:
                return int(value)
            except Exception:
                # swallow-ok: best-effort fallback; caller proceeds with safe default
                return default

        flags = _toint(body.get("flags", None), None)
        preset_id = _toint(body.get("presetId", None), None)
        brightness = _toint(body.get("brightness", None), None)
        if flags is None or preset_id is None or brightness is None:
            return jsonify({"ok": False, "error": "missing flags/presetId/brightness"}), 400

        # B2 cleanup: ``sendGroupControl`` was renamed to
        # ``sendGroupPreset`` in the Phase D rework; the old name no
        # longer exists on the controller, so the previous code path
        # always raised ``AttributeError`` and returned a confusing 500.
        # Removed the obsolete signature-compat ``except TypeError``
        # fallback at the same time. ``changed`` now reflects the
        # *actual* number of frames the transport accepted (B2): the
        # underlying send returns False when the gateway is offline, so
        # the route stops reporting ``changed: N`` for sends that
        # silently dropped on the floor.
        changed = 0
        try:
            if group_id is not None:
                try:
                    gid_int = int(group_id)
                except (TypeError, ValueError):
                    return jsonify({"ok": False, "error": "groupId must be an integer"}), 400
                if ctx.rl_instance.sendGroupPreset(gid_int, flags, preset_id, brightness):
                    changed = 1
            elif macs:
                for mac in macs:
                    dev = ctx.rl_instance.getDeviceFromAddress(mac)
                    if dev:
                        if ctx.rl_instance.sendRaceLink(dev, flags, preset_id, brightness):
                            changed += 1
            else:
                return jsonify({"ok": False, "error": "missing macs or groupId"}), 400
        except Exception as ex:
            # swallow-ok: log and translate to 500 for any unexpected
            # path (e.g. a device-repository raises during lookup).
            ctx.log(f"RaceLink: control failed: {ex}")
            return jsonify({"ok": False, "error": str(ex)}), 500

        ctx.sse.master.set(state="TX", tx_pending=True, last_event="CONTROL_SENT")
        return jsonify({"ok": True, "changed": changed})

    @bp.route("/api/fw/upload", methods=["POST"])
    def api_fw_upload():
        if ctx.tasks.is_running():
            return ctx.tasks.busy_response()
        try:
            info = ota_service.store_upload(request.files.get("file", None), (request.form.get("kind") or "").strip().lower())
            return jsonify({"ok": True, "file": {k: info[k] for k in ("id", "kind", "name", "size", "sha256", "uploaded_ts")}})
        except Exception as ex:
            # swallow-ok: best-effort fallback; caller proceeds with safe default
            return jsonify({"ok": False, "error": str(ex)}), 400

    @bp.route("/api/presets/upload", methods=["POST"])
    def api_presets_upload():
        if ctx.tasks.is_running():
            return ctx.tasks.busy_response()
        file_obj = request.files.get("file", None)
        try:
            info = presets_service.store_uploaded_file(file_obj)
            return jsonify({"ok": True, "file": {"name": info["name"], "size": info["size"], "saved_ts": info["saved_ts"]}, "files": presets_service.list_files()})
        except Exception as ex:
            # swallow-ok: best-effort fallback; caller proceeds with safe default
            return jsonify({"ok": False, "error": str(ex)}), 400

    @bp.route("/api/presets/list", methods=["GET"])
    def api_presets_list():
        files = presets_service.list_files()
        current = presets_service.get_current_name()
        if current and not presets_service.preset_path_for_name(current):
            current = ""
        if not current and files:
            current = files[0]["name"]
        return jsonify({"ok": True, "files": files, "current": current})

    @bp.route("/api/presets/select", methods=["POST"])
    def api_presets_select():
        if ctx.tasks.is_running():
            return ctx.tasks.busy_response()
        body = request.get_json(silent=True) or {}
        name = str(body.get("name") or "").strip()
        path = presets_service.preset_path_for_name(name)
        if not path:
            return jsonify({"ok": False, "error": "presets file not found"}), 404
        if not presets_service.apply_from_path(path):
            return jsonify({"ok": False, "error": "failed to parse presets.json"}), 400
        presets_service.set_current_name(name)
        return jsonify({"ok": True, "current": name})

    # ------------------------------------------------------------------
    # Phase B: RaceLink-native presets (OPC_CONTROL_ADV parameter snapshots)
    # ------------------------------------------------------------------

    def _rl_presets_unavailable():
        return jsonify({"ok": False, "error": "rl_presets service not available"}), 503

    @bp.route("/api/rl-presets", methods=["GET"])
    def api_rl_presets_list():
        if rl_presets_service is None:
            return _rl_presets_unavailable()
        return jsonify({"ok": True, "presets": rl_presets_service.list()})

    @bp.route("/api/rl-presets/schema", methods=["GET"])
    def api_rl_presets_schema():
        """Return the 14-field editor schema with generators resolved.

        Phase D: the Specials ``wled_control`` action now only carries the
        preset-picker form; the full editor lives in ``dlgRlPresets`` and
        needs its own schema source (``RL_PRESET_EDITOR_SCHEMA``).
        """
        schema = serialize_rl_preset_editor_schema(
            context={"rl_instance": ctx.rl_instance}
        )
        return jsonify({"ok": True, "schema": schema})

    @bp.route("/api/rl-presets", methods=["POST"])
    def api_rl_presets_create():
        if rl_presets_service is None:
            return _rl_presets_unavailable()
        body = request.get_json(silent=True) or {}
        label = body.get("label")
        if not isinstance(label, str) or not label.strip():
            return jsonify({"ok": False, "error": "label is required"}), 400
        try:
            preset = rl_presets_service.create(
                label=label,
                params=body.get("params"),
                flags=body.get("flags"),
                key=body.get("key"),
            )
        except ValueError as ex:
            return jsonify({"ok": False, "error": str(ex)}), 400
        return jsonify({"ok": True, "preset": preset})

    @bp.route("/api/rl-presets/<key>", methods=["GET"])
    def api_rl_presets_get(key):
        if rl_presets_service is None:
            return _rl_presets_unavailable()
        preset = rl_presets_service.get(key)
        if preset is None:
            return jsonify({"ok": False, "error": "preset not found"}), 404
        return jsonify({"ok": True, "preset": preset})

    @bp.route("/api/rl-presets/<key>", methods=["PUT"])
    def api_rl_presets_update(key):
        if rl_presets_service is None:
            return _rl_presets_unavailable()
        body = request.get_json(silent=True) or {}
        try:
            preset = rl_presets_service.update(
                key,
                label=body.get("label"),
                params=body.get("params"),
                flags=body.get("flags"),
            )
        except ValueError as ex:
            return jsonify({"ok": False, "error": str(ex)}), 400
        if preset is None:
            return jsonify({"ok": False, "error": "preset not found"}), 404
        return jsonify({"ok": True, "preset": preset})

    @bp.route("/api/rl-presets/<key>", methods=["DELETE"])
    def api_rl_presets_delete(key):
        if rl_presets_service is None:
            return _rl_presets_unavailable()
        if not rl_presets_service.delete(key):
            return jsonify({"ok": False, "error": "preset not found"}), 404
        return jsonify({"ok": True})

    @bp.route("/api/rl-presets/<key>/duplicate", methods=["POST"])
    def api_rl_presets_duplicate(key):
        if rl_presets_service is None:
            return _rl_presets_unavailable()
        body = request.get_json(silent=True) or {}
        new_label = body.get("label")
        try:
            preset = rl_presets_service.duplicate(key, new_label=new_label)
        except ValueError as ex:
            return jsonify({"ok": False, "error": str(ex)}), 400
        if preset is None:
            return jsonify({"ok": False, "error": "preset not found"}), 404
        return jsonify({"ok": True, "preset": preset})

    # ------------------------------------------------------------------
    # Scenes — CRUD + run + editor-schema
    # ------------------------------------------------------------------

    def _scenes_unavailable():
        return jsonify({"ok": False, "error": "scenes service not available"}), 503

    def _runner_unavailable():
        return jsonify({"ok": False, "error": "scene runner not available"}), 503

    @bp.route("/api/scenes", methods=["GET"])
    def api_scenes_list():
        if scenes_service is None:
            return _scenes_unavailable()
        return jsonify({"ok": True, "scenes": scenes_service.list()})

    @bp.route("/api/scenes/editor-schema", methods=["GET"])
    def api_scenes_editor_schema():
        """Return the per-kind action editor schema for the WebUI.

        Live state (preset option lists) is resolved at request time from the
        RL-preset and WLED-preset services so the editor sees current values.
        """
        sl_ctx = {"rl_instance": ctx.rl_instance}
        # Per-kind UI hints. Reuses the ``select / slider / toggle`` widget
        # vocabulary already established by RL_PRESET_EDITOR_SCHEMA.
        ui_per_kind = {
            KIND_RL_PRESET: {
                "presetId": {
                    "widget": "select",
                    "options": rl_preset_select_options(context=sl_ctx),
                },
                "brightness": {"widget": "slider", "min": 0, "max": 255},
            },
            KIND_WLED_PRESET: {
                "presetId": {
                    "widget": "select",
                    "options": wled_preset_select_options(context=sl_ctx),
                },
                "brightness": {"widget": "slider", "min": 0, "max": 255},
            },
            KIND_WLED_CONTROL: {
                "presetId": {
                    "widget": "select",
                    "options": rl_preset_select_options(context=sl_ctx),
                },
                "brightness": {"widget": "slider", "min": 0, "max": 255},
            },
            KIND_STARTBLOCK: {
                "fn_key": {"widget": "select", "options": [
                    {"value": "startblock_control", "label": "Startblock Control"},
                ]},
            },
            "delay": {"duration_ms": {"widget": "slider", "min": 0, "max": 60000}},
            "sync": {},
        }
        kinds_out = []
        for entry in get_action_kinds_metadata():
            out = dict(entry)
            out["ui"] = ui_per_kind.get(entry["kind"], {})
            kinds_out.append(out)
        return jsonify({
            "ok": True,
            "kinds": kinds_out,
            "flag_keys": list(USER_FLAG_KEYS),
            # Top-level target kinds. ``offset_group`` is now an action kind
            # (a container with its own children), not a target kind, so the
            # legacy ``groups_offset`` target is gone.
            "target_kinds": ["group", "device"],
            "offset_group": {
                "max_groups":   MAX_GROUPS_OFFSET_ENTRIES,
                "max_children": MAX_OFFSET_GROUP_CHILDREN,
                "group_id":     {"min": 0, "max": GROUP_ID_MAX},
                "offset_ms":    {"min": OFFSET_MS_MIN, "max": OFFSET_MS_MAX},
                "modes":        list(OFFSET_FORMULA_MODES),
                "base_ms":      {"min": -32768, "max": 32767},
                "step_ms":      {"min": -32768, "max": 32767},
                "center":       {"min": 0,      "max": GROUP_ID_MAX},
                "cycle":        {"min": 1,      "max": 255},
                "supports_all_groups": True,
                "child_kinds":  list(OFFSET_GROUP_CHILD_KINDS),
                "child_target_kinds": ["scope", "group", "device"],
            },
            # Active LoRa parameters for the cost-estimator tooltip.
            "lora": lora_parameters(),
        })

    @bp.route("/api/scenes/<key>", methods=["GET"])
    def api_scenes_get(key):
        if scenes_service is None:
            return _scenes_unavailable()
        scene = scenes_service.get(key)
        if scene is None:
            return jsonify({"ok": False, "error": "scene not found"}), 404
        return jsonify({"ok": True, "scene": scene})

    @bp.route("/api/scenes", methods=["POST"])
    def api_scenes_create():
        if scenes_service is None:
            return _scenes_unavailable()
        body = request.get_json(silent=True) or {}
        label = body.get("label")
        if not isinstance(label, str) or not label.strip():
            return jsonify({"ok": False, "error": "label is required"}), 400
        try:
            scene = scenes_service.create(
                label=label,
                actions=body.get("actions"),
                key=body.get("key"),
            )
        except ValueError as ex:
            return jsonify({"ok": False, "error": str(ex)}), 400
        _sse_refresh(ctx, {state_scope.SCENES})
        return jsonify({"ok": True, "scene": scene})

    @bp.route("/api/scenes/<key>", methods=["PUT"])
    def api_scenes_update(key):
        if scenes_service is None:
            return _scenes_unavailable()
        body = request.get_json(silent=True) or {}
        try:
            scene = scenes_service.update(
                key,
                label=body.get("label"),
                actions=body.get("actions"),
            )
        except ValueError as ex:
            return jsonify({"ok": False, "error": str(ex)}), 400
        if scene is None:
            return jsonify({"ok": False, "error": "scene not found"}), 404
        _sse_refresh(ctx, {state_scope.SCENES})
        return jsonify({"ok": True, "scene": scene})

    @bp.route("/api/scenes/<key>", methods=["DELETE"])
    def api_scenes_delete(key):
        if scenes_service is None:
            return _scenes_unavailable()
        if not scenes_service.delete(key):
            return jsonify({"ok": False, "error": "scene not found"}), 404
        _sse_refresh(ctx, {state_scope.SCENES})
        return jsonify({"ok": True})

    @bp.route("/api/scenes/<key>/duplicate", methods=["POST"])
    def api_scenes_duplicate(key):
        if scenes_service is None:
            return _scenes_unavailable()
        body = request.get_json(silent=True) or {}
        new_label = body.get("label")
        try:
            scene = scenes_service.duplicate(key, new_label=new_label)
        except ValueError as ex:
            return jsonify({"ok": False, "error": str(ex)}), 400
        if scene is None:
            return jsonify({"ok": False, "error": "scene not found"}), 404
        _sse_refresh(ctx, {state_scope.SCENES})
        return jsonify({"ok": True, "scene": scene})

    def _known_group_ids_from_ctx() -> list:
        """Best-effort list of currently-known group ids for the optimizer.
        Falls back to an empty list when no device repository is wired."""
        try:
            ctrl = getattr(ctx.rl_instance, "controller", None) if ctx.rl_instance else None
            repo = getattr(ctrl, "device_repository", None) if ctrl else None
            if repo is None:
                return []
            ids: set[int] = set()
            for d in repo.list():
                gid = getattr(d, "groupId", None)
                if isinstance(gid, int) and 0 <= gid <= 254:
                    ids.add(gid)
            return sorted(ids)
        except Exception:
            # swallow-ok: optimizer has a no-known-devices fallback; an
            # estimate is best-effort observability, not a hard contract.
            return []

    def _rl_preset_lookup_for_estimator():
        """Mirror ``_lookup_rl_preset`` from the runner so the estimator
        can resolve the same references the runner would. Returns ``None``
        if the rl-presets service isn't wired (estimator falls back to the
        action's own params, under-reporting but never crashing)."""
        if rl_presets_service is None:
            return None
        def lookup(ref):
            try:
                if isinstance(ref, str) and ref.startswith("RL:"):
                    return rl_presets_service.get(ref[3:])
                if isinstance(ref, int):
                    return rl_presets_service.get_by_id(ref)
                if isinstance(ref, str):
                    stripped = ref.strip()
                    if stripped.isdigit():
                        return rl_presets_service.get_by_id(int(stripped))
                    return rl_presets_service.get(stripped)
            except Exception:
                # swallow-ok: estimate path never blocks the editor.
                return None
            return None
        return lookup

    def _scene_cost_payload(scene_dict) -> dict:
        cost = estimate_scene(scene_dict,
                              known_group_ids=_known_group_ids_from_ctx(),
                              rl_preset_lookup=_rl_preset_lookup_for_estimator())
        return {
            "ok": True,
            "total": {
                "packets":    cost.total.packets,
                "bytes":      cost.total.bytes,
                "airtime_ms": cost.total.airtime_ms,
            },
            "per_action": [
                {
                    "packets":    a.packets,
                    "bytes":      a.bytes,
                    "airtime_ms": a.airtime_ms,
                    "detail":     a.detail or {},
                }
                for a in cost.per_action
            ],
            "lora": lora_parameters(),
        }

    @bp.route("/api/scenes/<key>/estimate", methods=["GET"])
    def api_scenes_estimate(key):
        """Return projected wire cost (packets, bytes, airtime) for a saved
        scene. The editor uses this to render the per-action cost badge and
        the scene-level total."""
        if scenes_service is None:
            return _scenes_unavailable()
        scene = scenes_service.get(key)
        if scene is None:
            return jsonify({"ok": False, "error": "scene not found"}), 404
        return jsonify(_scene_cost_payload(scene))

    @bp.route("/api/scenes/estimate", methods=["POST"])
    def api_scenes_estimate_draft():
        """Estimate cost for an unsaved draft. Body shape mirrors POST/PUT
        scene: ``{label?, actions: [...]}``. Validates the actions through
        the canonical validator (so the operator sees errors immediately
        on bad input) and then runs the estimator on the canonical form."""
        if scenes_service is None:
            return _scenes_unavailable()
        body = request.get_json(silent=True) or {}
        try:
            # Round-trip the actions through the validator without touching
            # storage. ``replace_all`` is too heavy; we only need canonical
            # actions, so we build a fake scene dict.
            from ..services.scenes_service import _canonical_actions  # local import
            canonical_actions = _canonical_actions(body.get("actions") or [])
        except ValueError as ex:
            return jsonify({"ok": False, "error": str(ex)}), 400
        scene_dict = {
            "label": (body.get("label") or "").strip() or "draft",
            "actions": canonical_actions,
        }
        return jsonify(_scene_cost_payload(scene_dict))

    @bp.route("/api/scenes/<key>/run", methods=["POST"])
    def api_scenes_run(key):
        """Run a scene synchronously and return the per-action result.

        v1: synchronous request. The HTTP response holds open until the
        runner finishes. ``delay`` actions are capped at 60 s each by the
        service validator, and total scenes are bounded at 20 actions, so
        worst-case wall time is 20 minutes — but realistic scenes finish in
        seconds.

        R7: per-action progress is emitted on the SSE bus (topic
        ``scene_progress``) before each action starts and after it returns.
        The bus is a separate connection from this request so broadcasting
        during the synchronous run does not block the response. The
        consumer (scenes.js) updates per-row borders live; the post-run
        result strip still comes from the JSON payload returned here.
        """
        if scenes_service is None:
            return _scenes_unavailable()
        if scene_runner_service is None:
            return _runner_unavailable()

        def _emit_progress(payload):
            ctx.sse.broadcast("scene_progress", payload)

        result = scene_runner_service.run(key, progress_cb=_emit_progress)
        if not result.ok and result.error == "scene_not_found":
            return jsonify(result.to_dict()), 404
        return jsonify({"ok": result.ok, "result": result.to_dict()})

    @bp.route("/api/fw/uploads", methods=["GET"])
    def api_fw_uploads():
        return jsonify({"ok": True, "files": ota_service.list_uploads()})

    @bp.route("/api/wifi/interfaces", methods=["GET"])
    def api_wifi_interfaces():
        return jsonify({"ok": True, "ifaces": host_wifi_service.wifi_interfaces()})

    @bp.route("/api/presets/download", methods=["POST"])
    def api_presets_download():
        ctx.sse.ensure_transport_hooked(ctx.rl_instance)
        if ctx.tasks.is_running():
            return ctx.tasks.busy_response()

        body = request.get_json(silent=True) or {}
        mac = str(body.get("mac") or "").strip()
        if not mac:
            return jsonify({"ok": False, "error": "missing mac"}), 400
        wifi = parse_wifi_options(body, ota_service)

        expected_mac = ota_service.expected_mac_hex(mac)
        if not expected_mac:
            return jsonify({"ok": False, "error": "invalid mac"}), 400

        def do_presets_download():
            return ota_workflows.download_presets(
                rl_instance=ctx.rl_instance,
                task_manager=ctx.tasks,
                mac=mac,
                base_url=wifi["base_url"],
                wifi=wifi,
                host_wifi_enable=wifi["host_wifi_enable"],
                host_wifi_restore=wifi["host_wifi_restore"],
            )

        task = ctx.tasks.start("presets_download", do_presets_download, meta={"stage": "INIT", "addr": mac, "message": "Preset download started", "baseUrl": wifi["base_url"]})
        if not task:
            return ctx.tasks.busy_response()
        return jsonify({"ok": True, "task": task})

    @bp.route("/api/fw/start", methods=["POST"])
    def api_fw_start():
        ctx.sse.ensure_transport_hooked(ctx.rl_instance)
        if ctx.tasks.is_running():
            return ctx.tasks.busy_response()

        body = request.get_json(silent=True) or {}
        macs = body.get("macs") or []
        if not isinstance(macs, list) or not macs:
            return jsonify({"ok": False, "error": "missing macs"}), 400

        do_firmware = bool(body.get("doFirmware", True))
        do_presets = bool(body.get("doPresets", False))
        do_cfg = bool(body.get("doCfg", False))
        if not (do_firmware or do_presets or do_cfg):
            return jsonify({"ok": False, "error": "no operations selected"}), 400

        fw_info = ota_service.get_upload(str(body.get("fwId") or "").strip(), expect_kind="firmware") if do_firmware else None
        if do_firmware and not fw_info:
            return jsonify({"ok": False, "error": "firmware file not uploaded (fwId)"}), 400

        presets_info = None
        if do_presets:
            presets_name = str(body.get("presetsName") or "").strip()
            presets_path = presets_service.preset_path_for_name(presets_name) if presets_name else None
            if not presets_path:
                return jsonify({"ok": False, "error": "presets file not found"}), 400
            presets_info = presets_service.file_info(presets_path, name=presets_name)

        cfg_info = ota_service.get_upload(str(body.get("cfgId") or "").strip(), expect_kind="cfg") if do_cfg else None
        if do_cfg and not cfg_info:
            return jsonify({"ok": False, "error": "cfg file not uploaded (cfgId)"}), 400

        try:
            retries = int(body.get("retries") or 3)
        except Exception:
            # swallow-ok: best-effort fallback; caller proceeds with safe default
            retries = 3
        retries = max(1, min(retries, 10))
        wifi = parse_wifi_options(body, ota_service)
        stop_on_error = bool(body.get("stopOnError") or False)

        def do_fwupdate():
            return ota_workflows.run_firmware_update(
                rl_instance=ctx.rl_instance,
                task_manager=ctx.tasks,
                devices_provider=ctx.devices,
                macs=macs,
                base_url=wifi["base_url"],
                fw_info=fw_info,
                presets_info=presets_info,
                cfg_info=cfg_info,
                retries=retries,
                stop_on_error=stop_on_error,
                wifi=wifi,
                host_wifi_enable=wifi["host_wifi_enable"],
                host_wifi_restore=wifi["host_wifi_restore"],
            )

        task = ctx.tasks.start("fwupdate", do_fwupdate, meta={"stage": "INIT", "index": 0, "total": len(macs), "retries": retries, "addr": None, "message": "Firmware update started", "baseUrl": wifi["base_url"]})
        if not task:
            return ctx.tasks.busy_response()
        return jsonify({"ok": True, "task": task})

    return {"ensure_presets_loaded": presets_service.ensure_loaded}
