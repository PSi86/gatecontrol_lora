import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class ReleaseWorkflowTests(unittest.TestCase):
    def test_release_workflow_uses_manual_dispatch_and_builds_artifacts(self):
        source = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

        self.assertIn("workflow_dispatch:", source)
        self.assertIn('description: "Optional host version override. Leave empty to auto-increment."', source)
        self.assertIn('description: "Branch to release from"', source)
        self.assertIn("python -m build", source)
        self.assertIn("softprops/action-gh-release@v3", source)
        self.assertIn("dist/*.whl", source)
        self.assertIn("dist/*.tar.gz", source)
        self.assertIn("dist/*-sha256.txt", source)
        self.assertIn("SOURCE_DATE_EPOCH", source)
        self.assertIn("python scripts/bump_host_version.py --version", source)

    def test_release_workflow_commits_tags_and_releases_computed_version(self):
        source = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

        self.assertIn('echo "tag=v${version}" >> "$GITHUB_OUTPUT"', source)
        self.assertIn('git commit -m "Release v${{ steps.host_version.outputs.version }}"', source)
        self.assertIn('git tag "${{ steps.host_version.outputs.tag }}"', source)
        self.assertIn('git push origin "HEAD:${{ inputs.target_branch }}" --follow-tags', source)
        self.assertIn("tag_name: ${{ steps.host_version.outputs.tag }}", source)
        self.assertIn("target_commitish: ${{ inputs.target_branch }}", source)
        self.assertIn('version = "${{ steps.host_version.outputs.version }}"', source)


if __name__ == "__main__":
    unittest.main()
