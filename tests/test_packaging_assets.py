import pathlib
import os
import shutil
import tarfile
import unittest
import zipfile

from racelink import _build_backend


ROOT = pathlib.Path(__file__).resolve().parents[1]


class PackagingAssetsTests(unittest.TestCase):
    def test_artifact_filenames_are_stable(self):
        self.assertEqual(_build_backend._wheel_name(), "racelink_host-0.1.0-py3-none-any.whl")
        self.assertEqual(_build_backend._sdist_name(), "racelink-host-0.1.0.tar.gz")

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

    def test_built_sdist_contains_packaged_webui_assets(self):
        build_dir = ROOT / ".sdist-test"
        if build_dir.exists():
            shutil.rmtree(build_dir)
        build_dir.mkdir()
        try:
            sdist_name = _build_backend.build_sdist(str(build_dir))
            sdist_path = build_dir / sdist_name
            with tarfile.open(sdist_path, "r:gz") as archive:
                names = {member.name for member in archive.getmembers()}
        finally:
            shutil.rmtree(build_dir)

        prefix = f"{_build_backend.NAME}-{_build_backend.VERSION}"
        self.assertIn(f"{prefix}/racelink/pages/racelink.html", names)
        self.assertIn(f"{prefix}/racelink/static/racelink.css", names)
        self.assertIn(f"{prefix}/racelink/static/racelink.js", names)

    def test_builds_are_reproducible_with_fixed_source_date_epoch(self):
        build_root = ROOT / ".repro-build-test"
        if build_root.exists():
            shutil.rmtree(build_root)
        first_dir = build_root / "first"
        second_dir = build_root / "second"
        first_dir.mkdir(parents=True)
        second_dir.mkdir(parents=True)
        original_epoch = os.environ.get("SOURCE_DATE_EPOCH")
        os.environ["SOURCE_DATE_EPOCH"] = "1713446400"
        try:
            first_wheel = first_dir / _build_backend.build_wheel(str(first_dir))
            first_sdist = first_dir / _build_backend.build_sdist(str(first_dir))
            second_wheel = second_dir / _build_backend.build_wheel(str(second_dir))
            second_sdist = second_dir / _build_backend.build_sdist(str(second_dir))
            first_wheel_bytes = first_wheel.read_bytes()
            first_sdist_bytes = first_sdist.read_bytes()
            second_wheel_bytes = second_wheel.read_bytes()
            second_sdist_bytes = second_sdist.read_bytes()
        finally:
            if original_epoch is None:
                os.environ.pop("SOURCE_DATE_EPOCH", None)
            else:
                os.environ["SOURCE_DATE_EPOCH"] = original_epoch
            shutil.rmtree(build_root)

        self.assertEqual(first_wheel_bytes, second_wheel_bytes)
        self.assertEqual(first_sdist_bytes, second_sdist_bytes)


if __name__ == "__main__":
    unittest.main()
