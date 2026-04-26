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
        self._ok = ok
        self._error = error
        self._actions = actions or []
        self._missing_keys = set(missing_keys)
        self._progress_events = list(progress_events or [])
        self.last_progress_cb = None

    def run(self, key, *, progress_cb=None):
        from racelink.services.scene_runner_service import SceneRunResult
        self.calls.append(key)
        self.last_progress_cb = progress_cb
        if key in self._missing_keys:
            # Match the real runner: scene-not-found short-circuits BEFORE any
            # progress event fires, so a misdirected POST doesn't leak SSE
            # noise to other tabs.
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
