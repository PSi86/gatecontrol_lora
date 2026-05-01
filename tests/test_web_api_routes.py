import ast
import contextlib
import importlib
import os
import pathlib
import sys
import tempfile
import types
import unittest

from racelink.domain import RL_Device, RL_DeviceGroup, RL_Dev_Type


ROOT = pathlib.Path(__file__).resolve().parents[1]


def _ensure_flask_stub():
    if "flask" in sys.modules:
        return

    flask = types.ModuleType("flask")

    class Blueprint:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def route(self, *args, **kwargs):
            def _decorator(fn):
                return fn

            return _decorator

    flask.Blueprint = Blueprint
    flask.request = types.SimpleNamespace(get_json=lambda silent=True: {}, files={}, form={})
    flask.jsonify = lambda payload: payload
    flask.Response = type("Response", (), {})
    flask.stream_with_context = lambda fn: fn
    flask.templating = types.SimpleNamespace(render_template=lambda *args, **kwargs: {})
    sys.modules["flask"] = flask


def _import_api_module():
    _ensure_flask_stub()
    sys.modules.pop("racelink.web.api", None)
    return importlib.import_module("racelink.web.api")


class _FakeBlueprint:
    def __init__(self):
        self.routes = {}

    def route(self, rule, methods=None):
        def _decorator(fn):
            self.routes[(rule, tuple(methods or ("GET",)))] = fn
            return fn

        return _decorator


class _FakeContext:
    def __init__(self):
        self.rl_instance = type("RL", (), {"uiPresetList": [{"value": "01", "label": "Red"}]})()
        self.services = {
            "host_wifi": type("HostWifi", (), {"wifi_interfaces": staticmethod(lambda: ["wlan0"])})(),
            "ota": type("OTA", (), {})(),
            "presets": type(
                "Presets",
                (),
                {
                    "ensure_loaded": staticmethod(lambda: True),
                    "list_files": staticmethod(lambda: []),
                    "get_current_name": staticmethod(lambda: ""),
                    "preset_path_for_name": staticmethod(lambda name: None),
                },
            )(),
        }
        self.rl_lock = contextlib.nullcontext()
        self.RL_DeviceGroup = RL_DeviceGroup
        self.logger = None
        self.sse = type("SSE", (), {"master": type("Master", (), {"snapshot": staticmethod(lambda: {})})()})()
        self.tasks = type("Tasks", (), {"snapshot": staticmethod(lambda: {}), "is_running": staticmethod(lambda: False)})()
        self._devices = [
            RL_Device(
                "AABBCCDDEEFF",
                RL_Dev_Type.NODE_WLED_STARTBLOCK_REV3,
                "SB",
                groupId=1,
                caps=RL_Dev_Type.NODE_WLED_STARTBLOCK_REV3,
            ),
            RL_Device(
                "112233445566",
                RL_Dev_Type.NODE_WLED_REV5,
                "WLED",
                groupId=2,
                caps=RL_Dev_Type.NODE_WLED_REV5,
            ),
        ]
        self._groups = [
            RL_DeviceGroup("Group 1"),
            RL_DeviceGroup("Group 2"),
            RL_DeviceGroup("All WLED Nodes", static_group=1, dev_type=0),
        ]

    def devices(self):
        return self._devices

    def groups(self):
        return self._groups


class WebApiRouteTests(unittest.TestCase):
    def setUp(self):
        self.api_module = _import_api_module()
        self.api_module.jsonify = lambda payload: payload
        self.bp = _FakeBlueprint()
        self.ctx = _FakeContext()
        self.api_module.register_api_routes(self.bp, self.ctx)

    def _route(self, path):
        return self.bp.routes[(path, ("GET",))]

    def test_specials_route_returns_specials_payload(self):
        payload = self._route("/api/specials")()

        self.assertTrue(payload["ok"])
        self.assertIn("specials", payload)
        self.assertIn("WLED", payload["specials"])
        self.assertIn("STARTBLOCK", payload["specials"])

    def test_neighboring_get_routes_execute_without_missing_symbols(self):
        devices = self._route("/api/devices")()
        groups = self._route("/api/groups")()
        options = self._route("/api/options")()

        self.assertTrue(devices["ok"])
        self.assertEqual(len(devices["devices"]), 2)
        self.assertTrue(groups["ok"])
        self.assertGreaterEqual(len(groups["groups"]), 1)
        self.assertTrue(options["ok"])
        self.assertEqual(options["presets"], [{"value": "01", "label": "Red"}])

    def test_groups_route_carries_caps_in_group_for_C5_filtering(self):
        """C5 contract: every row in /api/groups must include
        ``caps_in_group`` so the scene editor can filter target
        dropdowns to capable devices.

        The fixture's first device (a startblock node with caps
        [STARTBLOCK, WLED]) sits in groupId=1; the second device
        (plain WLED) sits in groupId=2 which the route filters out
        because the matching group at list-index 2 is the static
        ``All WLED Nodes`` group. That detail is incidental — what
        we assert is the cap-counts for the row that *does* hold a
        device, plus that every row carries the field."""
        groups = self._route("/api/groups")()
        self.assertTrue(groups["ok"])
        rows = {row["id"]: row for row in groups["groups"]}

        # Group at id=1 holds the startblock device (caps WLED+STARTBLOCK).
        self.assertIn(1, rows)
        self.assertEqual(rows[1].get("caps_in_group"), {"WLED": 1, "STARTBLOCK": 1})

        # Every row exposes the field — even Unconfigured (id=0) and
        # empty groups end up with an empty dict, never missing.
        for row in groups["groups"]:
            self.assertIn("caps_in_group", row)
            self.assertIsInstance(row["caps_in_group"], dict)


class _RouteValidationContext(_FakeContext):
    """Extends ``_FakeContext`` for routes that validate body fields and
    short-circuit before any ``rl_instance`` / repository call. The
    rename/delete routes only need a body and the lock context; the
    devices/control routes also need ``sse.master.set`` and
    ``sse.broadcast``."""

    def __init__(self):
        super().__init__()
        self.group_repo = None
        self.rl_grouplist = self._groups
        self.log = lambda msg: None
        self.rl_instance = type(
            "RL", (), {
                "uiPresetList": [],
                "save_to_db": lambda self, *a, **kw: None,
                "sendGroupPreset": lambda *a, **kw: True,
                "sendRaceLink": lambda *a, **kw: True,
                "getDeviceFromAddress": lambda self, mac: None,
            },
        )()
        # Replace the bare-bones ``sse`` from _FakeContext with a stub
        # that records broadcasts and accepts ``master.set(**kwargs)``.
        class _StubMaster:
            def __init__(self):
                self.snapshots = []
            def snapshot(self):
                return {}
            def set(self, **kwargs):
                self.snapshots.append(dict(kwargs))
        class _StubSse:
            def __init__(self):
                self.master = _StubMaster()
                self.broadcasts = []
            def broadcast(self, topic, payload):
                self.broadcasts.append((topic, dict(payload)))
        self.sse = _StubSse()


class GroupsRenameDeleteValidationTests(unittest.TestCase):
    """B1 + B4: missing/null/garbage ``id`` in the body must produce a
    clean 400 with a descriptive message, not a 500 from
    ``int(None)`` blowing up inside the route."""

    def setUp(self):
        self.api_module = _import_api_module()
        self.api_module.jsonify = lambda payload: payload
        self._flask_request = sys.modules["flask"].request
        self._flask_request.get_json = lambda silent=True: {}
        self.bp = _FakeBlueprint()
        self.ctx = _RouteValidationContext()
        self.api_module.register_api_routes(self.bp, self.ctx)

    def _set_body(self, body):
        snapshot = dict(body) if body is not None else None
        self._flask_request.get_json = lambda silent=True: snapshot

    def _post(self, path):
        handler = self.bp.routes[(path, ("POST",))]
        return handler()

    def _expect_bad_request(self, path, body, *, expect_in_error=""):
        self._set_body(body)
        result = self._post(path)
        # ``register_api_routes`` returns ``(payload, status)`` for non-200
        # responses thanks to our jsonify stub.
        self.assertIsInstance(result, tuple, f"expected (payload, status) tuple, got {result!r}")
        payload, status = result
        self.assertEqual(status, 400, f"expected 400, got {status} ({payload})")
        self.assertFalse(payload["ok"])
        if expect_in_error:
            self.assertIn(expect_in_error, payload["error"])

    def test_rename_with_empty_body_returns_400_not_500(self):
        self._expect_bad_request(
            "/api/groups/rename", {}, expect_in_error="missing field",
        )

    def test_rename_with_null_id_returns_400_not_500(self):
        self._expect_bad_request(
            "/api/groups/rename", {"id": None}, expect_in_error="must not be null",
        )

    def test_rename_with_non_numeric_id_returns_400_not_500(self):
        self._expect_bad_request(
            "/api/groups/rename", {"id": "abc"}, expect_in_error="must be an integer",
        )

    def test_delete_with_empty_body_returns_400_not_500(self):
        self._expect_bad_request(
            "/api/groups/delete", {}, expect_in_error="missing field",
        )

    def test_delete_with_null_id_returns_400_not_500(self):
        self._expect_bad_request(
            "/api/groups/delete", {"id": None}, expect_in_error="must not be null",
        )

    def test_rename_with_valid_id_proceeds_past_validation(self):
        """Smoke check: a well-formed id reaches the in-memory mutation
        path. We don't assert on the rename effect itself — that's
        covered by other tests; we just verify the validation gate
        lets a good request through."""
        self._set_body({"id": 0, "name": "Group 1 renamed"})
        result = self._post("/api/groups/rename")
        # 200 OK comes back as a plain dict, not a (payload, status) tuple.
        self.assertNotIsInstance(result, tuple, f"unexpected error response: {result}")
        self.assertTrue(result["ok"])

    def test_delete_non_empty_group_moves_devices_to_unconfigured(self):
        """Feature 1 (2026-04-29): /api/groups/delete now accepts
        non-empty groups by moving the devices to groupId=0 instead
        of rejecting. Devices in higher-indexed groups are
        renumbered down by one to keep the index→group mapping
        valid after the array shift."""
        # Fixture has two devices: one in groupId=1, one in groupId=2.
        # Group at list-index 1 ("Group 2" in the fixture) is
        # non-static so it can be deleted.
        ctx = self.ctx
        # Position the devices: one in the to-be-deleted group, one
        # in a higher-indexed group so we can verify the renumber.
        ctx._devices[0].groupId = 1   # will move to 0
        ctx._devices[1].groupId = 2   # will renumber to 1
        self._set_body({"id": 1})
        result = self._post("/api/groups/delete")
        self.assertNotIsInstance(result, tuple, f"unexpected error: {result}")
        self.assertTrue(result["ok"])
        self.assertEqual(result["moved_devices"], 1)
        self.assertEqual(result["renumbered_devices"], 1)
        # Confirm the in-memory device groupIds reflect the changes.
        self.assertEqual(int(ctx._devices[0].groupId), 0)
        self.assertEqual(int(ctx._devices[1].groupId), 1)

    def test_delete_static_group_still_rejected(self):
        """The static-group guard (e.g. ``All WLED Nodes``) still
        rejects the deletion. The non-empty change above must not
        relax that protection."""
        ctx = self.ctx
        # The fixture's third group is static_group=1.
        # In ctx.groups() it sits at index 2.
        self._set_body({"id": 2})
        result = self._post("/api/groups/delete")
        self.assertIsInstance(result, tuple)
        payload, status = result
        self.assertEqual(status, 400)
        self.assertIn("static", payload["error"])


class DevicesControlRoutesTests(unittest.TestCase):
    """B2: ``api_devices_control`` previously called the renamed-away
    ``sendGroupControl`` (always ``AttributeError`` → 500). Verify it
    now calls ``sendGroupPreset`` and that ``changed`` reflects the
    underlying boolean rather than blindly counting attempts."""

    def _make_ctx(self, *, group_send_returns: bool = True,
                  device_send_returns: bool = True):
        ctx = _RouteValidationContext()
        ctx.tasks = type(
            "Tasks", (), {
                "is_running": staticmethod(lambda: False),
                "snapshot": staticmethod(lambda: {}),
            },
        )()
        # Capture the dispatched calls so the test can assert what got
        # sent (or didn't).
        self.group_calls: list[tuple] = []
        self.device_calls: list[tuple] = []

        def _send_group_preset(gid, flags, preset_id, brightness):
            self.group_calls.append((gid, flags, preset_id, brightness))
            return group_send_returns

        def _send_race_link(dev, flags, preset_id, brightness):
            self.device_calls.append((dev.addr, flags, preset_id, brightness))
            return device_send_returns

        def _get_dev(mac):
            return next((d for d in ctx._devices if d.addr == mac), None)

        ctx.rl_instance = type(
            "RL", (), {
                "uiPresetList": [],
                "save_to_db": lambda self, *a, **kw: None,
                "sendGroupPreset": staticmethod(_send_group_preset),
                "sendRaceLink": staticmethod(_send_race_link),
                "getDeviceFromAddress": staticmethod(_get_dev),
            },
        )()
        return ctx

    def setUp(self):
        self.api_module = _import_api_module()
        self.api_module.jsonify = lambda payload: payload
        self._flask_request = sys.modules["flask"].request
        self._flask_request.get_json = lambda silent=True: {}
        self.bp = _FakeBlueprint()

    def _register(self, ctx):
        self.api_module.register_api_routes(self.bp, ctx)

    def _post(self, path, body):
        snapshot = dict(body)
        self._flask_request.get_json = lambda silent=True: snapshot
        handler = self.bp.routes[(path, ("POST",))]
        return handler()

    def test_group_path_calls_sendGroupPreset_not_sendGroupControl(self):
        """B2 stale-rename fix: the route used to call ``sendGroupControl``
        which no longer exists, so every group-control POST returned 500.
        Now it calls ``sendGroupPreset`` and, on success, reports
        ``changed: 1``."""
        ctx = self._make_ctx(group_send_returns=True)
        self._register(ctx)
        result = self._post("/api/devices/control", {
            "groupId": 1, "flags": 0x01, "presetId": 5, "brightness": 200,
        })
        self.assertNotIsInstance(result, tuple, f"unexpected error response: {result}")
        self.assertTrue(result["ok"])
        self.assertEqual(result["changed"], 1)
        self.assertEqual(self.group_calls, [(1, 0x01, 5, 200)])

    def test_group_path_with_failing_send_reports_zero_changed(self):
        """B2 return-value propagation: if the underlying send returns
        False (transport not ready), ``changed`` must stay at 0 — not
        the silent-success ``1`` the route used to report."""
        ctx = self._make_ctx(group_send_returns=False)
        self._register(ctx)
        result = self._post("/api/devices/control", {
            "groupId": 1, "flags": 0, "presetId": 5, "brightness": 200,
        })
        self.assertNotIsInstance(result, tuple, f"unexpected error response: {result}")
        self.assertTrue(result["ok"])
        self.assertEqual(result["changed"], 0,
                         "transport-failure must surface as changed=0")

    def test_device_path_propagates_failure_into_changed(self):
        """Same B2 contract on the per-device path: a False return from
        ``sendRaceLink`` does not increment ``changed``."""
        ctx = self._make_ctx(device_send_returns=False)
        self._register(ctx)
        result = self._post("/api/devices/control", {
            "macs": ["AABBCCDDEEFF"], "flags": 0, "presetId": 5, "brightness": 200,
        })
        self.assertNotIsInstance(result, tuple, f"unexpected error response: {result}")
        self.assertEqual(result["changed"], 0)

    def test_group_path_with_garbage_groupId_returns_400(self):
        """Defensive 400 for a non-numeric groupId — used to crash with
        ``ValueError: invalid literal for int()`` and return 500 via
        the outer except."""
        ctx = self._make_ctx()
        self._register(ctx)
        result = self._post("/api/devices/control", {
            "groupId": "abc", "flags": 0, "presetId": 5, "brightness": 200,
        })
        self.assertIsInstance(result, tuple)
        payload, status = result
        self.assertEqual(status, 400)
        self.assertIn("groupId", payload["error"])


class _RecordingSse:
    def __init__(self):
        self.broadcasts = []

    def broadcast(self, topic, payload):
        self.broadcasts.append((topic, dict(payload)))


class _FakeSceneRunner:
    """Records ``.run(key, progress_cb=...)`` calls and returns a configurable
    result. R7: when ``progress_events`` is supplied, the fake invokes the
    provided ``progress_cb`` once per scripted event so route tests can
    verify the SSE bridge wired to the callback."""

    def __init__(self, *, ok=True, error=None, actions=None, missing_keys=(),
                 progress_events=None):
        self.calls = []
        self.scene_args = []  # parallel to ``calls`` — captures the ``scene`` kwarg
        self._ok = ok
        self._error = error
        self._actions = actions or []
        self._missing_keys = set(missing_keys)
        self._progress_events = list(progress_events or [])
        self.last_progress_cb = None

    def run(self, key, *, progress_cb=None, scene=None):
        from racelink.services.scene_runner_service import SceneRunResult
        self.calls.append(key)
        self.scene_args.append(scene)
        self.last_progress_cb = progress_cb
        # When a draft dict is supplied the route bypasses the
        # ``scene_not_found`` lookup — match that semantics here so the
        # fake mirrors the real runner's contract.
        if scene is None and key in self._missing_keys:
            return SceneRunResult(scene_key=key, ok=False, error="scene_not_found")
        if progress_cb is not None:
            for ev in self._progress_events:
                progress_cb(dict(ev, scene_key=ev.get("scene_key", key)))
        return SceneRunResult(
            scene_key=key,
            ok=self._ok,
            error=self._error,
            actions=list(self._actions),
        )


class _SceneFakeContext(_FakeContext):
    """Extends _FakeContext with real SceneService + recording sse + stub runner."""

    def __init__(self, *, runner=None, scenes_storage_path=None):
        super().__init__()
        from racelink.services.scenes_service import SceneService
        self.services["scenes"] = SceneService(storage_path=scenes_storage_path)
        self.services["scene_runner"] = runner or _FakeSceneRunner()
        # _FakeContext gave us a minimal ``sse`` for the master snapshot test;
        # scene routes also need a recording broadcast helper for SSE refresh.
        self.sse = _RecordingSse()
        # Add a no-op state-master snapshot so other code paths still work.
        self.sse.master = type("Master", (), {"snapshot": staticmethod(lambda: {})})()


class WebApiScenesRouteTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.scenes_path = os.path.join(self._tmp.name, "scenes.json")

        # Re-import api (pops then imports) to make sure `request` symbol is
        # bound to the freshly-built flask stub. Without this, an earlier test
        # in the same process can pin api's ``request`` to a different stub.
        self.api_module = _import_api_module()
        self.api_module.jsonify = lambda payload: payload
        # Mutate the request stub IN-PLACE so api.py's captured reference
        # tracks our get_json overrides; reassigning sys.modules['flask'].request
        # would leave api's cached binding pointing at the old stub.
        self._flask_request = sys.modules["flask"].request
        self._flask_request.get_json = lambda silent=True: {}

        self.bp = _FakeBlueprint()
        self.runner = _FakeSceneRunner()
        self.ctx = _SceneFakeContext(runner=self.runner, scenes_storage_path=self.scenes_path)
        self.api_module.register_api_routes(self.bp, self.ctx)

    def _set_body(self, body):
        # Capture per-call to avoid mutation between later .get_json invocations.
        snapshot = dict(body)
        self._flask_request.get_json = lambda silent=True: snapshot

    def _route(self, path, method):
        return self.bp.routes[(path, (method,))]

    # ---- editor schema ------------------------------------------------

    def test_editor_schema_lists_all_kinds(self):
        payload = self._route("/api/scenes/editor-schema", "GET")()
        self.assertTrue(payload["ok"])
        kinds = {entry["kind"] for entry in payload["kinds"]}
        self.assertEqual(
            kinds,
            {"rl_preset", "wled_preset", "wled_control", "startblock",
             "sync", "delay", "offset_group"},
        )
        self.assertIn("flag_keys", payload)
        # All four user-intent flags must be exposed
        self.assertEqual(
            set(payload["flag_keys"]),
            {"arm_on_sync", "force_tt0", "force_reapply", "offset_mode"},
        )

    # ---- list / get ---------------------------------------------------

    def test_list_empty_initially(self):
        payload = self._route("/api/scenes", "GET")()
        self.assertEqual(payload, {"ok": True, "scenes": []})

    def test_get_unknown_returns_404(self):
        result = self._route("/api/scenes/<key>", "GET")("nope")
        self.assertEqual(result, ({"ok": False, "error": "scene not found"}, 404))

    # ---- create -------------------------------------------------------

    def test_create_returns_scene_and_broadcasts_sse(self):
        self._set_body({"label": "Demo", "actions": [{"kind": "sync"}]})
        result = self._route("/api/scenes", "POST")()
        self.assertTrue(result["ok"])
        self.assertEqual(result["scene"]["label"], "Demo")
        self.assertEqual(result["scene"]["key"], "demo")
        self.assertEqual(self.ctx.sse.broadcasts, [("refresh", {"what": ["scenes"]})])

    def test_create_missing_label_returns_400(self):
        self._set_body({"actions": []})
        result = self._route("/api/scenes", "POST")()
        self.assertEqual(result, ({"ok": False, "error": "label is required"}, 400))
        self.assertEqual(self.ctx.sse.broadcasts, [])

    def test_create_invalid_actions_returns_400(self):
        self._set_body({"label": "Bad", "actions": [{"kind": "unknown"}]})
        result = self._route("/api/scenes", "POST")()
        self.assertEqual(result[1], 400)
        self.assertEqual(self.ctx.sse.broadcasts, [])

    # ---- estimate -----------------------------------------------------

    def test_editor_schema_includes_offset_group_and_lora_params(self):
        payload = self._route("/api/scenes/editor-schema", "GET")()
        self.assertIn("offset_group", payload)
        og = payload["offset_group"]
        self.assertIn("modes", og)
        self.assertIn("max_groups", og)
        self.assertIn("child_kinds", og)
        self.assertIn("lora", payload)
        self.assertIn("sf", payload["lora"])
        self.assertIn("bw_hz", payload["lora"])

    def test_editor_schema_advertises_unified_target_kinds(self):
        # Pin the schema vocabulary at the unified shape — see
        # scenes_service._canonical_target and the broadcast-ruleset doc.
        # If a future contributor accidentally reintroduces the legacy
        # values ("scope" / singular "group"), this test surfaces it.
        payload = self._route("/api/scenes/editor-schema", "GET")()
        self.assertEqual(
            payload["target_kinds"],
            ["broadcast", "groups", "device"],
        )
        self.assertEqual(
            payload["container_target_kinds"],
            ["broadcast", "groups"],
        )
        self.assertEqual(
            payload["offset_group"]["child_target_kinds"],
            ["broadcast", "groups", "device"],
        )
        self.assertTrue(payload["offset_group"]["supports_broadcast_target"])

    def test_estimate_for_saved_scene_returns_per_action_and_total(self):
        self._set_body({"label": "Cost", "actions": [
            {"kind": "sync"},
            {"kind": "delay", "duration_ms": 50},
        ]})
        self._route("/api/scenes", "POST")()
        result = self._route("/api/scenes/<key>/estimate", "GET")("cost")
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["per_action"]), 2)
        # sync = 1 packet; delay = 0 packets but +50 ms airtime.
        self.assertEqual(result["total"]["packets"], 1)
        self.assertGreaterEqual(result["total"]["airtime_ms"], 50.0)
        self.assertIn("lora", result)

    def test_estimate_for_unknown_scene_returns_404(self):
        result = self._route("/api/scenes/<key>/estimate", "GET")("ghost")
        self.assertEqual(result, ({"ok": False, "error": "scene not found"}, 404))

    def test_estimate_draft_endpoint_validates_actions(self):
        self._set_body({"label": "Draft", "actions": [{"kind": "unknown"}]})
        result = self._route("/api/scenes/estimate", "POST")()
        self.assertEqual(result[1], 400)

    def test_estimate_draft_endpoint_returns_cost_for_valid_draft(self):
        self._set_body({
            "label": "Draft",
            "actions": [
                {"kind": "offset_group",
                 "groups": "all",
                 "offset": {"mode": "linear", "base_ms": 0, "step_ms": 100},
                 "actions": [
                     {"kind": "wled_control",
                      "target": {"kind": "scope"},
                      "params": {"mode": 5}},
                 ]},
                {"kind": "sync"},
            ],
        })
        result = self._route("/api/scenes/estimate", "POST")()
        self.assertTrue(result["ok"])
        # offset_group: 1 OPC_OFFSET + 1 child = 2; sync = 1. Total = 3.
        self.assertEqual(result["total"]["packets"], 3)
        # Per-action detail surfaces the optimizer strategy.
        self.assertEqual(
            result["per_action"][0]["detail"]["wire_path"],
            "A_broadcast_formula",
        )

    def test_known_group_ids_from_ctx_reads_repo_directly(self):
        """Pin the Phase-A bug fix:
        ``_known_group_ids_from_ctx()`` must read
        ``ctx.rl_instance.device_repository`` *directly* — not via a
        non-existent ``rl_instance.controller`` indirection. Earlier
        code added the indirection, silently returned ``[]``, and
        closed the optimizer's Strategy-C gate so the estimator
        under-reported by reaching for Strategy B (per-group EXPLICIT)
        where the runtime would do Strategy C (broadcast formula +
        sparse NONE overrides). Reproducer: 7-of-10 sparse linear
        offset_group with one broadcast child — runtime emits 5
        packets, estimator (pre-fix) reported 8.
        """
        # Inject a device_repository onto the fake rl_instance so the
        # helper can find 10 known group ids (one device per group).
        class _Repo:
            def __init__(self, devices):
                self._devices = devices
            def list(self):
                return self._devices

        class _D:
            def __init__(self, group_id):
                self.groupId = group_id

        # Fleet of 10 groups, gids 1..10.
        self.ctx.rl_instance.device_repository = _Repo(
            [_D(g) for g in range(1, 11)]
        )

        # Sparse offset_group — 7 of 10 known groups, mode=linear,
        # broadcast child. Pre-fix: 7 + 1 = 8 packets (Strategy B).
        # Post-fix: 1 broadcast formula + 3 NONE-overrides + 1 child
        # broadcast = 5 packets (Strategy C).
        self._set_body({
            "label": "SparseLinear",
            "actions": [
                {"kind": "offset_group",
                 "target": {"kind": "groups", "value": [1, 2, 3, 4, 5, 6, 7]},
                 "offset": {"mode": "linear", "base_ms": 0, "step_ms": 100},
                 "actions": [
                     {"kind": "wled_control",
                      "target": {"kind": "broadcast"},
                      "params": {"mode": 5}},
                 ]},
            ],
        })
        result = self._route("/api/scenes/estimate", "POST")()
        self.assertTrue(result["ok"])
        # Strategy C wins on packet count: 1 + 3 + 1 = 5.
        self.assertEqual(result["total"]["packets"], 5)
        self.assertEqual(
            result["per_action"][0]["detail"]["wire_path"],
            "C_formula_plus_overrides",
        )

    # ---- update / delete / duplicate ----------------------------------

    def test_update_partial_label(self):
        self._set_body({"label": "Original", "actions": []})
        self._route("/api/scenes", "POST")()

        self._set_body({"label": "Renamed"})
        result = self._route("/api/scenes/<key>", "PUT")("original")
        self.assertTrue(result["ok"])
        self.assertEqual(result["scene"]["label"], "Renamed")

    def test_update_missing_returns_404(self):
        self._set_body({"label": "x"})
        result = self._route("/api/scenes/<key>", "PUT")("missing")
        self.assertEqual(result, ({"ok": False, "error": "scene not found"}, 404))

    def test_delete_missing_returns_404(self):
        result = self._route("/api/scenes/<key>", "DELETE")("nope")
        self.assertEqual(result, ({"ok": False, "error": "scene not found"}, 404))

    def test_duplicate_creates_a_copy(self):
        self._set_body({"label": "Source", "actions": [{"kind": "sync"}]})
        self._route("/api/scenes", "POST")()
        self.ctx.sse.broadcasts.clear()

        self._set_body({})
        result = self._route("/api/scenes/<key>/duplicate", "POST")("source")
        self.assertTrue(result["ok"])
        self.assertEqual(result["scene"]["label"], "Source copy")
        self.assertEqual(self.ctx.sse.broadcasts, [("refresh", {"what": ["scenes"]})])

    # ---- run ----------------------------------------------------------

    def test_run_invokes_runner_and_returns_result(self):
        self._set_body({"label": "X", "actions": []})
        self._route("/api/scenes", "POST")()

        result = self._route("/api/scenes/<key>/run", "POST")("x")
        self.assertTrue(result["ok"])
        self.assertEqual(self.runner.calls, ["x"])
        self.assertEqual(result["result"]["scene_key"], "x")

    def test_run_unknown_scene_returns_404(self):
        runner = _FakeSceneRunner(missing_keys={"missing"})
        ctx = _SceneFakeContext(runner=runner, scenes_storage_path=self.scenes_path)
        api = self.api_module
        bp = _FakeBlueprint()
        api.register_api_routes(bp, ctx)
        result = bp.routes[("/api/scenes/<key>/run", ("POST",))]("missing")
        # Unknown scene returns the SceneRunResult dict + 404 status code
        self.assertIsInstance(result, tuple)
        self.assertEqual(result[1], 404)
        self.assertEqual(result[0]["error"], "scene_not_found")

    # ---- ephemeral-draft run (Run executes the displayed scene without
    # overwriting the saved one) ----------------------------------------

    def test_run_with_actions_body_passes_dict_to_runner(self):
        # Persist a baseline scene, then POST run with a body whose actions
        # differ — the runner must receive the body's actions as ``scene``,
        # and the persisted scene under the same key must remain unchanged
        # (only the explicit Save button persists).
        self._set_body({"label": "X", "actions": [{"kind": "sync"}]})
        self._route("/api/scenes", "POST")()
        before = self.ctx.services["scenes"].get("x")
        # Now simulate the editor sending a draft body with a different
        # action set. The fake runner records the ``scene`` kwarg.
        self._set_body({
            "label": "X",
            "actions": [{"kind": "delay", "duration_ms": 25}],
            "stop_on_error": False,
        })
        self._route("/api/scenes/<key>/run", "POST")("x")
        # Runner saw the draft as a dict, with canonicalised actions.
        self.assertEqual(len(self.runner.scene_args), 1)
        scene_arg = self.runner.scene_args[0]
        self.assertIsNotNone(scene_arg)
        self.assertEqual(scene_arg["key"], "x")
        self.assertEqual(scene_arg["actions"][0]["kind"], "delay")
        self.assertEqual(scene_arg["actions"][0]["duration_ms"], 25)
        # stop_on_error from the body wins.
        self.assertFalse(scene_arg["stop_on_error"])
        # Storage untouched: the persisted scene still has the SYNC action.
        after = self.ctx.services["scenes"].get("x")
        self.assertEqual(before["actions"], after["actions"])
        self.assertEqual(after["actions"][0]["kind"], "sync")

    def test_run_without_actions_body_uses_storage(self):
        # Body without an ``actions`` key (or empty body) keeps the legacy
        # behaviour: runner reads from storage. ``scene`` kwarg stays None.
        self._set_body({"label": "X", "actions": [{"kind": "sync"}]})
        self._route("/api/scenes", "POST")()
        self._set_body({})  # explicitly empty
        self._route("/api/scenes/<key>/run", "POST")("x")
        self.assertEqual(self.runner.scene_args, [None])

    def test_run_with_actions_body_inherits_saved_stop_on_error(self):
        # Body without ``stop_on_error`` falls back to the persisted scene's
        # value so toggling the saved checkbox still influences a draft run
        # when the operator hasn't touched the box in this session.
        self._set_body({"label": "X", "actions": [{"kind": "sync"}], "stop_on_error": False})
        self._route("/api/scenes", "POST")()
        self._set_body({"actions": [{"kind": "sync"}]})  # no stop_on_error
        self._route("/api/scenes/<key>/run", "POST")("x")
        self.assertEqual(len(self.runner.scene_args), 1)
        self.assertFalse(self.runner.scene_args[0]["stop_on_error"])

    def test_run_with_actions_body_for_unknown_key_runs_anyway(self):
        # The draft body is the source of truth — the saved scene under
        # ``key`` may not exist (deleted from another tab mid-edit) and
        # the run must still proceed against the supplied actions.
        self._set_body({"actions": [{"kind": "sync"}]})
        out = self._route("/api/scenes/<key>/run", "POST")("ghost")
        # Should NOT 404 — the ``scene`` kwarg short-circuits the lookup.
        self.assertNotIsInstance(out, tuple)
        self.assertTrue(out["ok"])
        self.assertEqual(self.runner.calls, ["ghost"])
        self.assertIsNotNone(self.runner.scene_args[0])

    def test_run_with_invalid_actions_body_returns_400(self):
        # Validation runs through ``_canonical_actions``; an unknown kind
        # surfaces as a 400 instead of being silently dispatched.
        self._set_body({"actions": [{"kind": "nope"}]})
        out = self._route("/api/scenes/<key>/run", "POST")("x")
        self.assertIsInstance(out, tuple)
        self.assertEqual(out[1], 400)
        self.assertFalse(out[0]["ok"])
        # Runner never called — validation short-circuits.
        self.assertEqual(self.runner.calls, [])

    # ---- R7 progress wiring -------------------------------------------

    def test_run_emits_scene_progress_events_per_action(self):
        # Script the fake runner with two transitions per action × two actions.
        scripted = [
            {"index": 0, "kind": "rl_preset", "status": "running"},
            {"index": 0, "kind": "rl_preset", "status": "ok", "duration_ms": 12},
            {"index": 1, "kind": "sync",      "status": "running"},
            {"index": 1, "kind": "sync",      "status": "ok", "duration_ms": 1},
        ]
        runner = _FakeSceneRunner(progress_events=scripted)
        ctx = _SceneFakeContext(runner=runner, scenes_storage_path=self.scenes_path)
        api = self.api_module
        bp = _FakeBlueprint()
        api.register_api_routes(bp, ctx)

        # Need an existing scene so the route doesn't 404. Easiest: create one.
        self._flask_request.get_json = lambda silent=True: {"label": "X", "actions": []}
        bp.routes[("/api/scenes", ("POST",))]()
        ctx.sse.broadcasts.clear()  # drop the SCENES refresh from the create

        bp.routes[("/api/scenes/<key>/run", ("POST",))]("x")
        topics = [t for t, _ in ctx.sse.broadcasts]
        self.assertEqual(topics, ["scene_progress"] * 4)
        payloads = [p for _, p in ctx.sse.broadcasts]
        # scene_key is added by the fake; also assert the payload pass-through.
        self.assertEqual([p["index"] for p in payloads], [0, 0, 1, 1])
        self.assertEqual([p["status"] for p in payloads], ["running", "ok", "running", "ok"])
        self.assertEqual([p["scene_key"] for p in payloads], ["x"] * 4)

    def test_run_with_unknown_scene_emits_no_progress(self):
        scripted = [{"index": 0, "kind": "sync", "status": "running"}]
        runner = _FakeSceneRunner(missing_keys={"missing"}, progress_events=scripted)
        ctx = _SceneFakeContext(runner=runner, scenes_storage_path=self.scenes_path)
        api = self.api_module
        bp = _FakeBlueprint()
        api.register_api_routes(bp, ctx)

        bp.routes[("/api/scenes/<key>/run", ("POST",))]("missing")
        # 404 path short-circuits before progress events fire — no SSE noise.
        topics = [t for t, _ in ctx.sse.broadcasts]
        self.assertNotIn("scene_progress", topics)


class WebApiStaticGuardTests(unittest.TestCase):
    def test_web_api_has_no_free_get_specials_config_symbol(self):
        path = ROOT / "racelink" / "web" / "api.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = set()
        assigned = set()
        used = set()

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    imported.add(alias.asname or alias.name)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                assigned.add(node.name)
                for arg in getattr(node, "args", ast.arguments()).args:
                    assigned.add(arg.arg)
            elif isinstance(node, ast.Name):
                if isinstance(node.ctx, ast.Store):
                    assigned.add(node.id)
                elif isinstance(node.ctx, ast.Load):
                    used.add(node.id)

        free_names = used - imported - assigned - set(dir(__builtins__))
        self.assertNotIn("get_specials_config", free_names)


if __name__ == "__main__":
    unittest.main()
