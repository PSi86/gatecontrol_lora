import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class ReleaseWorkflowTests(unittest.TestCase):
    def test_release_workflow_builds_tagged_artifacts(self):
        source = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

        self.assertIn('      - "v*.*.*"', source)
        self.assertIn("python -m build", source)
        self.assertIn("softprops/action-gh-release@v2", source)
        self.assertIn("dist/*.whl", source)
        self.assertIn("dist/*.tar.gz", source)
        self.assertIn("dist/*-sha256.txt", source)
        self.assertIn("SOURCE_DATE_EPOCH", source)

    def test_readme_documents_stable_release_filenames(self):
        source = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("racelink_host-<version>-py3-none-any.whl", source)
        self.assertIn("racelink-host-<version>.tar.gz", source)
        self.assertIn("racelink-host-<version>-sha256.txt", source)


if __name__ == "__main__":
    unittest.main()
