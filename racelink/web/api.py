"""Flask API registration for the RaceLink web layer."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple, Union

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from datetime import datetime

from flask import jsonify, request
from RHUI import UIFieldSelectOption

try:
    from ..domain import effect_select_options, get_dev_type_info, get_specials_config
    from .dto import group_counts, serialize_device
except Exception:  # pragma: no cover
    from racelink.domain import effect_select_options, get_dev_type_info, get_specials_config  # type: ignore
    from racelink.web.dto import group_counts, serialize_device  # type: ignore


def register_api_routes(bp, ctx):
    host_wifi_service = ctx.services["host_wifi"]
    ota_service = ctx.services["ota"]
    presets_service = ctx.services["presets"]

    def _parse_recv3_from_addr(addr_str):
        if addr_str is None:
            return None
        try:
            value = str(addr_str)
        except Exception:
            return None
        hexchars = "0123456789abcdefABCDEF"
        value = "".join(ch for ch in value if ch in hexchars)
        if len(value) < 6:
            return None
        try:
            return bytes.fromhex(value[-6:])
        except Exception:
            return None


    @bp.route("/racelink/api/devices", methods=["GET"])
    def api_devices():
        with ctx.rl_lock:
            rows = [serialize_device(device) for device in ctx.devices()]
        return jsonify({"ok": True, "devices": rows})

    @bp.route("/racelink/api/specials", methods=["GET"])
    def api_specials():
        return jsonify({"ok": True, "specials": get_specials_config(context={"rl_instance": ctx.rl_instance}, serialize_ui=True)})

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

        recv3 = _parse_recv3_from_addr(macs[0])
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
                ctx.rl_instance.lora.send_config(recv3=recv3, option=option, data0=data0, data1=data1, data2=data2, data3=data3)
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

        recv3 = _parse_recv3_from_addr(mac)
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
            dev_caps = set(get_dev_type_info(getattr(dev, "dev_type", 0)).get("caps", []))
            specials = get_specials_config(context={"rl_instance": ctx.rl_instance})
            option_info = None
            for cap in dev_caps:
                spec = specials.get(cap, {})
                for opt in spec.get("options", []):
                    if opt.get("key") == key:
                        option_info = opt
                        break
                if option_info:
                    break

        if not option_info:
            return jsonify({"ok": False, "error": "option not supported for device"}), 400
        option = option_info.get("option", None)
        if option is None:
            return jsonify({"ok": False, "error": "option not writable"}), 400
        min_v = option_info.get("min")
        max_v = option_info.get("max")
        if min_v is not None and value_int < int(min_v):
            return jsonify({"ok": False, "error": f"value must be >= {min_v}"}), 400
        if max_v is not None and value_int > int(max_v):
            return jsonify({"ok": False, "error": f"value must be <= {max_v}"}), 400

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

        recv3 = _parse_recv3_from_addr(mac)
        if not recv3:
            return jsonify({"ok": False, "error": "invalid mac/address"}), 400
        if recv3 == b"\xFF\xFF\xFF":
            return jsonify({"ok": False, "error": "broadcast not allowed for action"}), 400

        mac_str = str(mac).upper()
        with ctx.rl_lock:
            dev = ctx.rl_instance.getDeviceFromAddress(mac_str)
            if not dev:
                return jsonify({"ok": False, "error": "device not found"}), 404
            dev_caps = set(get_dev_type_info(getattr(dev, "dev_type", 0)).get("caps", []))
            specials = get_specials_config(context={"rl_instance": ctx.rl_instance})
            fn_info = None
            options_by_key = {}
            for cap in dev_caps:
                spec = specials.get(cap, {})
                for opt in spec.get("options", []):
                    key_name = opt.get("key")
                    if key_name:
                        options_by_key[key_name] = opt
                for fn in spec.get("functions", []):
                    if fn.get("key") == fn_key:
                        fn_info = fn
                        break
                if fn_info:
                    break

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

        params_coerced = {}
        for var in fn_info.get("vars", []) or []:
            raw_val = params.get(var, None)
            if raw_val is None:
                raw_val = options_by_key.get(var, {}).get("min", 0)
            try:
                value_int = int(raw_val)
            except Exception:
                return jsonify({"ok": False, "error": f"invalid value for {var}"}), 400
            opt_meta = options_by_key.get(var, {})
            min_v = opt_meta.get("min")
            max_v = opt_meta.get("max")
            if min_v is not None and value_int < int(min_v):
                return jsonify({"ok": False, "error": f"{var} must be >= {min_v}"}), 400
            if max_v is not None and value_int > int(max_v):
                return jsonify({"ok": False, "error": f"{var} must be <= {max_v}"}), 400
            params_coerced[var] = value_int

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
        base_url = ota_service.wled_base_url(body.get("baseUrl") or "")
        wifi = body.get("wifi") or {}
        wifi_ssid = str(wifi.get("ssid") or body.get("wifiSsid") or "WLED-AP")
        wifi_iface = str(wifi.get("iface") or body.get("wifiIface") or "wlan0")
        wifi_conn_name = str(wifi.get("connName") or body.get("wifiConnName") or "racelink-wled-ap")
        wifi_bssid = str(wifi.get("bssid") or body.get("wifiBssid") or "")
        wifi_timeout_s = float(wifi.get("timeoutS") or body.get("wifiTimeoutS") or 35.0)
        host_wifi_enable = bool(wifi.get("hostWifiEnable") if "hostWifiEnable" in wifi else body.get("hostWifiEnable", True))
        host_wifi_restore = bool(wifi.get("hostWifiRestore") if "hostWifiRestore" in wifi else body.get("hostWifiRestore", True))

        expected_mac = ota_service.expected_mac_hex(mac)
        if not expected_mac:
            return jsonify({"ok": False, "error": "invalid mac"}), 400

        def do_presets_download():
            results = {"ok": True, "baseUrl": base_url, "addr": mac, "file": None, "errors": []}
            host_wifi_initial = host_wifi_service.radio_enabled()
            host_wifi_changed = False
            results["hostWifi"] = {"wasEnabled": host_wifi_initial, "enabled": host_wifi_initial, "restored": False}

            try:
                if host_wifi_enable and not host_wifi_initial:
                    ctx.tasks.update(meta={"stage": "HOST_WIFI_ON", "addr": mac, "message": "Enabling host WiFi radio..."})
                    host_wifi_service.set_radio(True)
                    host_wifi_changed = True
                    host_wifi_service.wait_iface_ready(wifi_iface, timeout_s=15.0)
                    results["hostWifi"]["enabled"] = True

                ctx.tasks.update(meta={"stage": "LORA_AP_ON", "addr": mac, "message": "Enable WLED AP via LoRa (waiting for ACK)"})
                ok_ap = ctx.rl_instance.sendConfig(0x04, data0=1, recv3=ota_service.recv3_bytes_from_addr(mac), wait_for_ack=True, timeout_s=8.0)
                if not ok_ap:
                    raise RuntimeError(f"Timeout waiting for CONFIG ACK from {mac}")

                ctx.tasks.update(meta={"stage": "CONNECT_WIFI", "addr": mac, "message": f'Connecting host WiFi via profile "{wifi_conn_name}" (iface {wifi_iface}) to SSID "{wifi_ssid}"'})
                try:
                    host_wifi_service.connect_profile(wifi_conn_name, wifi_ssid, iface=wifi_iface, bssid=wifi_bssid, timeout_s=wifi_timeout_s)
                except Exception as ex1:
                    msg1 = str(ex1)
                    if host_wifi_enable and (not host_wifi_changed) and ("Wi-Fi is disabled" in msg1 or "wireless is disabled" in msg1.lower()):
                        ctx.tasks.update(meta={"stage": "HOST_WIFI_ON", "addr": mac, "message": f"Host WiFi appears disabled; enabling on {wifi_iface}..."})
                        host_wifi_service.set_radio(True)
                        host_wifi_changed = True
                        results["hostWifi"]["enabled"] = True
                        host_wifi_service.wait_iface_ready(wifi_iface, timeout_s=15.0)
                        host_wifi_service.connect_profile(wifi_conn_name, wifi_ssid, iface=wifi_iface, bssid=wifi_bssid, timeout_s=wifi_timeout_s)
                    else:
                        raise

                ctx.tasks.update(meta={"stage": "WAIT_HTTP", "addr": mac, "message": f"Waiting for WLED /json/info mac to match {expected_mac}"})
                info = ota_service.wait_for_expected_node(base_url, expected_mac, timeout_s=90.0, poll_s=1.0)
                if not info:
                    raise RuntimeError(f"Timeout waiting for node (baseUrl={base_url}) to report expected mac {expected_mac}")

                ctx.tasks.update(meta={"stage": "DOWNLOAD_PRESETS", "addr": mac, "message": "Downloading presets.json"})
                payload = ota_service.wled_download_presets(base_url, timeout_s=15.0)
                saved = presets_service.save_payload(payload)
                results["file"] = {k: saved[k] for k in ("name", "size", "saved_ts")}
                results["files"] = presets_service.list_files()

                try:
                    ctx.rl_instance.sendConfig(0x04, data0=0, recv3=ota_service.recv3_bytes_from_addr(mac), wait_for_ack=True, timeout_s=6.0)
                except Exception:
                    pass
            except Exception as ex:
                results["ok"] = False
                results["errors"].append(str(ex))
            finally:
                if host_wifi_restore and (host_wifi_initial is False) and host_wifi_service.radio_enabled():
                    try:
                        try:
                            host_wifi_service.profile_down(wifi_conn_name, timeout_s=10.0)
                        except Exception:
                            pass
                        host_wifi_service.set_radio(False)
                        results["hostWifi"]["enabled"] = False
                        results["hostWifi"]["restored"] = True
                    except Exception as ex2:
                        results["errors"].append(f"Host WiFi restore failed: {ex2}")
                        results["ok"] = False
            return results

        task = ctx.tasks.start("presets_download", do_presets_download, meta={"stage": "INIT", "addr": mac, "message": "Preset download started", "baseUrl": base_url})
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
        base_url = ota_service.wled_base_url(body.get("baseUrl") or "")
        stop_on_error = bool(body.get("stopOnError") or False)
        wifi = body.get("wifi") or {}
        wifi_ssid = str(wifi.get("ssid") or body.get("wifiSsid") or "WLED-AP")
        wifi_iface = str(wifi.get("iface") or body.get("wifiIface") or "wlan0")
        wifi_conn_name = str(wifi.get("connName") or body.get("wifiConnName") or "racelink-wled-ap")
        wifi_bssid = str(wifi.get("bssid") or body.get("wifiBssid") or "")
        wifi_timeout_s = float(wifi.get("timeoutS") or body.get("wifiTimeoutS") or 35.0)
        host_wifi_enable = bool(wifi.get("hostWifiEnable") if "hostWifiEnable" in wifi else body.get("hostWifiEnable", True))
        host_wifi_restore = bool(wifi.get("hostWifiRestore") if "hostWifiRestore" in wifi else body.get("hostWifiRestore", True))

        def do_fwupdate():
            results = {"ok": True, "baseUrl": base_url, "devices": [], "errors": []}
            host_wifi_initial = host_wifi_service.radio_enabled()
            host_wifi_changed = False
            results["hostWifi"] = {"wasEnabled": host_wifi_initial, "enabled": host_wifi_initial, "restored": False}
            if fw_info:
                results["fw"] = {k: fw_info[k] for k in ("id", "name", "size", "sha256")}
            if presets_info:
                results["presets"] = {k: presets_info[k] for k in ("name", "size", "sha256")}
            if cfg_info:
                results["cfg"] = {k: cfg_info[k] for k in ("id", "name", "size", "sha256")}

            try:
                if host_wifi_enable and not host_wifi_initial:
                    host_wifi_service.set_radio(True)
                    host_wifi_changed = True
                    host_wifi_service.wait_iface_ready(wifi_iface, timeout_s=15.0)
                    results["hostWifi"]["enabled"] = True

                total = len(macs)
                for idx, addr in enumerate(macs, start=1):
                    expected_mac = ota_service.expected_mac_hex(str(addr))
                    dev_res = {"addr": addr, "expectedMac": expected_mac, "groupId": ota_service.lookup_group_id_for_addr(str(addr), ctx.devices()), "ok": False, "error": None}
                    results["devices"].append(dev_res)
                    try:
                        ctx.tasks.update(meta={"stage": "LORA_AP_ON", "index": idx, "total": total, "addr": addr, "retries": retries, "message": "Enable WLED AP via LoRa"})
                        ctx.rl_instance.sendConfig(0x04, data0=1, recv3=ota_service.recv3_bytes_from_addr(str(addr)))
                        ctx.tasks.update(meta={"stage": "CONNECT_WIFI", "index": idx, "total": total, "addr": addr, "retries": retries, "message": f'Connecting host WiFi via profile "{wifi_conn_name}" (iface {wifi_iface}) to SSID "{wifi_ssid}"'})
                        host_wifi_service.connect_profile(wifi_conn_name, wifi_ssid, iface=wifi_iface, bssid=wifi_bssid, timeout_s=wifi_timeout_s)
                        ctx.tasks.update(meta={"stage": "WAIT_HTTP", "index": idx, "total": total, "addr": addr, "retries": retries, "message": f"Waiting for WLED /json/info mac to match {expected_mac}"})
                        info = ota_service.wait_for_expected_node(base_url, expected_mac, timeout_s=90.0, poll_s=1.0)
                        if not info:
                            raise RuntimeError(f"Timeout waiting for node (baseUrl={base_url}) to report expected mac {expected_mac}")
                        dev_res["info_before"] = {k: info.get(k) for k in ("mac", "ver", "arch", "name")}

                        if presets_info:
                            ota_service.wled_upload_file(base_url, presets_info["path"], timeout_s=45.0, dest_name="presets.json")
                        if cfg_info:
                            ota_service.wled_upload_file(base_url, cfg_info["path"], timeout_s=45.0, dest_name="cfg.json")
                        if fw_info:
                            ok = False
                            last_err = None
                            for attempt in range(1, retries + 1):
                                try:
                                    ctx.tasks.update(meta={"stage": "UPLOAD_FW", "index": idx, "total": total, "addr": addr, "attempt": attempt, "retries": retries, "message": f"Uploading firmware (try {attempt}/{retries})"})
                                    ota_service.wled_upload_firmware(base_url, fw_info["path"], timeout_s=30.0)
                                    ok = True
                                    break
                                except Exception as ex:
                                    last_err = ex
                                    time.sleep(2.0)
                            if not ok:
                                raise RuntimeError(f"Firmware upload failed: {last_err}")
                        dev_res["ok"] = True
                    except Exception as ex:
                        dev_res["error"] = str(ex)
                        results["errors"].append(str(ex))
                        if stop_on_error:
                            raise
                    finally:
                        try:
                            ctx.rl_instance.sendConfig(0x04, data0=0, recv3=ota_service.recv3_bytes_from_addr(str(addr)))
                        except Exception:
                            pass
            except Exception:
                results["ok"] = False
            finally:
                if host_wifi_restore and (host_wifi_initial is False) and host_wifi_service.radio_enabled():
                    try:
                        try:
                            host_wifi_service.profile_down(wifi_conn_name, timeout_s=10.0)
                        except Exception:
                            pass
                        host_wifi_service.set_radio(False)
                        results["hostWifi"]["enabled"] = False
                        results["hostWifi"]["restored"] = True
                    except Exception as ex2:
                        results["errors"].append(f"Host WiFi restore failed: {ex2}")
            return results

        task = ctx.tasks.start("fwupdate", do_fwupdate, meta={"stage": "INIT", "index": 0, "total": len(macs), "retries": retries, "addr": None, "message": "Firmware update started", "baseUrl": base_url})
        if not task:
            return ctx.tasks.busy_response()
        return jsonify({"ok": True, "task": task})

    return {"ensure_presets_loaded": presets_service.ensure_loaded}
