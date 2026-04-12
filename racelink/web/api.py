"""Flask API registration for the RaceLink web layer."""

from __future__ import annotations

from flask import jsonify, request

from ..domain import effect_select_options
from ..services import OTAWorkflowService, SpecialsService
from .dto import group_counts, serialize_device
from .request_helpers import parse_recv3_from_addr, parse_wifi_options


def register_api_routes(bp, ctx):
    host_wifi_service = ctx.services["host_wifi"]
    ota_service = ctx.services["ota"]
    presets_service = ctx.services["presets"]
    specials_service = SpecialsService(rl_instance=ctx.rl_instance)
    ota_workflows = OTAWorkflowService(
        host_wifi_service=host_wifi_service,
        ota_service=ota_service,
        presets_service=presets_service,
    )


    @bp.route("/racelink/api/devices", methods=["GET"])
    def api_devices():
        with ctx.rl_lock:
            rows = [serialize_device(device) for device in ctx.devices()]
        return jsonify({"ok": True, "devices": rows})

    @bp.route("/racelink/api/specials", methods=["GET"])
    def api_specials():
        return jsonify({"ok": True, "specials": specials_service.get_serialized_config()})

    @bp.route("/racelink/api/groups", methods=["GET"])
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

    @bp.route("/racelink/api/master", methods=["GET"])
    def api_master():
        return jsonify({"ok": True, "master": ctx.sse.master.snapshot(), "task": ctx.tasks.snapshot()})

    @bp.route("/racelink/api/task", methods=["GET"])
    def api_task():
        return jsonify({"ok": True, "task": ctx.tasks.snapshot()})

    @bp.route("/racelink/api/options", methods=["GET"])
    def api_options():
        return jsonify({"ok": True, "effects": effect_select_options(context={"rl_instance": ctx.rl_instance})})

    @bp.route("/racelink/api/discover", methods=["POST"])
    def api_discover():
        ctx.sse.ensure_transport_hooked(ctx.rl_instance)
        if ctx.tasks.is_running():
            return ctx.tasks.busy_response()

        body = request.get_json(silent=True) or {}
        target_gid = body.get("targetGroupId", None)
        new_group_name = body.get("newGroupName", None)
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

    @bp.route("/racelink/api/status", methods=["POST"])
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

    @bp.route("/racelink/api/devices/update-meta", methods=["POST"])
    def api_devices_update_meta():
        body = request.get_json(silent=True) or {}
        macs = body.get("macs") or []
        new_group = body.get("groupId", None)
        new_name = body.get("name", None)

        changed = 0
        if new_group is not None:
            ctx.sse.ensure_transport_hooked(ctx.rl_instance)
        with ctx.rl_lock:
            for mac in macs:
                dev = ctx.rl_instance.getDeviceFromAddress(mac)
                if not dev:
                    continue
                if new_name and isinstance(new_name, str) and len(macs) == 1:
                    dev.name = new_name
                    changed += 1
                if new_group is not None:
                    try:
                        dev.groupId = int(new_group)
                        ctx.rl_instance.setNodeGroupId(dev)
                        changed += 1
                    except Exception as ex:
                        ctx.log(f"RaceLink: setNodeGroupId failed for {mac}: {ex}")
        try:
            ctx.rl_instance.save_to_db({"manual": True})
        except Exception:
            pass

        ctx.sse.broadcast("refresh", {"what": ["groups", "devices"]})
        return jsonify({"ok": True, "changed": changed})

    @bp.route("/racelink/api/groups/create", methods=["POST"])
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
                ctx.rl_instance.save_to_db({"manual": True})
            except Exception:
                pass
        ctx.sse.broadcast("refresh", {"what": ["groups"]})
        return jsonify({"ok": True, "id": gid})

    @bp.route("/racelink/api/groups/rename", methods=["POST"])
    def api_groups_rename():
        body = request.get_json(silent=True) or {}
        gid = int(body.get("id"))
        name = str(body.get("name", "")).strip()
        with ctx.rl_lock:
            if gid < 0 or gid >= len(ctx.groups()):
                return jsonify({"ok": False, "error": "invalid group id"}), 400
            group = ctx.groups()[gid]
            if getattr(group, "static_group", 0):
                return jsonify({"ok": False, "error": "static group"}), 400
            group.name = name or group.name
            try:
                ctx.rl_instance.save_to_db({"manual": True})
            except Exception:
                pass
        ctx.sse.broadcast("refresh", {"what": ["groups"]})
        return jsonify({"ok": True})

    @bp.route("/racelink/api/groups/delete", methods=["POST"])
    def api_groups_delete():
        body = request.get_json(silent=True) or {}
        gid = int(body.get("id"))
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
                ctx.rl_instance.save_to_db({"manual": True})
            except Exception:
                pass
        ctx.sse.broadcast("refresh", {"what": ["groups"]})
        return jsonify({"ok": True})

    @bp.route("/racelink/api/groups/force", methods=["POST"])
    def api_groups_force():
        if ctx.tasks.is_running():
            return ctx.tasks.busy_response()
        try:
            ctx.rl_instance.forceGroups(args=None, sanityCheck=True)
        except Exception as ex:
            ctx.log(f"RaceLink: forceGroups failed: {ex}")
            return jsonify({"ok": False, "error": str(ex)}), 500
        ctx.sse.broadcast("refresh", {"what": ["groups", "devices"]})
        return jsonify({"ok": True})

    @bp.route("/racelink/api/save", methods=["POST"])
    def api_save():
        if ctx.tasks.is_running():
            return ctx.tasks.busy_response()
        try:
            ctx.rl_instance.save_to_db({"manual": True})
        except Exception as ex:
            return jsonify({"ok": False, "error": str(ex)}), 500
        return jsonify({"ok": True})

    @bp.route("/racelink/api/reload", methods=["POST"])
    def api_reload():
        if ctx.tasks.is_running():
            return ctx.tasks.busy_response()
        try:
            ctx.rl_instance.load_from_db()
        except Exception as ex:
            return jsonify({"ok": False, "error": str(ex)}), 500
        ctx.sse.broadcast("refresh", {"what": ["groups", "devices"]})
        return jsonify({"ok": True})

    @bp.route("/racelink/api/config", methods=["POST"])
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
            return jsonify({"ok": False, "error": "invalid option/data"}), 400

        if option not in {0x01, 0x03, 0x04, 0x80, 0x81}:
            return jsonify({"ok": False, "error": "unknown config option"}), 400

        try:
            if hasattr(ctx.rl_instance, "sendConfig"):
                ctx.rl_instance.sendConfig(option=option, data0=data0, data1=data1, data2=data2, data3=data3, recv3=recv3)
            else:
                ctx.rl_instance.transport.send_config(recv3=recv3, option=option, data0=data0, data1=data1, data2=data2, data3=data3)
        except Exception as ex:
            ctx.log(f"RaceLink: config failed: {ex}")
            return jsonify({"ok": False, "error": str(ex)}), 500

        ctx.sse.master.set(state="TX", tx_pending=True, last_event="CONFIG_SENT")
        return jsonify({"ok": True, "sent": 1, "recv3": recv3.hex().upper(), "option": option, "data0": data0, "data1": data1, "data2": data2, "data3": data3})

    @bp.route("/racelink/api/specials/config", methods=["POST"])
    def api_specials_config():
        if ctx.tasks.is_running():
            return ctx.tasks.busy_response()

        body = request.get_json(silent=True) or {}
        mac = body.get("mac", None)
        key = body.get("key", None)
        value = body.get("value", None)
        if not mac or not key:
            return jsonify({"ok": False, "error": "missing mac/key"}), 400

        recv3 = parse_recv3_from_addr(mac)
        if not recv3:
            return jsonify({"ok": False, "error": "invalid mac/address"}), 400
        if recv3 == b"\xFF\xFF\xFF":
            return jsonify({"ok": False, "error": "broadcast not allowed for config"}), 400

        try:
            value_int = int(value)
        except Exception:
            return jsonify({"ok": False, "error": "invalid value"}), 400

        mac_str = str(mac).upper()
        with ctx.rl_lock:
            dev = ctx.rl_instance.getDeviceFromAddress(mac_str)
            if not dev:
                return jsonify({"ok": False, "error": "device not found"}), 404
            option_info = specials_service.resolve_option(dev, key)

        if not option_info:
            return jsonify({"ok": False, "error": "option not supported for device"}), 400
        option = option_info.get("option", None)
        if option is None:
            return jsonify({"ok": False, "error": "option not writable"}), 400
        try:
            specials_service.validate_option_value(option_info, value_int)
        except ValueError as ex:
            return jsonify({"ok": False, "error": str(ex)}), 400

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
                    ctx.rl_instance.save_to_db({"manual": True})
                except Exception:
                    pass
            return {"mac": mac_str, "key": key, "value": value_int}

        task = ctx.tasks.start("special_config", do_special_config, meta={"mac": mac_str, "key": key, "message": "Preparing special config"})
        if not task:
            return ctx.tasks.busy_response()
        return jsonify({"ok": True, "task": task})

    @bp.route("/racelink/api/specials/action", methods=["POST"])
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

    @bp.route("/racelink/api/specials/get", methods=["POST"])
    def api_specials_get():
        return jsonify({"ok": False, "error": "not implemented"}), 501

    @bp.route("/racelink/api/devices/control", methods=["POST"])
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
                return default

        flags = _toint(body.get("flags", None), None)
        preset_id = _toint(body.get("presetId", None), None)
        brightness = _toint(body.get("brightness", None), None)
        if flags is None or preset_id is None or brightness is None:
            return jsonify({"ok": False, "error": "missing flags/presetId/brightness"}), 400

        changed = 0
        try:
            if group_id is not None:
                try:
                    ctx.rl_instance.sendGroupControl(int(group_id), flags, preset_id, brightness)
                except TypeError:
                    ctx.rl_instance.sendGroupControl(int(group_id), int(bool(flags & 0x01)), preset_id, brightness)
                changed = 1
            elif macs:
                for mac in macs:
                    dev = ctx.rl_instance.getDeviceFromAddress(mac)
                    if dev:
                        try:
                            ctx.rl_instance.sendRaceLink(dev, flags, preset_id, brightness)
                        except TypeError:
                            ctx.rl_instance.sendRaceLink(dev, int(bool(flags & 0x01)), preset_id, brightness)
                        changed += 1
            else:
                return jsonify({"ok": False, "error": "missing macs or groupId"}), 400
        except Exception as ex:
            ctx.log(f"RaceLink: control failed: {ex}")
            return jsonify({"ok": False, "error": str(ex)}), 500

        ctx.sse.master.set(state="TX", tx_pending=True, last_event="CONTROL_SENT")
        return jsonify({"ok": True, "changed": changed})

    @bp.route("/racelink/api/fw/upload", methods=["POST"])
    def api_fw_upload():
        if ctx.tasks.is_running():
            return ctx.tasks.busy_response()
        try:
            info = ota_service.store_upload(request.files.get("file", None), (request.form.get("kind") or "").strip().lower())
            return jsonify({"ok": True, "file": {k: info[k] for k in ("id", "kind", "name", "size", "sha256", "uploaded_ts")}})
        except Exception as ex:
            return jsonify({"ok": False, "error": str(ex)}), 400

    @bp.route("/racelink/api/presets/upload", methods=["POST"])
    def api_presets_upload():
        if ctx.tasks.is_running():
            return ctx.tasks.busy_response()
        file_obj = request.files.get("file", None)
        try:
            info = presets_service.store_uploaded_file(file_obj)
            return jsonify({"ok": True, "file": {"name": info["name"], "size": info["size"], "saved_ts": info["saved_ts"]}, "files": presets_service.list_files()})
        except Exception as ex:
            return jsonify({"ok": False, "error": str(ex)}), 400

    @bp.route("/racelink/api/presets/list", methods=["GET"])
    def api_presets_list():
        files = presets_service.list_files()
        current = presets_service.get_current_name()
        if current and not presets_service.preset_path_for_name(current):
            current = ""
        if not current and files:
            current = files[0]["name"]
        return jsonify({"ok": True, "files": files, "current": current})

    @bp.route("/racelink/api/presets/select", methods=["POST"])
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

    @bp.route("/racelink/api/fw/uploads", methods=["GET"])
    def api_fw_uploads():
        return jsonify({"ok": True, "files": ota_service.list_uploads()})

    @bp.route("/racelink/api/wifi/interfaces", methods=["GET"])
    def api_wifi_interfaces():
        return jsonify({"ok": True, "ifaces": host_wifi_service.wifi_interfaces()})

    @bp.route("/racelink/api/presets/download", methods=["POST"])
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

    @bp.route("/racelink/api/fw/start", methods=["POST"])
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
