import contextlib
import importlib
import sys
import types
import unittest
from pathlib import Path

from racelink.domain import RL_DeviceGroup


def _ensure_flask_stub():
    flask = sys.modules.get("flask")
    if flask is None:
        flask = types.ModuleType("flask")
        sys.modules["flask"] = flask

    class Blueprint:
        def __init__(self, name, import_name, **kwargs):
            self.name = name
            self.import_name = import_name
            self.kwargs = kwargs
            self.routes = {}

        def route(self, rule, methods=None):
            def _decorator(fn):
                self.routes[(rule, tuple(methods or ("GET",)))] = fn
                return fn

            return _decorator

    flask.Blueprint = Blueprint
    flask.request = types.SimpleNamespace(get_json=lambda silent=True: {}, files={}, form={})
    flask.jsonify = lambda payload: payload
    flask.Response = type("Response", (), {})
    flask.stream_with_context = lambda fn: fn
    flask.templating = types.SimpleNamespace(render_template=lambda *args, **kwargs: {"args": args, "kwargs": kwargs})


class _FakeApp:
    def __init__(self):
        self.blueprints = []

    def register_blueprint(self, blueprint):
        self.blueprints.append(blueprint)


class _FakeRuntime:
    def __init__(self):
        self.rl_instance = type("RL", (), {"uiPresetList": [{"value": "01", "label": "Red"}]})()
        self.state_repository = None
        self.rl_devicelist = []
        self.rl_grouplist = [RL_DeviceGroup("Group 1")]
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
        self.RL_DeviceGroup = RL_DeviceGroup
        self.logger = None
        self.option_getter = lambda _key, default=None: default
        self.translator = lambda text: text
        self.blueprint_registrar = None

    def option(self, key, default=None):
        return self.option_getter(key, default)

    def translate(self, text):
        return self.translator(text)


class WebRegistrationTests(unittest.TestCase):
    def setUp(self):
        _ensure_flask_stub()
        for name in ("racelink.web", "racelink.web.blueprint", "racelink.web.api", "racelink.web.sse"):
            sys.modules.pop(name, None)
        self.web = importlib.import_module("racelink.web")

    def test_register_racelink_web_mounts_prefix_aware_blueprint(self):
        app = _FakeApp()
        runtime = _FakeRuntime()

        bp = self.web.register_racelink_web(app, runtime, url_prefix="/shared-ui")

        self.assertEqual(len(app.blueprints), 1)
        self.assertIs(app.blueprints[0], bp)
        self.assertEqual(bp.kwargs.get("url_prefix"), "/shared-ui")
        self.assertEqual(bp.kwargs.get("static_url_path"), "/static")
        self.assertIn(("/", ("GET",)), bp.routes)
        self.assertIn(("/api/devices", ("GET",)), bp.routes)
        self.assertIn(("/api/events", ("GET",)), bp.routes)

    def test_asset_dirs_resolve_to_existing_paths(self):
        blueprint = importlib.import_module("racelink.web.blueprint")
        template_dir, static_dir = blueprint._resolve_asset_dirs()

        self.assertTrue(Path(template_dir).is_dir())
        self.assertTrue(Path(static_dir).is_dir())
        self.assertEqual(Path(template_dir).name, "pages")
        self.assertEqual(Path(static_dir).name, "static")
        self.assertEqual(Path(template_dir).parent.name, "racelink")
        self.assertEqual(Path(static_dir).parent.name, "racelink")

    def test_root_render_injects_base_and_static_paths(self):
        app = _FakeApp()
        runtime = _FakeRuntime()
        bp = self.web.register_racelink_web(app, runtime, url_prefix="/shared-ui")

        rendered = bp.routes[("/", ("GET",))]()
        kwargs = rendered["kwargs"]

        self.assertEqual(kwargs["rl_base_path"], "/shared-ui")
        self.assertEqual(kwargs["rl_static_path"], "/shared-ui/static")


if __name__ == "__main__":
    unittest.main()
