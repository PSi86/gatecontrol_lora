import pathlib
import sys
import tempfile
import types
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


def _ensure_serial_stub():
    if "serial" in sys.modules:
        return
    serial_stub = types.ModuleType("serial")
    serial_stub.Serial = object
    serial_stub.SerialException = Exception
    sys.modules["serial"] = serial_stub

    serial_tools = types.ModuleType("serial.tools")
    serial_list_ports = types.ModuleType("serial.tools.list_ports")
    serial_list_ports.comports = lambda: []
    serial_tools.list_ports = serial_list_ports
    serial_stub.tools = serial_tools
    sys.modules["serial.tools"] = serial_tools
    sys.modules["serial.tools.list_ports"] = serial_list_ports


def _ensure_flask_stub():
    flask = sys.modules.get("flask")
    if flask is None:
        flask = types.ModuleType("flask")
        sys.modules["flask"] = flask

    class Flask:
        def __init__(self, name, *args, **kwargs):
            self.name = name
            self.args = args
            self.kwargs = kwargs
            self.blueprints = {}
            self.routes = {}

        def register_blueprint(self, blueprint):
            self.blueprints[blueprint.name] = blueprint

        def route(self, rule, methods=None):
            def _decorator(fn):
                self.routes[(rule, tuple(methods or ("GET",)))] = fn
                return fn

            return _decorator

        def run(self, *args, **kwargs):
            return None

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

    flask.Flask = Flask
    flask.Blueprint = Blueprint
    flask.templating = types.SimpleNamespace(render_template=lambda *args, **kwargs: {"args": args, "kwargs": kwargs})
    flask.request = types.SimpleNamespace(args={}, json=None, form={}, files={}, get_json=lambda silent=True: {})
    flask.jsonify = lambda *args, **kwargs: {"args": args, "kwargs": kwargs}
    flask.Response = type("Response", (), {})
    flask.stream_with_context = lambda fn: fn


class StandaloneRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for name in (
            "flask",
            "racelink.integrations.standalone",
            "racelink.integrations.standalone.bootstrap",
            "racelink.integrations.standalone.config",
            "racelink.integrations.standalone.webapp",
        ):
            sys.modules.pop(name, None)
        _ensure_serial_stub()
        _ensure_flask_stub()
        from racelink.integrations.standalone import StandaloneConfig
        from racelink.integrations.standalone.bootstrap import build_standalone_runtime
        from racelink.integrations.standalone.webapp import create_standalone_app

        cls.StandaloneConfig = StandaloneConfig
        cls.build_standalone_runtime = staticmethod(build_standalone_runtime)
        cls.create_standalone_app = staticmethod(create_standalone_app)

    def _temp_path(self, filename: str) -> pathlib.Path:
        """Return a temp-dir path scoped to the current test.

        Using ``tempfile.TemporaryDirectory`` keeps artifacts outside the repo
        root so a mid-run crash cannot leave ``.standalone-test-*`` files
        behind for ``git status`` to surface.
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return pathlib.Path(tmp.name) / filename

    def test_default_config_load_returns_expected_defaults(self):
        config_path = self._temp_path("standalone_config.json")
        try:
            config = self.StandaloneConfig.load(str(config_path))

            self.assertEqual(config.path, str(config_path))
            self.assertEqual(config.host, "127.0.0.1")
            self.assertEqual(config.port, 5077)
            self.assertFalse(config.debug)
            self.assertEqual(config.options, {})
        finally:
            if config_path.exists():
                config_path.unlink()

    def test_config_save_and_load_round_trip(self):
        config_path = self._temp_path("standalone_config.json")
        config_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            config = self.StandaloneConfig(
                path=str(config_path),
                host="0.0.0.0",
                port=5088,
                debug=True,
                options={"psi_comms_port": "COM7", "sample": "value"},
            )
            config.save()

            loaded = self.StandaloneConfig.load(str(config_path))

            self.assertEqual(loaded.host, "0.0.0.0")
            self.assertEqual(loaded.port, 5088)
            self.assertTrue(loaded.debug)
            self.assertEqual(loaded.options["psi_comms_port"], "COM7")
            self.assertEqual(loaded.options["sample"], "value")
        finally:
            if config_path.exists():
                config_path.unlink()

    def test_create_standalone_app_returns_expected_runtime_shape(self):
        config_path = self._temp_path("standalone_config.json")
        try:
            config = self.StandaloneConfig(path=str(config_path))
            app, rl_app = self.create_standalone_app(config)

            self.assertEqual(getattr(app, "import_name", getattr(app, "name", None)), "racelink_standalone")
            self.assertIn("racelink", app.blueprints)
            self.assertIn(("/", ("GET",)), app.routes)
            self.assertIn("standalone", rl_app.integrations)
            self.assertIn("flask_app", rl_app.integrations)
            self.assertIs(rl_app.integrations["flask_app"], app)
            for service_name in ("config", "control", "gateway", "discovery", "status", "stream", "sync", "startblock", "ota", "presets", "host_wifi"):
                self.assertIn(service_name, rl_app.services)

            redirect = app.routes[("/", ("GET",))]()
            self.assertEqual(redirect, ("", 302, {"Location": "/racelink"}))
        finally:
            if config_path.exists():
                config_path.unlink()

    def test_build_standalone_runtime_exposes_default_source_and_sink(self):
        config_path = self._temp_path("standalone_config.json")
        try:
            runtime = self.build_standalone_runtime(config=str(config_path))

            self.assertEqual(runtime["config"].path, str(config_path))
            self.assertEqual(getattr(runtime["flask_app"], "import_name", getattr(runtime["flask_app"], "name", None)), "racelink_standalone")
            self.assertIs(runtime["race_link_app"].event_source, runtime["event_source"])
            self.assertIs(runtime["race_link_app"].data_sink, runtime["data_sink"])
        finally:
            if config_path.exists():
                config_path.unlink()


class StandaloneDocsTests(unittest.TestCase):
    # The two former tests in this class asserted against
    # ``docs/standalone.md`` and a "Consuming racelink-host from other
    # repositories" README section. Both targets were deliberately
    # moved to ``RaceLink_Docs/docs/RaceLink_Host/standalone-install.md``
    # during the 2026-04-30 documentation consolidation; the in-repo
    # docs directory was removed. The cross-repo doc layout is now
    # enforced by ``mkdocs build --strict`` in the docs repo. The
    # entrypoint contract (the bit that this Python package owns) is
    # still pinned by ``test_packaging_exposes_standalone_entrypoint``
    # below — that's the load-bearing assertion.

    def test_packaging_exposes_standalone_entrypoint(self):
        source = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

        self.assertIn('racelink-standalone = "racelink.integrations.standalone.webapp:run_standalone"', source)


if __name__ == "__main__":
    unittest.main()
