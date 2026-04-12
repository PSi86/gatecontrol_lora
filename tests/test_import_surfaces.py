import importlib.util
import importlib
import pathlib
import sys
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


def _ensure_rotorhazard_import_stubs():
    _ensure_serial_stub()

    if "eventmanager" not in sys.modules:
        eventmanager = types.ModuleType("eventmanager")
        eventmanager.Evt = types.SimpleNamespace(
            DATA_IMPORT_INITIALIZE="DATA_IMPORT_INITIALIZE",
            DATA_EXPORT_INITIALIZE="DATA_EXPORT_INITIALIZE",
            ACTIONS_INITIALIZE="ACTIONS_INITIALIZE",
            STARTUP="STARTUP",
            RACE_START="RACE_START",
            RACE_FINISH="RACE_FINISH",
            RACE_STOP="RACE_STOP",
        )
        sys.modules["eventmanager"] = eventmanager

    if "RHUI" not in sys.modules:
        rhui = types.ModuleType("RHUI")

        class UIField:
            def __init__(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs

        class UIFieldSelectOption:
            def __init__(self, value, label):
                self.value = value
                self.label = label

        rhui.UIField = UIField
        rhui.UIFieldSelectOption = UIFieldSelectOption
        rhui.UIFieldType = types.SimpleNamespace(TEXT="TEXT", SELECT="SELECT", BASIC_INT="BASIC_INT", CHECKBOX="CHECKBOX")
        sys.modules["RHUI"] = rhui

    if "EventActions" not in sys.modules:
        event_actions = types.ModuleType("EventActions")

        class ActionEffect:
            def __init__(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs

        event_actions.ActionEffect = ActionEffect
        sys.modules["EventActions"] = event_actions

    if "data_import" not in sys.modules:
        data_import = types.ModuleType("data_import")

        class DataImporter:
            def __init__(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs

        data_import.DataImporter = DataImporter
        sys.modules["data_import"] = data_import

    if "data_export" not in sys.modules:
        data_export = types.ModuleType("data_export")

        class DataExporter:
            def __init__(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs

        data_export.DataExporter = DataExporter
        sys.modules["data_export"] = data_export

    if "flask" not in sys.modules:
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
        flask.templating = types.SimpleNamespace(render_template=lambda *args, **kwargs: {})
        flask.request = types.SimpleNamespace(args={}, json=None, form={})
        flask.jsonify = lambda *args, **kwargs: {"args": args, "kwargs": kwargs}
        flask.Response = type("Response", (), {})
        flask.stream_with_context = lambda fn: fn
        sys.modules["flask"] = flask


def _load_root_plugin():
    module_name = "gatecontrol_root_plugin_test"
    if module_name in sys.modules:
        return sys.modules[module_name]

    package = types.ModuleType(module_name)
    package.__path__ = [str(ROOT)]
    sys.modules[module_name] = package

    _ensure_rotorhazard_import_stubs()

    spec = importlib.util.spec_from_file_location(module_name, ROOT / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class ImportSurfaceTests(unittest.TestCase):
    def test_root_plugin_surface_is_minimal(self):
        _ensure_serial_stub()
        module = _load_root_plugin()
        self.assertEqual(set(module.__all__), {"initialize"})
        self.assertTrue(callable(module.initialize))
        self.assertFalse(hasattr(module, "rl_app"))
        self.assertFalse(hasattr(module, "rl_instance"))

    def test_canonical_package_imports_exist(self):
        _ensure_serial_stub()
        import racelink.domain  # noqa: F401
        import racelink.integrations.rotorhazard  # noqa: F401
        import racelink.transport  # noqa: F401
        import racelink.web  # noqa: F401

    def test_rotorhazard_plugin_import_uses_package_protocol_mirror(self):
        _ensure_rotorhazard_import_stubs()
        root_plugin = _load_root_plugin()
        plugin_module = importlib.import_module(f"{root_plugin.__name__}.racelink.integrations.rotorhazard.plugin")

        self.assertTrue(hasattr(plugin_module, "initialize"))


if __name__ == "__main__":
    unittest.main()
