import unittest
import importlib.util
import pathlib


def _load_request_helpers():
    root = pathlib.Path(__file__).resolve().parents[1]
    path = root / "racelink" / "web" / "request_helpers.py"
    spec = importlib.util.spec_from_file_location("request_helpers_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class WebRequestHelpersTests(unittest.TestCase):
    def test_parse_recv3_from_addr_uses_last_three_bytes(self):
        helpers = _load_request_helpers()
        self.assertEqual(helpers.parse_recv3_from_addr("AA:BB:CC:DD:EE:FF"), bytes.fromhex("DDEEFF"))

    def test_parse_wifi_options_applies_defaults(self):
        class FakeOTA:
            @staticmethod
            def wled_base_url(raw):
                return (raw or "http://4.3.2.1").rstrip("/")

        helpers = _load_request_helpers()
        parsed = helpers.parse_wifi_options({}, FakeOTA())
        self.assertEqual(parsed["base_url"], "http://4.3.2.1")
        self.assertEqual(parsed["iface"], "wlan0")
        self.assertTrue(parsed["host_wifi_enable"])


class RequireIntTests(unittest.TestCase):
    """B4: ``require_int`` is the validation helper the audit asked
    for. It must turn each "missing/null/garbage" failure mode into a
    typed exception that web routes can translate to a clean 400."""

    def setUp(self):
        self.helpers = _load_request_helpers()

    def test_returns_int_when_value_is_well_formed(self):
        self.assertEqual(self.helpers.require_int({"id": 7}, "id"), 7)
        self.assertEqual(self.helpers.require_int({"id": "12"}, "id"), 12)

    def test_raises_when_key_missing(self):
        with self.assertRaises(self.helpers.RequestParseError) as cm:
            self.helpers.require_int({}, "id")
        self.assertIn("missing field", str(cm.exception))

    def test_raises_when_value_is_none(self):
        with self.assertRaises(self.helpers.RequestParseError) as cm:
            self.helpers.require_int({"id": None}, "id")
        self.assertIn("must not be null", str(cm.exception))

    def test_raises_when_value_not_coercible_to_int(self):
        with self.assertRaises(self.helpers.RequestParseError) as cm:
            self.helpers.require_int({"id": "abc"}, "id")
        self.assertIn("must be an integer", str(cm.exception))

    def test_raises_when_body_is_not_a_mapping(self):
        with self.assertRaises(self.helpers.RequestParseError):
            self.helpers.require_int(None, "id")  # type: ignore[arg-type]
        with self.assertRaises(self.helpers.RequestParseError):
            self.helpers.require_int(["not", "a", "dict"], "id")  # type: ignore[arg-type]

    def test_label_appears_in_error_message(self):
        """Operator-friendly label overrides the wire field name in the
        error text — useful when the JSON key differs from the concept."""
        with self.assertRaises(self.helpers.RequestParseError) as cm:
            self.helpers.require_int({}, "id", label="group id")
        self.assertIn("group id", str(cm.exception))
        self.assertNotIn("'id'", str(cm.exception))

    def test_min_max_bounds_enforced(self):
        # In-range succeeds.
        self.assertEqual(self.helpers.require_int({"x": 5}, "x", min=0, max=10), 5)
        # Below min → raise.
        with self.assertRaises(self.helpers.RequestParseError):
            self.helpers.require_int({"x": -1}, "x", min=0, max=10)
        # Above max → raise.
        with self.assertRaises(self.helpers.RequestParseError):
            self.helpers.require_int({"x": 11}, "x", min=0, max=10)

    def test_request_parse_error_is_a_value_error_subclass(self):
        """Routes that catch ``ValueError`` (the existing pattern in
        api.py) still see this exception; new code can specialise."""
        self.assertTrue(issubclass(
            self.helpers.RequestParseError, ValueError,
        ))


class OptionalIntTests(unittest.TestCase):
    def setUp(self):
        self.helpers = _load_request_helpers()

    def test_returns_default_for_missing_or_null(self):
        self.assertIsNone(self.helpers.optional_int({}, "x"))
        self.assertIsNone(self.helpers.optional_int({"x": None}, "x"))
        self.assertEqual(self.helpers.optional_int({}, "x", default=42), 42)

    def test_coercion_failure_still_raises(self):
        """Partial input (key present but garbage) is a hard error
        even on the optional path — silently falling back hides bugs."""
        with self.assertRaises(self.helpers.RequestParseError):
            self.helpers.optional_int({"x": "abc"}, "x")


if __name__ == "__main__":
    unittest.main()
