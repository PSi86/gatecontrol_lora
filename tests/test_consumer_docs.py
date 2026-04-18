import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class ConsumerDocsTests(unittest.TestCase):
    def test_readme_documents_online_and_offline_package_consumption(self):
        source = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("## Consuming `racelink-host` from other repositories", source)
        self.assertIn("python -m pip install ./racelink_host-0.1.0-py3-none-any.whl", source)
        self.assertIn("python -m pip install --no-index ./racelink_host-0.1.0-py3-none-any.whl", source)
        self.assertIn("RaceLink_RH-plugin", source)
        self.assertIn("Offline bundles should be populated from the published wheel", source)
        self.assertIn("not from a source checkout snapshot", source)

    def test_repo_split_map_references_packaged_webui_asset_paths(self):
        source = (ROOT / "docs" / "repo_split_map.md").read_text(encoding="utf-8")

        self.assertIn("`racelink/pages/**` and `racelink/static/**`", source)


if __name__ == "__main__":
    unittest.main()
