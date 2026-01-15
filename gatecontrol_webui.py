"""
GateControl WebUI (importable module)
-------------------------------------

This module registers a Flask blueprint for the GateControl LoRa plugin.

Key goals:
- No periodic polling required (uses Server-Sent Events / SSE for live UI state)
- "Busy" protection: only one long-running radio task at a time (discover/status)
- UI can show master activity (TX pending / RX window open) based on USB events
- UI can show task progress (RX reply counts) based on LoRa/USB events

Usage in your plugin's __init__.py:

    from .gatecontrol_webui import register_gc_blueprint

    def initialize(rhapi):
        ...
        register_gc_blueprint(
            rhapi,
            gc_instance=gc_instance,
            gc_devicelist=gc_devicelist,
            gc_grouplist=gc_grouplist,
            GC_DeviceGroup=GC_DeviceGroup,
            logger=logger
        )

This registers the page at /gatecontrol and JSON endpoints under /gatecontrol/api/*.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple, Union

import json
import time
import threading

import os
import re
from RHUI import UIFieldSelectOption
import tempfile
import hashlib
import uuid
import subprocess
import shutil

from flask import Blueprint, request, jsonify, templating, Response, stream_with_context

# Use gevent lock/queue if available, otherwise fallback to threading primitives
try:
    from gevent.lock import Semaphore as _GCLock  # type: ignore
    _DefaultLock = _GCLock
except Exception:  # pragma: no cover
    try:
        from gevent.lock import RLock as _GCLock  # type: ignore
        _DefaultLock = _GCLock
    except Exception:  # pragma: no cover
        _DefaultLock = threading.Lock

try:
    from gevent.queue import Queue as _GCQueue  # type: ignore
except Exception:  # pragma: no cover
    try:
        from queue import Queue as _GCQueue  # type: ignore
    except Exception:  # pragma: no cover
        _GCQueue = None  # should never happen


def register_gc_blueprint(
    rhapi,
    *,
    gc_instance,
    gc_devicelist,
    gc_grouplist,
    GC_DeviceGroup,
    logger=None
):
    """
    Register the GateControl blueprint with RotorHazard.

    All references are passed explicitly to avoid tight coupling with __init__.py globals.
    """

    _gc_lock = _DefaultLock()
    _clients_lock = _DefaultLock()
    _task_lock = _DefaultLock()

    # --- helpers ---
    def _log(msg):
        try:
            if logger:
                logger.info(msg)
            else:
                print(msg)
        except Exception:
            print(msg)

    # --- Master state mirrored to UI ---
    _master = {
        "state": "IDLE",              # IDLE | TX | RX | ERROR
        "tx_pending": False,
        "rx_window_open": False,
        "rx_window_ms": 0,
        "last_event": None,
        "last_event_ts": 0.0,
        "last_tx_len": 0,
        "last_rx_count_delta": 0,
        "last_error": None,
    }

    # --- Task state (one at a time) ---
    _task = None  # dict or None
    _task_seq = 0

    # --- SSE clients ---
    _clients = set()  # set[Queue]

    def _master_snapshot():
        return dict(_master)

    def _task_snapshot():
        with _task_lock:
            return dict(_task) if _task else None

    def _broadcast(ev_name: str, payload):
        # Fan-out to all connected SSE clients
        with _clients_lock:
            dead = []
            for q in list(_clients):
                try:
                    q.put((ev_name, payload), timeout=0.01)
                except Exception:
                    dead.append(q)
            for q in dead:
                try:
                    _clients.remove(q)
                except Exception:
                    pass

    def _set_master(**updates):
        changed = False
        for k, v in updates.items():
            if _master.get(k) != v:
                _master[k] = v
                changed = True
        if changed:
            _master["last_event_ts"] = time.time()
            _broadcast("master", _master_snapshot())

    def _set_task(new_task: Optional[dict]):
        nonlocal _task
        with _task_lock:
            _task = new_task
        _broadcast("task", _task_snapshot())

    def _task_update(**updates):
        with _task_lock:
            if not _task:
                return
            for k, v in updates.items():
                _task[k] = v
        _broadcast("task", _task_snapshot())

    def _task_is_running() -> bool:
        with _task_lock:
            return bool(_task and _task.get("state") == "running")

    def _task_busy_response():
        snap = _task_snapshot()
        return jsonify({"ok": False, "busy": True, "task": snap}), 409

    # --- Transport event hookup (LoRaUSB.on_event) ---
    _hooked_lora = {"ok": False}

    def _ensure_transport_hooked():
        """
        Attach a callback to LoRa transport events so we can update master/task state
        and feed SSE clients.

        Prefers a listener registry (lora.add_listener) when available; otherwise
        falls back to chaining lora.on_event.
        """
        if _hooked_lora["ok"]:
            return

        lora = getattr(gc_instance, "lora", None)
        if not lora:
            return

        # Newer transport: listener registry
        if hasattr(lora, "add_listener"):
            try:
                lora.add_listener(_on_transport_event)  # type: ignore[attr-defined]
                _hooked_lora["ok"] = True
                _log("GateControl: transport event listener installed (add_listener)")
                return
            except Exception as ex:
                _log(f"GateControl: add_listener failed, falling back to on_event: {ex}")

        # Legacy transport: single on_event callback
        if not hasattr(lora, "on_event"):
            return

        prev = getattr(lora, "on_event", None)

        def _mux(ev: dict):
            # 1) our handler
            try:
                _on_transport_event(ev)
            except Exception:
                pass
            # 2) previous handler (if any)
            try:
                if prev and prev is not _mux:
                    prev(ev)
            except Exception:
                pass

        try:
            lora.on_event = _mux
            _hooked_lora["ok"] = True
            _log("GateControl: transport event hook installed")
        except Exception as ex:
            _log(f"GateControl: transport hook failed: {ex}")

    # Event type constants from gc_transport (optional)
    try:
        from .gc_transport import EV_ERROR, EV_RX_WINDOW_OPEN, EV_RX_WINDOW_CLOSED, EV_TX_DONE  # type: ignore
    except Exception:
        try:
            from gc_transport import EV_ERROR, EV_RX_WINDOW_OPEN, EV_RX_WINDOW_CLOSED, EV_TX_DONE  # type: ignore
        except Exception:
            EV_ERROR = 0xF0
            EV_RX_WINDOW_OPEN = 0xF1
            EV_RX_WINDOW_CLOSED = 0xF2
            EV_TX_DONE = 0xF3

    def _on_transport_event(ev: dict):
        """
        Receive USB transport events (EV_*) and LoRa reply events and update UI state.
        """
        t = ev.get("type", None)

        # USB-only events
        if t == EV_RX_WINDOW_OPEN:
            _set_master(
                state="RX",
                rx_window_open=True,
                rx_window_ms=int(ev.get("window_ms", 0) or 0),
                last_event="RX_WINDOW_OPEN",
                last_error=None,
            )
            if _task_is_running():
                _task_update(rx_windows=int((_task_snapshot() or {}).get("rx_windows", 0)) + 1)
            return

        if t == EV_RX_WINDOW_CLOSED:
            delta = int(ev.get("rx_count_delta", 0) or 0)
            # If no TX pending, fall back to IDLE. (TX_DONE will override if needed.)
            _set_master(
                state="TX" if _master.get("tx_pending") else "IDLE",
                rx_window_open=False,
                rx_window_ms=0,
                last_event="RX_WINDOW_CLOSED",
                last_rx_count_delta=delta,
                last_error=None,
            )
            if _task_is_running():
                snap = _task_snapshot() or {}
                _task_update(
                    rx_count_delta_total=int(snap.get("rx_count_delta_total", 0)) + delta,
                    rx_windows=int(snap.get("rx_windows", 0)) + 1,
                )
            return

        if t == EV_TX_DONE:
            _set_master(
                tx_pending=False,
                state="RX" if _master.get("rx_window_open") else "IDLE",
                last_event="TX_DONE",
                last_tx_len=int(ev.get("last_len", 0) or 0),
                last_error=None,
            )
            return

        if t == EV_ERROR:
            # Keep error state visible; tasks can continue but UI should show error
            raw = ev.get("data", b"")
            try:
                if isinstance(raw, (bytes, bytearray)):
                    raw = raw.hex().upper()
            except Exception:
                pass
            _set_master(
                state="ERROR",
                last_event="USB_ERROR",
                last_error=str(raw),
            )
            if _task_is_running():
                _task_update(last_error=str(raw))
            return

        # LoRa reply events from LoRaUSB parser
        reply = ev.get("reply")
        if not reply:
            return

        if reply == "ACK":
            with _clients_lock:
                has_clients = bool(_clients)
            if has_clients:
                _broadcast("refresh", {"what": ["devices"]})

        # Track replies for running tasks
        if _task_is_running():
            snap = _task_snapshot() or {}
            tname = snap.get("name")
            if tname == "discover" and reply == "IDENTIFY_REPLY":
                _task_update(rx_replies=int(snap.get("rx_replies", 0)) + 1)
            elif tname == "status" and reply == "STATUS_REPLY":
                _task_update(rx_replies=int(snap.get("rx_replies", 0)) + 1)

        # Update master activity hint (we received something)
        _set_master(last_event=reply, last_error=None)

    # --- Serialization ---
    def _gc_serialize_device(dev):
        """Make GC_Device JSON-serializable for the UI table."""
        # Online status (central link logic): always boolean, no timestamp gating.
        # Devices become online only when an expected reply is received; otherwise offline.
        online = bool(getattr(dev, "link_online", False))

        d = {
            "addr": getattr(dev, "addr", None),
            "name": getattr(dev, "name", None),
            "type": int(getattr(dev, "type", 0) or 0),
            "groupId": int(getattr(dev, "groupId", 0) or 0),

            # new proto v1.2 fields
            "flags": int(getattr(dev, "flags", 0) or 0),
            "configByte": int(getattr(dev, "configByte", 0) or 0),
            "presetId": int(getattr(dev, "presetId", 0) or 0),
            "brightness": int(getattr(dev, "brightness", 0) or 0),

            "voltage_mV": int(getattr(dev, "voltage_mV", 0) or 0),
            "node_rssi": int(getattr(dev, "node_rssi", 0) or 0),
            "node_snr": int(getattr(dev, "node_snr", 0) or 0),
            "host_rssi": int(getattr(dev, "host_rssi", 0) or 0),
            "host_snr": int(getattr(dev, "host_snr", 0) or 0),

            "version": int(getattr(dev, "version", 0) or 0),
            "caps": int(getattr(dev, "caps", 0) or 0),
            "last_seen_ts": float(getattr(dev, "last_seen_ts", 0.0) or 0.0),
            "last_ack": getattr(dev, "last_ack", None),
            "online": online,
        }
        return d

    def _gc_group_counts():
        counts = {}
        try:
            for dev in gc_devicelist:
                gid = int(getattr(dev, "groupId", 0) or 0)
                counts[gid] = counts.get(gid, 0) + 1
        except Exception:
            pass
        return counts

    bp = Blueprint(
        "gatecontrol",
        __name__,
        template_folder="pages",
        static_folder="static",
        static_url_path="/gatecontrol/static"
    )

    # -----------------------
    # Page
    # -----------------------
    @bp.route("/gatecontrol")
    def gc_render():
        _ensure_transport_hooked()
        return templating.render_template(
            "gatecontrol.html",
            serverInfo=None,
            getOption=rhapi.db.option,
            __=rhapi.__
        )

    # -----------------------
    # SSE Events
    # -----------------------
    @bp.route("/gatecontrol/api/events")
    def api_events():
        _ensure_transport_hooked()

        q = _GCQueue()
        with _clients_lock:
            _clients.add(q)

        # Push initial snapshots
        try:
            q.put(("master", _master_snapshot()), timeout=0.01)
            q.put(("task", _task_snapshot()), timeout=0.01)
        except Exception:
            pass

        def _encode(event_name: str, payload) -> str:
            return f"event: {event_name}\ndata: {json.dumps(payload, separators=(',',':'))}\n\n"

        @stream_with_context
        def gen():
            # Keep-alive ping every ~15s
            last_ping = time.time()
            try:
                while True:
                    try:
                        item = q.get(timeout=1.0)
                    except Exception:
                        item = None

                    now = time.time()
                    if item is None:
                        if now - last_ping >= 15.0:
                            last_ping = now
                            yield ": ping\n\n"
                        continue

                    ev_name, payload = item
                    yield _encode(ev_name, payload)
            finally:
                with _clients_lock:
                    try:
                        _clients.remove(q)
                    except Exception:
                        pass

        headers = {
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # harmless without nginx, helpful with proxies
        }
        return Response(gen(), mimetype="text/event-stream", headers=headers)

    # -----------------------
    # JSON API: Read
    # -----------------------
    @bp.route("/gatecontrol/api/devices", methods=["GET"])
    def api_devices():
        with _gc_lock:
            rows = [_gc_serialize_device(d) for d in gc_devicelist]
        return jsonify({"ok": True, "devices": rows})

    @bp.route("/gatecontrol/api/groups", methods=["GET"])
    def api_groups():
        with _gc_lock:
            group_rows = []
            counts = _gc_group_counts()
            for i, g in enumerate(gc_grouplist):
                group_rows.append({
                    "id": i,
                    "name": getattr(g, "name", f"Group {i}"),
                    "static": bool(getattr(g, "static_group", 0)),
                    "device_type": int(getattr(g, "device_type", 0) or 0),
                    "device_count": int(counts.get(i, 0)),
                })
        return jsonify({"ok": True, "groups": group_rows})

    @bp.route("/gatecontrol/api/master", methods=["GET"])
    def api_master():
        return jsonify({"ok": True, "master": _master_snapshot(), "task": _task_snapshot()})

    @bp.route("/gatecontrol/api/task", methods=["GET"])
    def api_task():
        return jsonify({"ok": True, "task": _task_snapshot()})

    @bp.route("/gatecontrol/api/options", methods=["GET"])
    def api_options():
        # still called "effects" for UI legacy; values can represent preset ids
        opts = []
        try:
            for opt in gc_instance.uiEffectList:
                val = getattr(opt, "value", None)
                lab = getattr(opt, "label", None) or getattr(opt, "name", None) or str(opt)
                if val is None:
                    continue
                opts.append({"value": str(val), "label": str(lab)})
        except Exception:
            opts = []
        return jsonify({"ok": True, "effects": opts})

    # -----------------------
    # JSON API: Actions (Tasks)
    # -----------------------
    def _start_task(name: str, target_fn, meta: Optional[dict] = None):
        nonlocal _task_seq, _task
        with _task_lock:
            if _task and _task.get("state") == "running":
                return None
            _task_seq += 1
            tid = _task_seq
            _task_obj = {
                "id": tid,
                "name": name,
                "state": "running",  # running|done|error
                "started_ts": time.time(),
                "ended_ts": None,
                "meta": meta or {},
                "rx_replies": 0,
                "rx_windows": 0,
                "rx_count_delta_total": 0,
                "last_error": None,
                "result": None,
            }
            _task = _task_obj

        _broadcast("task", _task_snapshot())

        def runner():
            try:
                # hint: something is going out now
                _set_master(state="TX", tx_pending=True, last_event=f"TASK_{name.upper()}_START")
                res = target_fn()
                _task_update(state="done", ended_ts=time.time(), result=res)
                _set_master(state="IDLE" if not _master.get("rx_window_open") else "RX", last_event=f"TASK_{name.upper()}_DONE")
                # Tell UI to refresh lists
                _broadcast("refresh", {"what": ["groups", "devices"]})
            except Exception as ex:
                _task_update(state="error", ended_ts=time.time(), last_error=str(ex))
                _set_master(state="ERROR", last_event=f"TASK_{name.upper()}_ERROR", last_error=str(ex))
            finally:
                pass

        th = threading.Thread(target=runner, daemon=True)
        th.start()
        return _task_snapshot()

    @bp.route("/gatecontrol/api/discover", methods=["POST"])
    def api_discover():
        _ensure_transport_hooked()
        if _task_is_running():
            return _task_busy_response()

        body = request.get_json(silent=True) or {}
        target_gid = body.get("targetGroupId", None)
        new_group_name = body.get("newGroupName", None)

        # group creation is cheap; do it before starting the radio task
        created_gid = None
        with _gc_lock:
            if new_group_name:
                g = GC_DeviceGroup(str(new_group_name), static_group=0, device_type=0)
                gc_grouplist.append(g)
                created_gid = len(gc_grouplist) - 1
                _log(f"GateControl: Created group '{new_group_name}' (id={created_gid})")
            if target_gid is None and created_gid is not None:
                target_gid = created_gid

        def do_discover():
            # Discovery: ask nodes with groupId=0 and assign to group
            n_found = int(gc_instance.getDevices(groupFilter=0, addToGroup=int(target_gid) if target_gid is not None else -1) or 0)
            return {"found": n_found, "createdGroupId": created_gid, "targetGroupId": target_gid}

        t = _start_task("discover", do_discover, meta={"createdGroupId": created_gid, "targetGroupId": target_gid})
        if not t:
            return _task_busy_response()
        return jsonify({"ok": True, "task": t})

    @bp.route("/gatecontrol/api/status", methods=["POST"])
    def api_status():
        _ensure_transport_hooked()
        if _task_is_running():
            return _task_busy_response()

        body = request.get_json(silent=True) or {}
        selection = body.get("selection") or body.get("macs") or []
        group_id = body.get("groupId", None)

        def do_status():
            updated = 0
            if selection:
                # If plugin has getStatusSelection(selection) prefer it
                if hasattr(gc_instance, "getStatusSelection"):
                    updated = int(gc_instance.getStatusSelection(selection) or 0)
                else:
                    for mac in selection:
                        dev = gc_instance.getDeviceFromAddress(mac)
                        if dev:
                            updated += int(gc_instance.getStatus(targetDevice=dev) or 0)
            elif group_id is not None:
                updated = int(gc_instance.getStatus(groupFilter=int(group_id)) or 0)
            else:
                updated = int(gc_instance.getStatus(groupFilter=255) or 0)
            return {"updated": updated, "groupId": group_id, "selectionCount": len(selection) if selection else 0}

        meta = {"groupId": group_id, "selectionCount": len(selection) if selection else 0}
        t = _start_task("status", do_status, meta=meta)
        if not t:
            return _task_busy_response()
        return jsonify({"ok": True, "task": t})

    # -----------------------
    # JSON API: Meta updates (group/name)
    # -----------------------
    @bp.route("/gatecontrol/api/devices/update-meta", methods=["POST"])
    def api_devices_update_meta():
        body = request.get_json(silent=True) or {}
        macs = body.get("macs") or []
        new_group = body.get("groupId", None)
        new_name = body.get("name", None)

        changed = 0
        with _gc_lock:
            for mac in macs:
                dev = gc_instance.getDeviceFromAddress(mac)
                if not dev:
                    continue
                if new_name and isinstance(new_name, str) and macs and len(macs) == 1:
                    dev.name = new_name
                    changed += 1
                if new_group is not None:
                    try:
                        gc_instance.setGateGroupId(dev, int(new_group))
                        changed += 1
                    except Exception as ex:
                        _log(f"GateControl: setGateGroupId failed for {mac}: {ex}")
        try:
            gc_instance.save_to_db({"manual": True})
        except Exception:
            pass

        _broadcast("refresh", {"what": ["groups", "devices"]})
        return jsonify({"ok": True, "changed": changed})

    # -----------------------
    # JSON API: Groups
    # -----------------------
    @bp.route("/gatecontrol/api/groups/create", methods=["POST"])
    def api_groups_create():
        body = request.get_json(silent=True) or {}
        name = str(body.get("name", "")).strip()
        device_type = int(body.get("device_type", 0) or 0)
        if not name:
            return jsonify({"ok": False, "error": "name required"}), 400
        with _gc_lock:
            gc_grouplist.append(GC_DeviceGroup(name, static_group=0, device_type=device_type))
            gid = len(gc_grouplist) - 1
            try:
                gc_instance.save_to_db({"manual": True})
            except Exception:
                pass
        _broadcast("refresh", {"what": ["groups"]})
        return jsonify({"ok": True, "id": gid})

    @bp.route("/gatecontrol/api/groups/rename", methods=["POST"])
    def api_groups_rename():
        body = request.get_json(silent=True) or {}
        gid = int(body.get("id"))
        name = str(body.get("name", "")).strip()
        with _gc_lock:
            if gid < 0 or gid >= len(gc_grouplist):
                return jsonify({"ok": False, "error": "invalid group id"}), 400
            g = gc_grouplist[gid]
            if getattr(g, "static_group", 0):
                return jsonify({"ok": False, "error": "static group"}), 400
            g.name = name or g.name
            try:
                gc_instance.save_to_db({"manual": True})
            except Exception:
                pass
        _broadcast("refresh", {"what": ["groups"]})
        return jsonify({"ok": True})

    @bp.route("/gatecontrol/api/groups/delete", methods=["POST"])
    def api_groups_delete():
        body = request.get_json(silent=True) or {}
        gid = int(body.get("id"))
        with _gc_lock:
            if gid < 0 or gid >= len(gc_grouplist):
                return jsonify({"ok": False, "error": "invalid group id"}), 400
            g = gc_grouplist[gid]
            if getattr(g, "static_group", 0):
                return jsonify({"ok": False, "error": "static group"}), 400
            for d in gc_devicelist:
                if int(getattr(d, "groupId", -1)) == gid:
                    return jsonify({"ok": False, "error": "group not empty"}), 400
            del gc_grouplist[gid]
            try:
                gc_instance.save_to_db({"manual": True})
            except Exception:
                pass
        _broadcast("refresh", {"what": ["groups"]})
        return jsonify({"ok": True})

    @bp.route("/gatecontrol/api/groups/force", methods=["POST"])
    def api_groups_force():
        if _task_is_running():
            return _task_busy_response()
        try:
            gc_instance.forceGroups(args=None, sanityCheck=True)
        except Exception as ex:
            _log(f"GateControl: forceGroups failed: {ex}")
            return jsonify({"ok": False, "error": str(ex)}), 500
        _broadcast("refresh", {"what": ["groups", "devices"]})
        return jsonify({"ok": True})

    # -----------------------
    # JSON API: Save/Reload
    # -----------------------
    @bp.route("/gatecontrol/api/save", methods=["POST"])
    def api_save():
        if _task_is_running():
            return _task_busy_response()
        try:
            gc_instance.save_to_db({"manual": True})
        except Exception as ex:
            return jsonify({"ok": False, "error": str(ex)}), 500
        return jsonify({"ok": True})

    @bp.route("/gatecontrol/api/reload", methods=["POST"])
    def api_reload():
        if _task_is_running():
            return _task_busy_response()
        try:
            gc_instance.load_from_db()
        except Exception as ex:
            return jsonify({"ok": False, "error": str(ex)}), 500
        _broadcast("refresh", {"what": ["groups", "devices"]})
        return jsonify({"ok": True})

    # -----------------------
    # JSON API: CONTROL (flags/presetId)
    # -----------------------

    @bp.route("/gatecontrol/api/config", methods=["POST"])
    def api_config():
        """Send unicast CONFIG packet to exactly one node (no broadcast)."""
        if _task_is_running():
            return _task_busy_response()

        body = request.get_json(silent=True) or {}
        macs = body.get("macs") or []
        mac = body.get("mac", None)
        if mac and not macs:
            macs = [mac]

        if len(macs) != 1:
            return jsonify({"ok": False, "error": "select exactly one device"}), 400

        def _parse_recv3_from_addr(addr_str) -> Optional[bytes]:
            """Parse address string (3B or 6B, with/without separators) and return last3 bytes."""
            if addr_str is None:
                return None
            try:
                s = str(addr_str)
            except Exception:
                return None
            hexchars = "0123456789abcdefABCDEF"
            s = "".join(ch for ch in s if ch in hexchars)
            if len(s) < 6:
                return None
            s = s[-6:]
            try:
                return bytes.fromhex(s)
            except Exception:
                return None

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

        config_options = {
            0x01,  # MAC filter enabled
            0x03,  # MAC filter persistency
            0x04,  # WLAN AP open
            0x80,  # forget master MAC
            0x81,  # reboot
        }
        if option not in config_options:
            return jsonify({"ok": False, "error": "unknown config option"}), 400

        try:
            # Prefer instance helper if present
            if hasattr(gc_instance, "sendConfig"):
                gc_instance.sendConfig(option=option, data0=data0, data1=data1, data2=data2, data3=data3, recv3=recv3)
            else:
                gc_instance.lora.send_config(recv3=recv3, option=option, data0=data0, data1=data1, data2=data2, data3=data3)
        except Exception as ex:
            _log(f"GateControl: config failed: {ex}")
            return jsonify({"ok": False, "error": str(ex)}), 500

        _set_master(state="TX", tx_pending=True, last_event="CONFIG_SENT")
        return jsonify({
            "ok": True,
            "sent": 1,
            "recv3": recv3.hex().upper(),
            "option": option,
            "data0": data0,
            "data1": data1,
            "data2": data2,
            "data3": data3,
        })

    @bp.route("/gatecontrol/api/devices/control", methods=["POST"])
    def api_devices_control():
        """
        CONTROL message:
          - per-device: {macs:[...], flags:int, presetId:int, brightness:int}
          - per-group:  {groupId:int, flags:int, presetId:int, brightness:int}
        """
        if _task_is_running():
            return _task_busy_response()

        body = request.get_json(silent=True) or {}
        macs = body.get("macs") or []
        group_id = body.get("groupId", None)

        flags = body.get("flags", None)
        presetId = body.get("presetId", None)
        brightness = body.get("brightness", None)

        def _toint(x, default=None):
            try:
                return int(x)
            except Exception:
                return default

        flags = _toint(flags, None)
        presetId = _toint(presetId, None)
        brightness = _toint(brightness, None)

        if flags is None or presetId is None or brightness is None:
            return jsonify({"ok": False, "error": "missing flags/presetId/brightness"}), 400

        changed = 0
        try:
            if group_id is not None:
                # Prefer new signature sendGroupControl(groupId, flags, presetId, brightness)
                try:
                    gc_instance.sendGroupControl(int(group_id), flags, presetId, brightness)
                except TypeError:
                    # fallback old signature if user hasn't applied proto patch (state/effect)
                    gc_instance.sendGroupControl(int(group_id), int(bool(flags & 0x01)), presetId, brightness)
                changed = 1
            elif macs:
                for mac in macs:
                    dev = gc_instance.getDeviceFromAddress(mac)
                    if dev:
                        try:
                            gc_instance.sendGateControl(dev, flags, presetId, brightness)
                        except TypeError:
                            gc_instance.sendGateControl(dev, int(bool(flags & 0x01)), presetId, brightness)
                        changed += 1
            else:
                return jsonify({"ok": False, "error": "missing macs or groupId"}), 400
        except Exception as ex:
            _log(f"GateControl: control failed: {ex}")
            return jsonify({"ok": False, "error": str(ex)}), 500

        _set_master(state="TX", tx_pending=True, last_event="CONTROL_SENT")
        return jsonify({"ok": True, "changed": changed})

    
    # -----------------------
    # Firmware Update (WLED OTA via HTTP)
    # -----------------------

    _upload_lock = _DefaultLock()
    _uploads = {}  # id -> {id, kind, path, name, size, sha256, uploaded_ts}

    def _uploads_dir() -> str:
        d = os.path.join(tempfile.gettempdir(), "gatecontrol_lora_uploads")
        os.makedirs(d, exist_ok=True)
        return d

    def _wifi_interfaces() -> List[str]:
        base = "/sys/class/net"
        ifaces: List[str] = []
        try:
            for name in os.listdir(base):
                if name.startswith("."):
                    continue
                if os.path.isdir(os.path.join(base, name, "wireless")):
                    ifaces.append(name)
        except Exception:
            ifaces = []
        if not ifaces:
            try:
                ifaces = [name for name in os.listdir(base) if not name.startswith(".")]
            except Exception:
                ifaces = []
        return sorted(set(ifaces))

    def _sha256_file(path: str) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 256), b""):
                h.update(chunk)
        return h.hexdigest()

    def _parse_wled_presets_minimal(presets: Union[str, bytes, Dict[str, Any]]) -> List[Tuple[int, str]]:
        if isinstance(presets, (str, bytes)):
            data = json.loads(presets)
        elif isinstance(presets, dict):
            data = presets
        else:
            raise TypeError("presets must be dict, str, or bytes")

        out: List[Tuple[int, str]] = []
        for key, preset_obj in data.items():
            try:
                preset_id = int(key)
            except (TypeError, ValueError):
                continue

            if not isinstance(preset_obj, dict):
                continue

            raw_name = preset_obj.get("n", "")
            name = raw_name.strip() if isinstance(raw_name, str) else ""
            if not name:
                name = f"Preset ID {preset_id}"
            out.append((preset_id, name))

        out.sort(key=lambda t: t[0])
        return out

    def _store_upload(file_storage, kind: str) -> dict:
        # kind: firmware | presets | cfg
        if not file_storage or not getattr(file_storage, "filename", ""):
            raise ValueError("missing file")
        kind = (kind or "").strip().lower()
        if kind not in ("firmware", "presets", "cfg"):
            raise ValueError("invalid kind")

        # sanitize filename
        filename = os.path.basename(file_storage.filename)
        if not filename:
            filename = f"{kind}.bin" if kind == "firmware" else f"{kind}.json"

        up_id = uuid.uuid4().hex[:12]
        dst = os.path.join(_uploads_dir(), f"{up_id}__{filename}")

        file_storage.save(dst)

        info = {
            "id": up_id,
            "kind": kind,
            "path": dst,
            "name": filename,
            "size": int(os.path.getsize(dst)),
            "sha256": _sha256_file(dst),
            "uploaded_ts": time.time(),
        }
        with _upload_lock:
            _uploads[up_id] = info
        return info

    def _get_upload(up_id: str, expect_kind: Optional[str] = None) -> Optional[dict]:
        if not up_id:
            return None
        with _upload_lock:
            info = _uploads.get(up_id)
        if not info:
            return None
        if expect_kind and info.get("kind") != expect_kind:
            return None
        if not os.path.exists(info.get("path", "")):
            return None
        return info

    def _norm_hex(s: str) -> str:
        return re.sub(r"[^0-9a-fA-F]", "", s or "").lower()

    def _expected_mac_hex(addr: str) -> str:
        """Return normalized 6-byte MAC (12 hex chars) from an address string."""
        hx = _norm_hex(addr)
        if len(hx) < 12:
            return ""
        return hx[-12:]

    def _expected_last3_hex(addr: str) -> str:
        """Return normalized last 3 bytes (6 hex chars) from an address string."""
        hx = _norm_hex(addr)
        if len(hx) < 6:
            return ""
        return hx[-6:]

    def _lookup_group_id_for_addr(addr: str) -> int:
        """Lookup the currently configured groupId for a given device address (best-effort)."""
        want = _expected_mac_hex(addr)
        if not want:
            return 0
        try:
            with _gc_lock:
                for dev in gc_devicelist:
                    have = _expected_mac_hex(str(getattr(dev, "addr", "") or ""))
                    if have and have.lower() == want.lower():
                        return int(getattr(dev, "groupId", 0) or 0)
        except Exception:
            pass
        return 0

    def _mac_hex_to_bssid(mac_hex: str) -> str:
        hx = _norm_hex(mac_hex)
        if len(hx) < 12:
            return ""
        hx = hx[-12:]
        return ":".join(hx[i:i+2] for i in range(0, 12, 2))

    def _recv3_bytes_from_addr(addr: str) -> bytes:
        last3 = _expected_last3_hex(addr)
        if len(last3) != 6:
            raise ValueError("invalid addr")
        return bytes.fromhex(last3)

    def _nmcli_run(args: list, timeout_s: float = 20.0) -> subprocess.CompletedProcess:
        if not shutil.which("nmcli"):
            raise RuntimeError("nmcli not available on host (cannot switch WiFi automatically)")
        p = subprocess.run(["nmcli"] + args, capture_output=True, text=True, timeout=max(1.0, timeout_s))
        return p

    def _host_wifi_radio_enabled() -> bool:
        """Return whether host WiFi radio is enabled (NetworkManager)."""
        try:
            p = _nmcli_run(["-t", "-f", "WIFI", "radio"], timeout_s=6.0)
            if p.returncode != 0:
                # Fallback to non -t output
                raw = (p.stderr or p.stdout or "").lower()
                return "enabled" in raw and "disabled" not in raw
            raw = (p.stdout or "").strip().lower()
            return raw == "enabled"
        except Exception:
            return False

    def _host_wifi_set_radio(enabled: bool) -> None:
        onoff = "on" if enabled else "off"
        p = _nmcli_run(["radio", "wifi", onoff], timeout_s=12.0)
        if p.returncode != 0:
            out = (p.stderr or p.stdout or "").strip()
            raise RuntimeError(f"nmcli radio wifi {onoff} failed ({p.returncode}): {out}")

    def _host_wifi_wait_iface_ready(iface: str, timeout_s: float = 12.0) -> None:
        """Wait until NM reports iface state is not 'unavailable'."""
        iface = (iface or "wlan0").strip()
        deadline = time.time() + max(1.0, float(timeout_s))
        last_state = None
        while time.time() < deadline:
            p = _nmcli_run(["-t", "-f", "DEVICE,TYPE,STATE", "dev", "status"], timeout_s=6.0)
            if p.returncode == 0:
                for line in (p.stdout or "").splitlines():
                    parts = line.split(":")
                    if len(parts) >= 3 and parts[0] == iface and parts[1] == "wifi":
                        last_state = parts[2]
                        if last_state and last_state.lower() != "unavailable":
                            return
            time.sleep(0.4)
        raise RuntimeError(f"host WiFi iface '{iface}' not ready (state={last_state})")

    def _wifi_rescan(iface: str) -> None:
        iface = (iface or "wlan0").strip()
        p = _nmcli_run(["dev", "wifi", "rescan", "ifname", iface], timeout_s=10.0)
        # Some versions return non-zero if busy; don't hard-fail
        return

    def _wifi_list_ssids(iface: str) -> list:
        iface = (iface or "wlan0").strip()
        p = _nmcli_run(["-t", "-f", "SSID", "dev", "wifi", "list", "ifname", iface, "--rescan", "no"], timeout_s=12.0)
        if p.returncode != 0:
            return []
        ssids = []
        for line in (p.stdout or "").splitlines():
            s = (line or "").strip()
            if s:
                ssids.append(s)
        return ssids


    def _wifi_profile_up(conn_name: str, iface: str = "", bssid: str = "", timeout_s: float = 30.0) -> None:
        """Activate a pre-configured NetworkManager connection profile (preferred).

        Example (one-time, as root):
            nmcli con add type wifi ifname wlan0 con-name gatecontrol-wled-ap ssid "WLED-AP" \
              wifi-sec.key-mgmt wpa-psk wifi-sec.psk "wled1234" connection.autoconnect no connection.permissions ""

        Then activate as unprivileged user:
            nmcli con up id gatecontrol-wled-ap ifname wlan0
        """
        conn_name = str(conn_name or "").strip()
        iface = str(iface or "wlan0").strip()
        bssid = str(bssid or "").strip()
        if not conn_name:
            raise RuntimeError("WiFi connection profile name missing")
        args = ["--wait", str(int(max(10.0, min(90.0, float(timeout_s))))), "con", "up", "id", conn_name, "ifname", iface]
        # Debian nmcli supports selecting AP via 'ap BSSID' for Wi-Fi profiles.
        if bssid:
            args += ["ap", bssid]
        p = _nmcli_run(args, timeout_s=max(12.0, min(95.0, float(timeout_s) + 10.0)))
        if p.returncode != 0:
            out = (p.stderr or p.stdout or "").strip()
            raise RuntimeError(f"nmcli con up failed ({p.returncode}): {out}")

    def _wifi_profile_down(conn_name: str, timeout_s: float = 20.0) -> None:
        conn_name = str(conn_name or "").strip()
        if not conn_name:
            return
        p = _nmcli_run(["--wait", str(int(max(5.0, min(40.0, float(timeout_s))))), "con", "down", "id", conn_name], timeout_s=max(10.0, min(45.0, float(timeout_s) + 10.0)))
        # Don't hard-fail on down; best-effort
        return

    def _wifi_connect_host_profile(conn_name: str, ssid: str, iface: str = "", bssid: str = "", timeout_s: float = 35.0) -> None:
        """Connect host WiFi using a pre-created NetworkManager profile.

        We still do a scan/retry loop so we can wait until the node AP is actually visible.
        """
        ssid = str(ssid or "").strip()
        iface = str(iface or "wlan0").strip()
        bssid = str(bssid or "").strip()
        conn_name = str(conn_name or "").strip()
        if not conn_name:
            raise RuntimeError("WiFi connection profile name missing")
        if not ssid:
            # If caller doesn't provide SSID, we try anyway (profile knows it)
            ssid = ""

        _host_wifi_wait_iface_ready(iface, timeout_s=12.0)

        deadline = time.time() + max(5.0, float(timeout_s))
        last_err = None

        while time.time() < deadline:
            try:
                _wifi_rescan(iface)
            except Exception:
                pass

            if ssid:
                ssids = _wifi_list_ssids(iface)
                if ssid not in ssids:
                    time.sleep(0.7)
                    continue

            try:
                _wifi_profile_up(conn_name, iface=iface, bssid=bssid, timeout_s=min(60.0, max(15.0, float(timeout_s))))
                return
            except Exception as ex:
                last_err = str(ex)
                # If the AP drops between scan and activation, keep trying until timeout.
                if "No network with SSID" in last_err or "no suitable device" in last_err.lower():
                    time.sleep(0.8)
                    continue
                # If WiFi is disabled and we don't have permission to enable it, fail fast.
                if "Wi-Fi is disabled" in last_err or "wireless is disabled" in last_err.lower():
                    raise RuntimeError(last_err)
                # Otherwise retry a bit
                time.sleep(0.9)

        if last_err:
            raise RuntimeError(f"nmcli profile connect timeout: {last_err}")
        raise RuntimeError("nmcli profile connect timeout")
    def _wifi_connect_host(ssid: str, password: str, iface: str = "", bssid: str = "", timeout_s: float = 25.0) -> None:
        """Connect the RotorHazard host to the node's AP (Linux/NetworkManager via nmcli).

        Includes scan/retry loop because the node AP may take a moment to start.
        """
        ssid = str(ssid or "").strip()
        password = str(password or "").strip()
        iface = str(iface or "wlan0").strip()
        bssid = str(bssid or "").strip()

        if not ssid:
            raise RuntimeError("WiFi SSID missing")

        # Ensure interface is usable (WiFi radio must be enabled outside, but be defensive)
        _host_wifi_wait_iface_ready(iface, timeout_s=12.0)

        deadline = time.time() + max(3.0, float(timeout_s))
        last_err = None

        while time.time() < deadline:
            try:
                _wifi_rescan(iface)
            except Exception:
                pass

            ssids = _wifi_list_ssids(iface)
            if ssid in ssids:
                args = ["-w", str(int(max(5.0, min(25.0, float(timeout_s))))), "dev", "wifi", "connect", ssid]
                if password:
                    args += ["password", password]
                if bssid:
                    args += ["bssid", bssid]
                if iface:
                    args += ["ifname", iface]

                p = _nmcli_run(args, timeout_s=max(10.0, min(35.0, float(timeout_s) + 5.0)))
                if p.returncode == 0:
                    return
                out = (p.stderr or p.stdout or "").strip()
                last_err = out or f"nmcli rc={p.returncode}"
                # If the AP dropped between scan and connect, keep trying
                if "No network with SSID" in last_err:
                    time.sleep(0.6)
                    continue
                # Auth errors etc -> fail fast
                raise RuntimeError(f"nmcli connect failed ({p.returncode}): {last_err}")

            # SSID not visible yet
            time.sleep(0.7)

        if last_err:
            raise RuntimeError(f"nmcli connect timeout: {last_err}")
        raise RuntimeError(f"nmcli connect timeout: SSID '{ssid}' not found")

    def _http_get_json(url: str, timeout_s: float = 4.0) -> dict:
        import urllib.request
        import urllib.error

        req = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            raw = r.read()
        return json.loads(raw.decode("utf-8", errors="replace") or "{}")

    def _http_post_json(url: str, payload: dict, timeout_s: float = 6.0) -> dict:
        import urllib.request
        import urllib.error

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            raw = r.read()
        # Some WLED endpoints reply with full JSON if v:true was sent; otherwise may be empty
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8", errors="replace") or "{}")
        except Exception:
            return {}

    def _http_post_multipart_file(url: str, field_name: str, file_path: str, timeout_s: float = 90.0) -> int:
        import urllib.request
        import urllib.error

        boundary = "----gc" + uuid.uuid4().hex
        filename = os.path.basename(file_path)
        with open(file_path, "rb") as f:
            content = f.read()

        lines = []
        lines.append(f"--{boundary}\r\n".encode("utf-8"))
        lines.append(
            f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'.encode("utf-8")
        )
        lines.append(b"Content-Type: application/octet-stream\r\n\r\n")
        lines.append(content)
        lines.append(b"\r\n")
        lines.append(f"--{boundary}--\r\n".encode("utf-8"))
        body = b"".join(lines)

        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "Accept": "*/*"},
        )
        try:

            with urllib.request.urlopen(req, timeout=timeout_s) as r:

                return int(getattr(r, "status", 200) or 200)

        except urllib.error.HTTPError as e:

            # return the HTTP status code so callers can provide user-friendly errors

            return int(getattr(e, "code", 500) or 500)

    def _wled_base_url(raw: str) -> str:
        s = (raw or "").strip()
        if not s:
            s = "http://4.3.2.1"
        if not (s.startswith("http://") or s.startswith("https://")):
            s = "http://" + s
        return s.rstrip("/")

    def _wled_info(base_url: str, timeout_s: float = 4.0) -> Optional[dict]:
        try:
            return _http_get_json(f"{base_url}/json/info", timeout_s=timeout_s)
        except Exception:
            return None

    def _wait_for_expected_node(base_url: str, expected_mac: str, timeout_s: float, poll_s: float = 1.0) -> Optional[dict]:
        """Poll /json/info until the reported MAC matches expected_mac (full 6 bytes)."""
        deadline = time.time() + float(timeout_s)
        expected = _expected_mac_hex(expected_mac)
        while time.time() < deadline:
            info = _wled_info(base_url, timeout_s=4.0)
            if info and isinstance(info, dict):
                mac = _expected_mac_hex(info.get("mac") or "")
                if mac and expected and mac == expected:
                    return info
            time.sleep(poll_s)
        return None

    def _wled_upload_firmware(base_url: str, fw_path: str, timeout_s: float = 120.0) -> None:
        # WLED accepts OTA firmware via POST /update with multipart field name "update"
        # (equivalent to: curl -F update=@firmware.bin http://<ip>/update)
        status = _http_post_multipart_file(f"{base_url}/update", "update", fw_path, timeout_s=timeout_s)
        if status >= 400:
            if status == 401:
                raise RuntimeError("HTTP 401 from /update (Unauthorized). Likely WLED OTA lock is enabled. Disable OTA lock in WLED: Settings → Security & Updates → OTA locked (enter passphrase, Save, Reboot).")
            raise RuntimeError(f"HTTP {status} from /update")

    def _wled_upload_file(base_url: str, file_path: str, timeout_s: float = 45.0) -> None:
        # WLED exposes filesystem upload endpoint: POST /upload with multipart field name "data"
        status = _http_post_multipart_file(f"{base_url}/upload", "data", file_path, timeout_s=timeout_s)
        if status >= 400:
            if status == 401:
                raise RuntimeError("HTTP 401 from /upload (Unauthorized). Likely WLED OTA lock is enabled. Disable OTA lock in WLED: Settings → Security & Updates → OTA locked (enter passphrase, Save, Reboot).")
            raise RuntimeError(f"HTTP {status} from /upload")

    def _wled_reboot(base_url: str, timeout_s: float = 6.0) -> None:
        # JSON API supports immediate reboot via {"rb":true} on /json/state 
        _http_post_json(f"{base_url}/json/state", {"rb": True}, timeout_s=timeout_s)

    @bp.route("/gatecontrol/api/fw/upload", methods=["POST"])
    def api_fw_upload():
        if _task_is_running():
            return _task_busy_response()

        kind = (request.form.get("kind") or "").strip().lower()
        f = request.files.get("file", None)
        try:
            info = _store_upload(f, kind)
            # return only safe fields
            return jsonify({"ok": True, "file": {k: info[k] for k in ("id", "kind", "name", "size", "sha256", "uploaded_ts")}})
        except Exception as ex:
            return jsonify({"ok": False, "error": str(ex)}), 400

    @bp.route("/gatecontrol/api/presets/upload", methods=["POST"])
    def api_presets_upload():
        if _task_is_running():
            return _task_busy_response()

        f = request.files.get("file", None)
        try:
            info = _store_upload(f, "presets")
            with open(info["path"], "rb") as fh:
                payload = fh.read()
            parsed = _parse_wled_presets_minimal(payload or b"{}")
            gc_instance.uiEffectList = [UIFieldSelectOption(str(pid), name) for pid, name in parsed]
            try:
                gc_instance.register_quickset_ui()
                gc_instance.registerActions()
                gc_instance._rhapi.ui.broadcast_ui("run")
            except Exception:
                pass
            return jsonify({
                "ok": True,
                "file": {k: info[k] for k in ("id", "kind", "name", "size", "sha256", "uploaded_ts")},
                "presets": [{"id": pid, "name": name} for pid, name in parsed],
            })
        except Exception as ex:
            return jsonify({"ok": False, "error": str(ex)}), 400

    @bp.route("/gatecontrol/api/fw/uploads", methods=["GET"])
    def api_fw_uploads():
        with _upload_lock:
            rows = [
                {k: v.get(k) for k in ("id", "kind", "name", "size", "sha256", "uploaded_ts")}
                for v in _uploads.values()
                if v and os.path.exists(v.get("path", ""))
            ]
        rows.sort(key=lambda r: r.get("uploaded_ts", 0), reverse=True)
        return jsonify({"ok": True, "files": rows})

    @bp.route("/gatecontrol/api/wifi/interfaces", methods=["GET"])
    def api_wifi_interfaces():
        ifaces = _wifi_interfaces()
        return jsonify({"ok": True, "ifaces": ifaces})

    @bp.route("/gatecontrol/api/fw/start", methods=["POST"])
    def api_fw_start():
        _ensure_transport_hooked()
        if _task_is_running():
            return _task_busy_response()

        body = request.get_json(silent=True) or {}
        macs = body.get("macs") or []
        if not isinstance(macs, list) or not macs:
            return jsonify({"ok": False, "error": "missing macs"}), 400

        do_firmware = bool(body.get("doFirmware", True))
        do_presets = bool(body.get("doPresets", False))
        do_cfg = bool(body.get("doCfg", False))

        if not (do_firmware or do_presets or do_cfg):
            return jsonify({"ok": False, "error": "no operations selected"}), 400

        fw_info = None
        if do_firmware:
            fw_id = str(body.get("fwId") or "").strip()
            fw_info = _get_upload(fw_id, expect_kind="firmware")
            if not fw_info:
                return jsonify({"ok": False, "error": "firmware file not uploaded (fwId)"}), 400

        presets_info = None
        cfg_info = None
        if do_presets:
            presets_id = str(body.get("presetsId") or "").strip()
            presets_info = _get_upload(presets_id, expect_kind="presets") if presets_id else None
            if not presets_info:
                return jsonify({"ok": False, "error": "presets file not uploaded (presetsId)"}), 400
        if do_cfg:
            cfg_id = str(body.get("cfgId") or "").strip()
            cfg_info = _get_upload(cfg_id, expect_kind="cfg") if cfg_id else None
            if not cfg_info:
                return jsonify({"ok": False, "error": "cfg file not uploaded (cfgId)"}), 400

        try:
            retries = int(body.get("retries") or 3)
        except Exception:
            retries = 3
        retries = max(1, min(retries, 10))

        base_url = _wled_base_url(body.get("baseUrl") or "")
        stop_on_error = bool(body.get("stopOnError") or False)

        # Host WiFi connection settings (connecting the RotorHazard host to the node's AP)
        wifi = body.get("wifi") or {}
        wifi_ssid = str(wifi.get("ssid") or body.get("wifiSsid") or "WLED-AP")
        wifi_iface = str(wifi.get("iface") or body.get("wifiIface") or "wlan0")
        wifi_conn_name = str(wifi.get("connName") or body.get("wifiConnName") or "gatecontrol-wled-ap")
        # Optional: connect to a specific BSSID/AP (rarely needed; only makes sense if multiple APs share same SSID)
        wifi_bssid = str(wifi.get("bssid") or body.get("wifiBssid") or "")
        wifi_timeout_s = float(wifi.get("timeoutS") or body.get("wifiTimeoutS") or 35.0)

        host_wifi_enable = bool(wifi.get("hostWifiEnable") if "hostWifiEnable" in wifi else body.get("hostWifiEnable", True))
        host_wifi_restore = bool(wifi.get("hostWifiRestore") if "hostWifiRestore" in wifi else body.get("hostWifiRestore", True))



        def do_fwupdate():
            results = {
                "ok": True,
                "baseUrl": base_url,
                "fw": ({k: fw_info[k] for k in ("id", "name", "size", "sha256")} if fw_info else None),
                "presets": ({k: presets_info[k] for k in ("id", "name", "size", "sha256")} if presets_info else None),
                "cfg": ({k: cfg_info[k] for k in ("id", "name", "size", "sha256")} if cfg_info else None),
                "devices": [],
                "errors": [],
            }

            host_wifi_initial = _host_wifi_radio_enabled()
            host_wifi_changed = False  # whether we toggled WiFi radio on during this run
            results["hostWifi"] = {"wasEnabled": host_wifi_initial, "enabled": host_wifi_initial, "restored": False}
            try:

                # Ensure host WiFi radio is ON for the duration of this task (if requested).
                # With a pre-created NM profile (solution B), the service doesn't need to create/modify connections,
                # but it still needs WiFi radio enabled to bring the profile up.
                if host_wifi_enable and not host_wifi_initial:
                    _task_update(meta={
                        "stage": "HOST_WIFI_ON",
                        "index": 0,
                        "total": len(macs),
                        "addr": "HOST",
                        "attempt": 0,
                        "retries": retries,
                        "message": "Enabling host WiFi radio…",
                    })
                    _host_wifi_set_radio(True)
                    host_wifi_changed = True
                    # Wait until wlan iface becomes available to scan/connect
                    _host_wifi_wait_iface_ready(wifi_iface, timeout_s=15.0)
                    results["hostWifi"]["enabled"] = True

                total = len(macs)
                for idx, addr in enumerate(macs, start=1):
                    expected_mac = _expected_mac_hex(str(addr))
                    dev_res = {
                        "addr": addr,
                        "expectedMac": expected_mac,
                        "groupId": _lookup_group_id_for_addr(str(addr)),
                        "ok": False,
                        "error": None,
                        "info_before": None,
                        "info_after": None,
                    }
                    results["devices"].append(dev_res)

                    # 1) Enable WiFi AP on the node via LoRa config option 0x04 (existing command in plugin UI)
                    _task_update(meta={
                        "stage": "LORA_AP_ON",
                        "index": idx,
                        "total": total,
                        "addr": addr,
                        "attempt": 0,
                        "retries": retries,
                        "message": "Enable WLED AP via LoRa",
                    })
                    try:
                        recv3 = _recv3_bytes_from_addr(str(addr))
                        gc_instance.sendConfig(0x04, data0=1, recv3=recv3)
                    except Exception as ex:
                        dev_res["error"] = f"LoRa AP enable failed: {ex}"
                        results["errors"].append(dev_res["error"])
                        if stop_on_error:
                            raise RuntimeError(dev_res["error"])
                        continue

                    # 2) Connect the RotorHazard host to the node's AP (required to reach the default AP IP, e.g. 4.3.2.1)
                    _task_update(meta={
                        "stage": "CONNECT_WIFI",
                        "index": idx,
                        "total": total,
                        "addr": addr,
                        "attempt": 0,
                        "retries": retries,
                        "message": f'Connecting host WiFi via profile "{wifi_conn_name}" (iface {wifi_iface}) to SSID "{wifi_ssid}"',
                    })
                    
                    try:
                        try:
                            _wifi_connect_host_profile(
                                wifi_conn_name,
                                wifi_ssid,
                                iface=wifi_iface,
                                bssid=wifi_bssid,
                                timeout_s=wifi_timeout_s
                            )
                        except Exception as ex1:
                            msg1 = str(ex1)
                            # If host WiFi radio is disabled, try enabling it (once) when allowed.
                            if host_wifi_enable and (not host_wifi_changed) and ("Wi-Fi is disabled" in msg1 or "wireless is disabled" in msg1.lower()):
                                _task_update(meta={
                                    "stage": "HOST_WIFI_ON",
                                    "index": idx,
                                    "total": total,
                                    "addr": "HOST",
                                    "attempt": 0,
                                    "retries": retries,
                                    "message": f"Host WiFi appears disabled; enabling on {wifi_iface}…",
                                })
                                _host_wifi_set_radio(True)
                                host_wifi_changed = True
                                results["hostWifi"]["enabled"] = True
                                _host_wifi_wait_iface_ready(wifi_iface, timeout_s=15.0)
                                # retry connect
                                _wifi_connect_host_profile(
                                    wifi_conn_name,
                                    wifi_ssid,
                                    iface=wifi_iface,
                                    bssid=wifi_bssid,
                                    timeout_s=wifi_timeout_s
                                )
                            else:
                                raise
                    except Exception as ex:

                        dev_res["error"] = f"Host WiFi connect failed: {ex}"
                        results["errors"].append(dev_res["error"])
                        if stop_on_error:
                            raise RuntimeError(dev_res["error"])
                        continue

                    # 3) Wait until HTTP reachable AND the node behind base_url matches expected full MAC via /json/info
                    _task_update(meta={
                        "stage": "WAIT_HTTP",
                        "index": idx,
                        "total": total,
                        "addr": addr,
                        "attempt": 0,
                        "retries": retries,
                        "message": f"Waiting for WLED /json/info mac to match {expected_mac}",
                    })
                    info = _wait_for_expected_node(base_url, expected_mac, timeout_s=90.0, poll_s=1.0)
                    if not info:
                        dev_res["error"] = f"Timeout waiting for node (baseUrl={base_url}) to report expected mac {expected_mac}"
                        results["errors"].append(dev_res["error"])
                        if stop_on_error:
                            raise RuntimeError(dev_res["error"])
                        continue
                    dev_res["info_before"] = {k: info.get(k) for k in ("mac", "ver", "arch", "name")}

                    # 4) Optional: upload presets/cfg files to filesystem.
                    try:
                        if presets_info:
                            _task_update(meta={
                                "stage": "UPLOAD_PRESETS",
                                "index": idx,
                                "total": total,
                                "addr": addr,
                                "attempt": 0,
                                "retries": retries,
                                "message": "Uploading presets.json",
                            })
                            _wled_upload_file(base_url, presets_info["path"], timeout_s=45.0)
                        if cfg_info:
                            _task_update(meta={
                                "stage": "UPLOAD_CFG",
                                "index": idx,
                                "total": total,
                                "addr": addr,
                                "attempt": 0,
                                "retries": retries,
                                "message": "Uploading cfg.json",
                            })
                            _wled_upload_file(base_url, cfg_info["path"], timeout_s=45.0)
                    except Exception as ex:
                        dev_res["error"] = f"Config upload failed: {ex}"
                        results["errors"].append(dev_res["error"])
                        if stop_on_error:
                            raise RuntimeError(dev_res["error"])
                        # keep going to firmware update even if optional config failed

                    if not fw_info:
                        dev_res["ok"] = True
                        _task_update(meta={
                            "stage": "LORA_AP_OFF",
                            "index": idx,
                            "total": total,
                            "addr": addr,
                            "attempt": 0,
                            "retries": retries,
                            "message": "Disable WLED AP via LoRa after upload",
                        })
                        try:
                            recv3 = _recv3_bytes_from_addr(str(addr))
                            gc_instance.sendConfig(0x04, data0=0, recv3=recv3)
                        except Exception:
                            pass
                        continue

                    # 5) Firmware upload with retries (WLED reboots automatically after successful OTA)
                    ok = False
                    last_err = None
                    for attempt in range(1, retries + 1):
                        _task_update(meta={
                            "stage": "UPLOAD_FW",
                            "index": idx,
                            "total": total,
                            "addr": addr,
                            "attempt": attempt,
                            "retries": retries,
                            "message": f"Uploading firmware (try {attempt}/{retries})",
                        })
                        try:
                            _wled_upload_firmware(base_url, fw_info["path"], timeout_s=180.0)
                            ok = True
                            break
                        except Exception as ex:
                            last_err = ex
                            time.sleep(2.0)

                    if not ok:
                        dev_res["error"] = f"Firmware upload failed: {last_err}"
                        results["errors"].append(dev_res["error"])
                        if stop_on_error:
                            raise RuntimeError(dev_res["error"])
                        continue

                    # 6) After OTA: device reboots; in your firmware AP does NOT auto-open.
                    #    So we must re-enable AP via LoRa and reconnect the host WiFi before post-verification.
                    _task_update(meta={
                        "stage": "POST_OTA_WAIT",
                        "index": idx,
                        "total": total,
                        "addr": addr,
                        "attempt": 0,
                        "retries": retries,
                        "message": "Waiting a moment for reboot…",
                    })
                    time.sleep(6.0)

                    # After OTA reboot, the node may not accept LoRa commands until its group is re-applied.
                    # Re-send Set Group (config option 0x01) before trying to toggle AP again.
                    _task_update(meta={
                        "stage": "LORA_SET_GROUP",
                        "index": idx,
                        "total": total,
                        "addr": addr,
                        "attempt": 0,
                        "retries": retries,
                        "message": f"Re-applying Group ID {dev_res.get('groupId', 0)} via LoRa",
                    })
                    try:
                        # IMPORTANT: Setting the group must use setGateGroupId(), not sendConfig(),
                        # because the node will only accept further LoRa commands after SET_GROUP
                        # is ACKed (your firmware behavior).
                        target_dev = None
                        want = _expected_mac_hex(str(addr))
                        with _gc_lock:
                            for d in gc_devicelist:
                                have = _expected_mac_hex(str(getattr(d, "addr", "") or ""))
                                if have and want and have.lower() == want.lower():
                                    target_dev = d
                                    break
                        if not target_dev:
                            raise RuntimeError(f"Device {addr} not found in gc_devicelist")

                        ok_set = gc_instance.setGateGroupId(target_dev, forceSet=True, wait_for_ack=True)
                        if not ok_set:
                            raise RuntimeError("No ACK_OK for SET_GROUP")
                    except Exception as ex:
                        dev_res["error"] = f"Set group via LoRa failed: {ex}"
                        results["errors"].append(dev_res["error"])
                        if stop_on_error:
                            raise RuntimeError(dev_res["error"])
                        continue

                    _task_update(meta={
                        "stage": "POST_OTA_LORA_AP_ON",
                        "index": idx,
                        "total": total,
                        "addr": addr,
                        "attempt": 0,
                        "retries": retries,
                        "message": "Re-enable WLED AP via LoRa after reboot",
                    })
                    try:
                        recv3 = _recv3_bytes_from_addr(str(addr))
                        gc_instance.sendConfig(0x04, data0=1, recv3=recv3)
                    except Exception:
                        # best-effort; even if this fails, the HTTP wait below will time out
                        pass

                    
                    _task_update(meta={
                        "stage": "POST_OTA_CONNECT_WIFI",
                        "index": idx,
                        "total": total,
                        "addr": addr,
                        "attempt": 0,
                        "retries": retries,
                        "message": f'Reconnecting host WiFi via profile "{wifi_conn_name}"…',
                    })
                    try:
                        _wifi_connect_host_profile(
                            wifi_conn_name,
                            wifi_ssid,
                            iface=wifi_iface,
                            bssid=wifi_bssid,
                            timeout_s=wifi_timeout_s
                        )
                    except Exception as ex1:
                        msg1 = str(ex1)
                        if host_wifi_enable and ("Wi-Fi is disabled" in msg1 or "wireless is disabled" in msg1.lower()):
                            _task_update(meta={
                                "stage": "HOST_WIFI_ON",
                                "index": idx,
                                "total": total,
                                "addr": "HOST",
                                "attempt": 0,
                                "retries": retries,
                                "message": f"Host WiFi appears disabled; enabling on {wifi_iface}…",
                            })
                            _host_wifi_set_radio(True)
                            host_wifi_changed = True
                            results["hostWifi"]["enabled"] = True
                            _host_wifi_wait_iface_ready(wifi_iface, timeout_s=15.0)
                            _wifi_connect_host_profile(
                                wifi_conn_name,
                                wifi_ssid,
                                iface=wifi_iface,
                                bssid=wifi_bssid,
                                timeout_s=wifi_timeout_s
                            )
                        else:
                            raise

                    _task_update(meta={
                        "stage": "POST_OTA_VERIFY",
                        "index": idx,
                        "total": total,
                        "addr": addr,
                        "attempt": 0,
                        "retries": retries,
                        "message": "Waiting for WLED /json/info after reboot…",
                    })
                    info2 = _wait_for_expected_node(base_url, expected_mac, timeout_s=150.0, poll_s=2.0)
                    if not info2:
                        dev_res["error"] = "Timeout waiting for node after OTA reboot (AP/HTTP not reachable)"
                        results["errors"].append(dev_res["error"])
                        if stop_on_error:
                            raise RuntimeError(dev_res["error"])
                        continue
                    dev_res["info_after"] = {k: info2.get(k) for k in ("mac", "ver", "arch", "name")}

                    # 7) Try to disable AP again via LoRa (best-effort)
                    try:
                        _task_update(meta={
                            "stage": "LORA_AP_OFF",
                            "index": idx,
                            "total": total,
                            "addr": addr,
                            "attempt": 0,
                            "retries": retries,
                            "message": "Disable WLED AP via LoRa (best-effort)",
                        })
                        recv3 = _recv3_bytes_from_addr(str(addr))
                        gc_instance.sendConfig(0x04, data0=0, recv3=recv3)
                    except Exception:
                        pass

                    dev_res["ok"] = (dev_res["error"] is None)
            except Exception as ex:
                results["ok"] = False
                results["errors"].append(f"fwupdate exception: {ex}")
            finally:
                if host_wifi_restore and (host_wifi_initial is False) and _host_wifi_radio_enabled():
                    try:
                        _task_update(meta={
                            "stage": "HOST_WIFI_OFF",
                            "index": len(macs),
                            "total": len(macs),
                            "addr": "HOST",
                            "attempt": 0,
                            "retries": retries,
                            "message": "Restoring host WiFi radio (off)…",
                        })
                        try:
                            _wifi_profile_down(wifi_conn_name, timeout_s=10.0)
                        except Exception:
                            pass
                        _host_wifi_set_radio(False)
                        results["hostWifi"]["enabled"] = False
                        results["hostWifi"]["restored"] = True
                    except Exception as ex2:
                        results["errors"].append(f"Host WiFi restore failed: {ex2}")

            return results

        t = _start_task("fwupdate", do_fwupdate, meta={
            "stage": "INIT",
            "index": 0,
            "total": len(macs),
            "retries": retries,
            "addr": None,
            "message": "Firmware update started",
            "baseUrl": base_url,
        })
        if not t:
            return _task_busy_response()
        return jsonify({"ok": True, "task": t})


# Finally register blueprint
    rhapi.ui.blueprint_add(bp)
    _log("GateControl UI blueprint registered at /gatecontrol")
