import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
CONTROLLER_PATH = ROOT / "controller.py"
API_PATH = ROOT / "racelink" / "web" / "api.py"
STANDALONE_WEBAPP_PATH = ROOT / "racelink" / "integrations" / "standalone" / "webapp.py"


class BootstrapContractTests(unittest.TestCase):
    def test_rotorhazard_integration_package_is_removed(self):
        self.assertFalse((ROOT / "racelink" / "integrations" / "rotorhazard").joinpath("__init__.py").exists())
        self.assertFalse((ROOT / "__init__.py").exists())

    def test_controller_no_longer_contains_rotorhazard_adapter_hooks(self):
        source = CONTROLLER_PATH.read_text(encoding="utf-8")
        self.assertNotIn("rh_adapter", source)
        self.assertNotIn("rh_source", source)
        self.assertNotIn("registerActions", source)
        self.assertNotIn("register_settings", source)
        self.assertNotIn("createUiDevList", source)
        self.assertNotIn("createUiGroupList", source)

    def test_web_api_delegates_long_workflows_to_services(self):
        source = API_PATH.read_text(encoding="utf-8")
        self.assertIn("ota_workflows.download_presets", source)
        self.assertIn("ota_workflows.run_firmware_update", source)
        self.assertIn("specials_service.resolve_option", source)
        self.assertIn("specials_service.resolve_action", source)
        # ``host_wifi_service.connect_ap`` lives in ota_workflow_service —
        # the web layer must keep delegating long-running WiFi work to
        # services, not call nmcli helpers directly. Pre-rename
        # ``connect_profile`` is also still asserted-out so a partial
        # revert that re-introduces the old API is caught.
        self.assertNotIn("host_wifi_service.connect_ap(", source)
        self.assertNotIn("host_wifi_service.connect_profile(", source)
        self.assertNotIn("ota_service.wait_for_expected_node(", source)

    def test_standalone_webapp_uses_shared_host_runtime_and_web_registration(self):
        source = STANDALONE_WEBAPP_PATH.read_text(encoding="utf-8")
        self.assertIn("create_runtime(", source)
        self.assertIn("register_racelink_web", source)
        self.assertIn("RaceLinkWebRuntime", source)
        self.assertNotIn("StandaloneRhApiShim", source)


if __name__ == "__main__":
    unittest.main()
