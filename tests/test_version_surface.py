import os
import pathlib
import shutil
import subprocess
import unittest
import zipfile
from uuid import uuid4
import venv

import racelink
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


class VersionSurfaceTests(unittest.TestCase):
    def test_public_version_api_is_canonical(self):
        from racelink._version import VERSION, __version__, get_version

        self.assertEqual(racelink.__version__, VERSION)
        self.assertEqual(racelink.VERSION, VERSION)
        self.assertEqual(__version__, VERSION)
        self.assertEqual(racelink.get_version(), VERSION)
        self.assertEqual(get_version(), VERSION)
        self.assertEqual(_build_backend.VERSION, VERSION)

    def test_pyproject_declares_dynamic_version_and_script(self):
        source = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

        self.assertIn('dynamic = ["version"]', source)
        self.assertNotIn('version = "0.1.0"', source)
        self.assertIn('racelink-host-version = "racelink._version:print_version"', source)

    def test_installed_wheel_reports_same_version_without_dist_metadata_lookup(self):
        temp_dir = ROOT / f".version-smoke-{uuid4().hex}"
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        temp_dir.mkdir()
        try:
            dist_dir = temp_dir / "dist"
            venv_dir = temp_dir / "venv"
            dist_dir.mkdir()
            wheel_name = _build_backend.build_wheel(str(dist_dir))
            wheel_path = dist_dir / wheel_name

            venv.EnvBuilder(with_pip=False).create(venv_dir)
            python_exe = _venv_python(venv_dir)
            site_packages_dir = _site_packages_dir(python_exe, temp_dir)
            site_packages_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(wheel_path) as archive:
                archive.extractall(site_packages_dir)

            result = subprocess.run(
                [
                    str(python_exe),
                    "-c",
                    "import racelink; print(racelink.__version__); print(racelink.get_version())",
                ],
                check=True,
                cwd=str(temp_dir),
                capture_output=True,
                text=True,
            )

            lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            self.assertEqual(lines, [racelink.__version__, racelink.__version__])
        finally:
            if temp_dir.exists():
                shutil.rmtree(temp_dir)


if __name__ == "__main__":
    unittest.main()
