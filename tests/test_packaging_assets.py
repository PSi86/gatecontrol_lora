import pathlib
import shutil
import unittest
import zipfile

from racelink import _build_backend


ROOT = pathlib.Path(__file__).resolve().parents[1]


class PackagingAssetsTests(unittest.TestCase):
    def test_iter_sources_includes_webui_assets(self):
        rel_paths = {rel_path for _src, rel_path in _build_backend._iter_sources()}

        self.assertIn("racelink/pages/racelink.html", rel_paths)
        self.assertIn("racelink/static/racelink.css", rel_paths)
        self.assertIn("racelink/static/racelink.js", rel_paths)

    def test_built_wheel_contains_webui_assets(self):
        build_dir = ROOT / ".wheel-test"
        if build_dir.exists():
            shutil.rmtree(build_dir)
        build_dir.mkdir()
        try:
            wheel_name = _build_backend.build_wheel(str(build_dir))
            wheel_path = build_dir / wheel_name
            with zipfile.ZipFile(wheel_path) as archive:
                names = set(archive.namelist())
        finally:
            shutil.rmtree(build_dir)

        self.assertIn("racelink/pages/racelink.html", names)
        self.assertIn("racelink/static/racelink.css", names)
        self.assertIn("racelink/static/racelink.js", names)


if __name__ == "__main__":
    unittest.main()
