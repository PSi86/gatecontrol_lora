import os
import pathlib
import shutil
import subprocess
import textwrap
import unittest
import venv
import zipfile
from uuid import uuid4

from racelink import _build_backend


ROOT = pathlib.Path(__file__).resolve().parents[1]


def _venv_python(venv_dir: pathlib.Path) -> pathlib.Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _site_packages_dir(python_exe: pathlib.Path, cwd: pathlib.Path) -> pathlib.Path:
    result = subprocess.run(
        [str(python_exe), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        check=True,
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    return pathlib.Path(result.stdout.strip())


class InstalledArtifactSmokeTests(unittest.TestCase):
    def test_wheel_installs_and_exposes_runtime_surface_without_repo_checkout(self):
        temp_dir = ROOT / f".artifact-smoke-{uuid4().hex}"
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        temp_dir.mkdir()
        try:
            dist_dir = temp_dir / "dist"
            venv_dir = temp_dir / "venv"
            dist_dir.mkdir()

            wheel_name = _build_backend.build_wheel(str(dist_dir))
            _build_backend.build_sdist(str(dist_dir))
            wheel_path = dist_dir / wheel_name

            venv.EnvBuilder(with_pip=False).create(venv_dir)
            python_exe = _venv_python(venv_dir)
            site_packages_dir = _site_packages_dir(python_exe, temp_dir)
            site_packages_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(wheel_path) as archive:
                archive.extractall(site_packages_dir)

            smoke_script = textwrap.dedent(
                """
                import pathlib
                import sys
                import types

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

                flask = types.ModuleType("flask")

                class Flask:
                    def __init__(self, name, *args, **kwargs):
                        self.name = name
                        self.import_name = name
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
                flask.templating = types.SimpleNamespace(
                    render_template=lambda *args, **kwargs: {"args": args, "kwargs": kwargs}
                )
                flask.request = types.SimpleNamespace(args={}, json=None, form={}, files={}, get_json=lambda silent=True: {})
                flask.jsonify = lambda *args, **kwargs: {"args": args, "kwargs": kwargs}
                flask.Response = type("Response", (), {})
                flask.stream_with_context = lambda fn: fn
                sys.modules["flask"] = flask

                import controller
                from racelink.app import create_runtime
                from racelink.integrations.standalone import build_standalone_runtime, create_standalone_app
                from racelink.web import register_rl_blueprint
                from racelink.web.blueprint import _resolve_asset_dirs

                template_dir, static_dir = _resolve_asset_dirs()
                assert pathlib.Path(template_dir).is_dir(), template_dir
                assert pathlib.Path(static_dir).is_dir(), static_dir
                assert pathlib.Path(template_dir, "racelink.html").is_file()
                assert pathlib.Path(static_dir, "racelink.css").is_file()
                assert pathlib.Path(static_dir, "racelink.js").is_file()
                assert callable(create_runtime)
                assert callable(build_standalone_runtime)
                assert callable(create_standalone_app)
                assert callable(register_rl_blueprint)
                assert hasattr(controller, "RaceLink_Host")
                """
            )

            smoke_env = os.environ.copy()
            smoke_env.pop("PYTHONPATH", None)
            result = subprocess.run(
                [str(python_exe), "-c", smoke_script],
                check=False,
                cwd=str(temp_dir),
                env=smoke_env,
                capture_output=True,
                text=True,
            )

            if result.returncode != 0:
                self.fail(
                    "Installed wheel smoke test failed.\n"
                    f"STDOUT:\n{result.stdout}\n"
                    f"STDERR:\n{result.stderr}"
                )
        finally:
            if temp_dir.exists():
                shutil.rmtree(temp_dir)


if __name__ == "__main__":
    unittest.main()
