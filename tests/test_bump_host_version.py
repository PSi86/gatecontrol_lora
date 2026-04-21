import pathlib
import shutil
import textwrap
import unittest
from uuid import uuid4

from scripts.bump_host_version import bump_host_version


ROOT = pathlib.Path(__file__).resolve().parents[1]


class BumpHostVersionTests(unittest.TestCase):
    def _write_version_file(self, version: str) -> pathlib.Path:
        temp_dir = ROOT / f".bump-host-version-{uuid4().hex}"
        temp_dir.mkdir()
        self.addCleanup(shutil.rmtree, temp_dir, True)
        version_file = temp_dir / "_version.py"
        version_file.write_text(
            textwrap.dedent(
                f"""\
                \"\"\"Canonical RaceLink Host version helpers.\"\"\"

                VERSION = "{version}"
                __version__ = VERSION


                def get_version() -> str:
                    return VERSION
                """
            ),
            encoding="utf-8",
        )
        return version_file

    def test_explicit_version_is_normalized(self):
        version_file = self._write_version_file("0.1.0")

        version = bump_host_version(version_file=version_file, version="v1.2.3")

        self.assertEqual(version, "1.2.3")
        self.assertIn('VERSION = "1.2.3"', version_file.read_text(encoding="utf-8"))

    def test_empty_version_increments_patch(self):
        version_file = self._write_version_file("0.1.0")

        version = bump_host_version(version_file=version_file, version="")

        self.assertEqual(version, "0.1.1")
        self.assertIn('VERSION = "0.1.1"', version_file.read_text(encoding="utf-8"))

    def test_empty_version_preserves_suffix(self):
        version_file = self._write_version_file("0.1.0-rc1")

        version = bump_host_version(version_file=version_file, version="")

        self.assertEqual(version, "0.1.1-rc1")
        self.assertIn('VERSION = "0.1.1-rc1"', version_file.read_text(encoding="utf-8"))

    def test_invalid_explicit_version_fails(self):
        version_file = self._write_version_file("0.1.0")

        with self.assertRaisesRegex(ValueError, "Version must look like semantic versioning"):
            bump_host_version(version_file=version_file, version="banana")

    def test_invalid_current_version_fails(self):
        version_file = self._write_version_file("banana")

        with self.assertRaisesRegex(ValueError, "Current host version is not valid semver"):
            bump_host_version(version_file=version_file, version="")


if __name__ == "__main__":
    unittest.main()
