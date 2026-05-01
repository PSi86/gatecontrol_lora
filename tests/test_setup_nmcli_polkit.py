"""Unit tests for the racelink-setup-nmcli console script.

These tests exercise the pure-Python helpers in
:mod:`racelink.tools.setup_nmcli_polkit` plus a couple of ``main()``
exit-code paths. Anything that would actually touch the host system —
``usermod``, ``systemctl reload polkit``, writing under ``/etc`` — is
mocked. The tests run on Windows + Linux + macOS without needing root.
"""

from __future__ import annotations

import unittest
from unittest import mock

from racelink.tools import setup_nmcli_polkit as setup


class ResolveTargetUserTests(unittest.TestCase):
    def test_argv_overrides_environment(self):
        with mock.patch.dict("os.environ", {"SUDO_USER": "from-env"}, clear=False):
            self.assertEqual(
                setup._resolve_target_user(["racelink-setup-nmcli", "from-cli"]),
                "from-cli",
            )

    def test_falls_back_to_sudo_user(self):
        with mock.patch.dict("os.environ", {"SUDO_USER": "pi"}, clear=False):
            self.assertEqual(
                setup._resolve_target_user(["racelink-setup-nmcli"]),
                "pi",
            )

    def test_raises_when_neither_present(self):
        env = {k: v for k, v in __import__("os").environ.items() if k != "SUDO_USER"}
        with mock.patch.dict("os.environ", env, clear=True):
            with self.assertRaises(RuntimeError):
                setup._resolve_target_user(["racelink-setup-nmcli"])

    def test_blank_argv_falls_through_to_sudo_user(self):
        with mock.patch.dict("os.environ", {"SUDO_USER": "pi"}, clear=False):
            self.assertEqual(
                setup._resolve_target_user(["racelink-setup-nmcli", "   "]),
                "pi",
            )


class PolkitRuleTextTests(unittest.TestCase):
    def test_rule_contains_target_user_and_namespace_match(self):
        rule = setup._polkit_rule_text("alice")
        # The rule must scope to the NetworkManager action namespace
        # AND to the named user; missing either turns it into either a
        # blanket grant or a no-op respectively.
        self.assertIn("org.freedesktop.NetworkManager.", rule)
        self.assertIn('subject.user === "alice"', rule)
        self.assertIn("polkit.Result.YES", rule)

    def test_rule_text_is_deterministic_for_same_user(self):
        # Re-running setup must produce a byte-identical file so the
        # idempotency claim holds (no churn on /etc/polkit-1/rules.d).
        self.assertEqual(
            setup._polkit_rule_text("pi"),
            setup._polkit_rule_text("pi"),
        )


class MainExitCodeTests(unittest.TestCase):
    def test_refuses_to_run_when_not_root(self):
        with mock.patch.object(setup.sys, "platform", "linux"), \
             mock.patch.object(setup.os, "geteuid", return_value=1000, create=True):
            rc = setup.main(["racelink-setup-nmcli"])
        self.assertEqual(rc, 2)

    def test_refuses_to_run_on_non_linux(self):
        with mock.patch.object(setup.sys, "platform", "darwin"):
            rc = setup.main(["racelink-setup-nmcli"])
        self.assertEqual(rc, 2)

    def test_fails_when_nmcli_not_on_path(self):
        with mock.patch.object(setup.sys, "platform", "linux"), \
             mock.patch.object(setup.os, "geteuid", return_value=0, create=True), \
             mock.patch.object(setup.shutil, "which", return_value=None):
            rc = setup.main(["racelink-setup-nmcli", "alice"])
        self.assertEqual(rc, 2)


class CheckNmcliHintTests(unittest.TestCase):
    def test_returns_none_when_nmcli_present(self):
        with mock.patch.object(setup.shutil, "which", return_value="/usr/bin/nmcli"):
            self.assertIsNone(setup._check_nmcli())

    def test_debian_hint_when_os_release_says_debian(self):
        os_release = 'ID=raspbian\nID_LIKE="debian"\n'
        with mock.patch.object(setup.shutil, "which", return_value=None), \
             mock.patch("builtins.open", mock.mock_open(read_data=os_release)):
            hint = setup._check_nmcli()
        self.assertIsNotNone(hint)
        self.assertIn("apt", hint)


if __name__ == "__main__":
    unittest.main()
