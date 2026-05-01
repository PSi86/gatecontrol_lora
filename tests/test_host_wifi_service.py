"""Unit tests for HostWifiService.connect_ap (dynamic nmcli path).

The service shells out to ``nmcli`` via :meth:`HostWifiService.nmcli_run`
which we monkey-patch so the tests never touch the host's actual radio
or network configuration. The assertions cover:

* SSID multi-match: scan returns multiple candidates, the service picks
  the first that's in the input list.
* The constructed ``nmcli`` argv carries ``password`` + ``ifname``
  (and optionally ``bssid``).
* disconnect_ap issues a ``nmcli con down`` — and **does not** issue a
  ``nmcli con delete`` (we keep the persistent profile for reuse).
* Empty SSID list and empty password raise.
* "Secrets were required" surfaces as a clear "wrong password" error.
"""

from __future__ import annotations

import subprocess
import unittest

from racelink.services.host_wifi_service import HostWifiService


def _ok(stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["nmcli"], returncode=0, stdout=stdout, stderr="")


def _err(stderr: str, returncode: int = 1) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["nmcli"], returncode=returncode, stdout="", stderr=stderr)


class _Recorder:
    """Stub for ``HostWifiService.nmcli_run`` that scripts deterministic
    responses by argv prefix and records every invocation.
    """

    def __init__(self):
        self.calls: list[list[str]] = []
        self._scripted: list[tuple[tuple[str, ...], subprocess.CompletedProcess]] = []
        self._default = _ok()

    def script(self, argv_prefix: tuple[str, ...], result: subprocess.CompletedProcess) -> None:
        self._scripted.append((argv_prefix, result))

    def __call__(self, args, timeout_s=20.0):
        self.calls.append(list(args))
        for prefix, result in self._scripted:
            if tuple(args[: len(prefix)]) == prefix:
                return result
        return self._default


class ConnectApTests(unittest.TestCase):
    def setUp(self):
        self.svc = HostWifiService()
        self.recorder = _Recorder()
        self.svc.nmcli_run = self.recorder  # type: ignore[method-assign]

    def _ready_iface(self, iface: str = "wlan0") -> None:
        self.recorder.script(
            ("-t", "-f", "DEVICE,TYPE,STATE", "dev", "status"),
            _ok(f"{iface}:wifi:connected\n"),
        )

    def test_picks_first_visible_ssid_from_candidate_list(self):
        # Scan returns the legacy SSID, candidate list has both new + old.
        # Service must connect to "WLED-AP" because that's what's
        # actually visible — the new name isn't broadcast in this case.
        self._ready_iface()
        self.recorder.script(
            ("-t", "-f", "SSID", "dev", "wifi", "list"),
            _ok("Other-AP\nWLED-AP\nlab-wifi\n"),
        )
        matched = self.svc.connect_ap(
            ["WLED_RaceLink_AP", "WLED-AP"],
            "wled1234",
            iface="wlan0",
        )
        self.assertEqual(matched, "WLED-AP")
        # Verify the connect argv: should pass the matched SSID, the
        # password, and the iface. ``bssid`` not asked for so absent.
        connect_calls = [c for c in self.recorder.calls if "connect" in c]
        self.assertEqual(len(connect_calls), 1)
        argv = connect_calls[0]
        self.assertIn("WLED-AP", argv)
        self.assertIn("password", argv)
        self.assertIn("wled1234", argv)
        self.assertIn("ifname", argv)
        self.assertIn("wlan0", argv)
        self.assertNotIn("bssid", argv)

    def test_prefers_first_listed_ssid_when_both_visible(self):
        # Both SSIDs visible — picks the first in the candidate list, not
        # the first in the scan output. New firmware is the default, so
        # operators with mixed fleets see the new name preferred.
        self._ready_iface()
        self.recorder.script(
            ("-t", "-f", "SSID", "dev", "wifi", "list"),
            _ok("WLED-AP\nWLED_RaceLink_AP\n"),
        )
        matched = self.svc.connect_ap(
            ["WLED_RaceLink_AP", "WLED-AP"],
            "wled1234",
            iface="wlan0",
        )
        self.assertEqual(matched, "WLED_RaceLink_AP")

    def test_passes_bssid_when_supplied(self):
        self._ready_iface()
        self.recorder.script(
            ("-t", "-f", "SSID", "dev", "wifi", "list"),
            _ok("WLED_RaceLink_AP\n"),
        )
        self.svc.connect_ap(
            ["WLED_RaceLink_AP"], "wled1234",
            iface="wlan0", bssid="aa:bb:cc:dd:ee:ff",
        )
        connect_argv = [c for c in self.recorder.calls if "connect" in c][0]
        self.assertIn("bssid", connect_argv)
        self.assertIn("aa:bb:cc:dd:ee:ff", connect_argv)

    def test_empty_ssid_list_raises(self):
        with self.assertRaises(RuntimeError) as cm:
            self.svc.connect_ap([], "wled1234")
        self.assertIn("no candidate SSIDs", str(cm.exception))

    def test_missing_password_raises(self):
        with self.assertRaises(RuntimeError) as cm:
            self.svc.connect_ap(["WLED_RaceLink_AP"], "")
        self.assertIn("password missing", str(cm.exception))

    def test_wrong_password_surfaces_clear_error(self):
        # nmcli's "Secrets were required, but not provided" is the actual
        # message you get on a bad PSK. The service translates it into
        # an operator-facing toast that names BOTH practical causes
        # (wrong password vs. ESP hostapd rate-limiting after recent
        # failed attempts) so the operator doesn't waste a race day
        # hunting a wrong-password bug when the real fix is a 30-s
        # wait. Pin both phrases so a future re-wording that loses
        # either signal fires here first.
        self._ready_iface()
        self.recorder.script(
            ("-t", "-f", "SSID", "dev", "wifi", "list"),
            _ok("WLED_RaceLink_AP\n"),
        )
        self.recorder.script(
            ("--wait",),  # the actual connect command
            _err("Error: Connection activation failed: Secrets were required, but not provided."),
        )
        with self.assertRaises(RuntimeError) as cm:
            self.svc.connect_ap(
                ["WLED_RaceLink_AP"], "wrong-password",
                iface="wlan0", timeout_s=2.0,
            )
        msg = str(cm.exception)
        self.assertIn("authentication failed", msg.lower())
        self.assertIn("password is wrong", msg.lower())
        self.assertIn("rate-limiting", msg.lower())
        self.assertIn("retry", msg.lower())

    def test_polkit_denial_points_at_setup_command(self):
        # rc=4 + "Insufficient privileges" is the exact string nmcli
        # prints when polkit blocks the user. The service must translate
        # this into an operator-readable hint that names the bundled
        # setup tool, otherwise the toast is just an opaque traceback.
        # Pin the hint helper so the assertion isn't tangled up in
        # platform-specific path resolution — the helper has its own
        # tests below.
        from unittest import mock
        from racelink.services import host_wifi_service as svc

        self._ready_iface()
        self.recorder.script(
            ("-t", "-f", "SSID", "dev", "wifi", "list"),
            _ok("WLED_RaceLink_AP\n"),
        )
        self.recorder.script(
            ("--wait",),  # the actual connect command
            _err(
                "Error: Failed to add/activate new connection: Insufficient privileges",
                returncode=4,
            ),
        )
        with mock.patch.object(
            svc, "_setup_command_hint",
            return_value="sudo /home/pi/.venv/bin/racelink-setup-nmcli",
        ):
            with self.assertRaises(RuntimeError) as cm:
                self.svc.connect_ap(
                    ["WLED_RaceLink_AP"], "wled1234",
                    iface="wlan0", timeout_s=2.0,
                )
        msg = str(cm.exception)
        # The actionable hint must be present; without it, operators
        # have to reverse-engineer the polkit policy from a stack trace.
        self.assertIn("racelink-setup-nmcli", msg)
        self.assertIn("polkit", msg.lower())
        # The hint must include the absolute venv path the helper
        # provided — ``sudo``'s default secure_path strips the venv's
        # bin/ so the bare console-script name would fail.
        self.assertIn("sudo /home/pi/.venv/bin/racelink-setup-nmcli", msg)
        # And the inner loop must NOT have retried — polkit denials are
        # deterministic, retrying would just delay the failure by the
        # whole timeout budget. Exactly one connect call should have
        # been made.
        connect_calls = [c for c in self.recorder.calls if "connect" in c]
        self.assertEqual(len(connect_calls), 1)


class SetupCommandHintTests(unittest.TestCase):
    """The hint helper drives the polkit-denied error message — needs
    its own coverage so a refactor that breaks the path resolution
    fires here, not in the operator's toast."""

    def test_returns_absolute_console_script_path_when_present(self):
        from racelink.services import host_wifi_service as svc
        from unittest import mock
        import os

        # Mock ``abspath`` to identity so the synthetic Linux-shaped
        # path we hand the helper isn't rewritten by Windows' drive
        # logic when this test runs on a non-Linux dev box. Mock
        # ``isfile`` to claim the script exists in that directory.
        fake_python = "/home/pi/.venv/bin/python"
        expected_script = "/home/pi/.venv/bin/racelink-setup-nmcli"
        with mock.patch.object(svc.sys, "executable", fake_python), \
             mock.patch.object(svc.os.path, "abspath", side_effect=lambda p: p), \
             mock.patch.object(svc.os.path, "join",
                               side_effect=lambda *parts: "/".join(parts)), \
             mock.patch.object(svc.os.path, "dirname",
                               side_effect=lambda p: p.rsplit("/", 1)[0]), \
             mock.patch.object(svc.os.path, "isfile",
                               lambda p: p == expected_script):
            hint = svc._setup_command_hint()
        self.assertIn(expected_script, hint)
        self.assertTrue(hint.startswith("sudo "))

    def test_falls_back_to_python_dash_m_when_script_missing(self):
        from racelink.services import host_wifi_service as svc
        from unittest import mock

        fake_python = "/home/pi/.venv/bin/python"
        with mock.patch.object(svc.sys, "executable", fake_python), \
             mock.patch.object(svc.os.path, "isfile", return_value=False):
            hint = svc._setup_command_hint()
        # The fallback hits the same install via the venv's Python so
        # the command still works even if the console script wasn't
        # installed (e.g. a partial ``pip install --no-deps`` build).
        self.assertIn("-m racelink.tools.setup_nmcli_polkit", hint)
        self.assertIn(fake_python, hint)


class PreDeleteProfileTests(unittest.TestCase):
    """Regression: NM's profile-reuse path occasionally fails with
    ``Error: 802-11-wireless-security.key-mgmt: property is missing.``
    when consecutive OTAs target different physical devices that share
    an SSID. The fix is to delete any existing profile before each
    connect so NM always creates a fresh one. End-state on disk is the
    same (one profile per SSID), but the in-flight connect is
    deterministic.
    """

    def setUp(self):
        self.svc = HostWifiService()
        self.recorder = _Recorder()
        self.svc.nmcli_run = self.recorder  # type: ignore[method-assign]

    def _ready_iface(self, iface: str = "wlan0") -> None:
        self.recorder.script(
            ("-t", "-f", "DEVICE,TYPE,STATE", "dev", "status"),
            _ok(f"{iface}:wifi:connected\n"),
        )

    def test_connect_deletes_existing_profile_before_connect(self):
        # Walk the recorded argv list and assert ``con delete id <SSID>``
        # appears strictly before ``dev wifi connect``. Without this
        # ordering the bug returns: stale profile makes connect fail
        # with the key-mgmt error, even though the password is correct.
        self._ready_iface()
        self.recorder.script(
            ("-t", "-f", "SSID", "dev", "wifi", "list"),
            _ok("WLED_RaceLink_AP\n"),
        )
        self.svc.connect_ap(
            ["WLED_RaceLink_AP"], "wled1234",
            iface="wlan0",
        )
        delete_idx = None
        connect_idx = None
        for i, argv in enumerate(self.recorder.calls):
            if argv[:4] == ["con", "delete", "id", "WLED_RaceLink_AP"]:
                delete_idx = i
            if "connect" in argv and "wifi" in argv:
                connect_idx = i
                break
        self.assertIsNotNone(delete_idx, "expected con delete id <SSID> before connect")
        self.assertIsNotNone(connect_idx, "expected dev wifi connect")
        self.assertLess(delete_idx, connect_idx)

    def test_unknown_connection_during_delete_is_tolerated(self):
        # First OTA on a fresh host: no profile exists yet. nmcli
        # reports ``Error: unknown connection 'X'.`` with a non-zero
        # return — the helper must not raise, otherwise the very first
        # OTA would always fail.
        self._ready_iface()
        self.recorder.script(
            ("-t", "-f", "SSID", "dev", "wifi", "list"),
            _ok("WLED_RaceLink_AP\n"),
        )
        self.recorder.script(
            ("con", "delete"),
            _err("Error: unknown connection 'WLED_RaceLink_AP'.", returncode=10),
        )
        # Should complete normally — connect uses the default _ok stub.
        matched = self.svc.connect_ap(
            ["WLED_RaceLink_AP"], "wled1234",
            iface="wlan0",
        )
        self.assertEqual(matched, "WLED_RaceLink_AP")

    def test_repeated_ota_against_same_ssid_succeeds(self):
        # End-to-end regression: simulate two consecutive OTAs (the
        # operator's actual reported scenario). First call creates the
        # profile, second call deletes it and recreates. Both must
        # return the matched SSID without raising.
        self._ready_iface()
        self.recorder.script(
            ("-t", "-f", "SSID", "dev", "wifi", "list"),
            _ok("WLED_RaceLink_AP\n"),
        )
        # First OTA: con delete returns "unknown connection" (no
        # profile yet); connect succeeds.
        # Second OTA: con delete succeeds (rc=0); connect succeeds.
        # The default _ok stub serves both.
        first = self.svc.connect_ap(
            ["WLED_RaceLink_AP"], "wled1234", iface="wlan0",
        )
        second = self.svc.connect_ap(
            ["WLED_RaceLink_AP"], "wled1234", iface="wlan0",
        )
        self.assertEqual(first, "WLED_RaceLink_AP")
        self.assertEqual(second, "WLED_RaceLink_AP")
        delete_calls = [c for c in self.recorder.calls if c[:2] == ["con", "delete"]]
        self.assertEqual(len(delete_calls), 2)


class OpenApFallbackTests(unittest.TestCase):
    """Regression: a fleet OTA against multiple WLED nodes failed with
    ``Error: 802-11-wireless-security.key-mgmt: property is missing.``
    on nodes whose AP password had been cleared (open AP). NM rejects
    a ``password <X>`` argument for a BSS that advertises no security,
    so the connect raises before HTTP is reached. The service now
    retries once without the password, transparently handling
    mixed-PSK/open fleets without operator intervention.

    The recorder's prefix-matching is too coarse to distinguish the
    PSK and open-AP attempts (both share ``--wait`` as prefix). These
    tests use a hand-rolled ``nmcli_run`` that inspects the full argv
    so PSK-call vs. open-call responses can be scripted independently.
    """

    def setUp(self):
        self.svc = HostWifiService()
        self.calls: list[list[str]] = []
        self.svc.nmcli_run = self._fake_nmcli  # type: ignore[method-assign]
        # Default scripted behaviour: every nmcli call returns ok with
        # empty stdout. Tests override the connect-call branch to
        # script the failure path under test.
        self._connect_with_psk_response = _ok()
        self._connect_open_response = _ok()

    def _fake_nmcli(self, args, timeout_s: float = 20.0) -> subprocess.CompletedProcess:
        self.calls.append(list(args))
        a = list(args)
        # Dispatch by argv shape so the harness can return different
        # responses for the PSK and open-AP connect attempts.
        if a[:5] == ["-t", "-f", "DEVICE,TYPE,STATE", "dev", "status"]:
            return _ok("wlan0:wifi:connected\n")
        if a[:4] == ["-t", "-f", "SSID", "dev"]:
            return _ok("WLED_RaceLink_AP\n")
        if "connect" in a and "wifi" in a:
            if "password" in a:
                return self._connect_with_psk_response
            return self._connect_open_response
        return _ok()

    def test_key_mgmt_error_falls_back_to_open_ap_connect(self):
        # Script: PSK connect rc=1 with key-mgmt error → service
        # retries without password → second call rc=0. connect_ap
        # returns the matched SSID without raising.
        self._connect_with_psk_response = _err(
            "Error: 802-11-wireless-security.key-mgmt: property is missing.",
            returncode=1,
        )
        self._connect_open_response = _ok()
        matched = self.svc.connect_ap(
            ["WLED_RaceLink_AP"], "wled1234",
            iface="wlan0", timeout_s=2.0,
        )
        self.assertEqual(matched, "WLED_RaceLink_AP")
        # Two connect attempts: first with password, second without.
        connect_calls = [c for c in self.calls if "connect" in c and "wifi" in c]
        self.assertEqual(len(connect_calls), 2)
        self.assertIn("password", connect_calls[0])
        self.assertNotIn("password", connect_calls[1])

    def test_open_fallback_re_deletes_partial_profile_before_retry(self):
        # The failed PSK attempt may have left a partial profile that
        # NM would reuse for the open retry. Verify the service issues
        # a second ``con delete`` between the failed PSK call and the
        # open retry so the retry creates fresh.
        self._connect_with_psk_response = _err(
            "Error: 802-11-wireless-security.key-mgmt: property is missing.",
            returncode=1,
        )
        self._connect_open_response = _ok()
        self.svc.connect_ap(
            ["WLED_RaceLink_AP"], "wled1234",
            iface="wlan0", timeout_s=2.0,
        )
        # Locate the indices of the two connect calls and the two
        # delete calls; assert delete_idx[1] sits between them.
        connect_idxs = [
            i for i, c in enumerate(self.calls) if "connect" in c and "wifi" in c
        ]
        delete_idxs = [
            i for i, c in enumerate(self.calls) if c[:2] == ["con", "delete"]
        ]
        self.assertEqual(len(connect_idxs), 2)
        self.assertGreaterEqual(len(delete_idxs), 2)
        # First delete is before the first connect (the standard
        # pre-delete); a second delete must sit between the first and
        # second connect (the fallback re-delete).
        self.assertLess(delete_idxs[0], connect_idxs[0])
        self.assertTrue(
            any(connect_idxs[0] < d < connect_idxs[1] for d in delete_idxs),
            f"expected re-delete between connects (delete idxs={delete_idxs}, "
            f"connect idxs={connect_idxs})",
        )

    def test_open_fallback_failure_surfaces_original_psk_error(self):
        # If the open retry also fails, the toast carries the ORIGINAL
        # PSK-mode error (more actionable: "AP looked open from NM's
        # view but our open-mode connect also failed" → genuine NM
        # state issue, not a real open AP). The open-mode error itself
        # would just be confusing noise.
        self._connect_with_psk_response = _err(
            "Error: 802-11-wireless-security.key-mgmt: property is missing.",
            returncode=1,
        )
        self._connect_open_response = _err(
            "Error: open-mode connect also failed for some other reason",
            returncode=2,
        )
        with self.assertRaises(RuntimeError) as cm:
            self.svc.connect_ap(
                ["WLED_RaceLink_AP"], "wled1234",
                iface="wlan0", timeout_s=2.0,
            )
        msg = str(cm.exception)
        # The original key-mgmt error is what the operator sees —
        # it's the actionable signal ("looks like an open AP, check
        # the device's AP-password setting") rather than the
        # secondary "open mode also failed" line.
        self.assertIn("key-mgmt", msg)
        self.assertNotIn("open-mode connect also failed", msg)


class DisconnectApTests(unittest.TestCase):
    def setUp(self):
        self.svc = HostWifiService()
        self.recorder = _Recorder()
        self.svc.nmcli_run = self.recorder  # type: ignore[method-assign]

    def test_issues_con_down_for_named_ssid(self):
        self.svc.disconnect_ap("WLED_RaceLink_AP")
        # Should call ``nmcli con down id WLED_RaceLink_AP`` and nothing else.
        self.assertEqual(len(self.recorder.calls), 1)
        argv = self.recorder.calls[0]
        self.assertIn("con", argv)
        self.assertIn("down", argv)
        self.assertIn("id", argv)
        self.assertIn("WLED_RaceLink_AP", argv)

    def test_does_not_delete_persistent_profile(self):
        # Plan invariant: NM creates one profile per SSID and we reuse
        # it. ``con delete`` would force the password prompt on the next
        # OTA — verify we never issue it from the cleanup path.
        self.svc.disconnect_ap("WLED_RaceLink_AP")
        for argv in self.recorder.calls:
            self.assertNotIn("delete", argv, f"unexpected delete in argv: {argv}")

    def test_empty_ssid_is_noop(self):
        self.svc.disconnect_ap("")
        self.assertEqual(self.recorder.calls, [])


if __name__ == "__main__":
    unittest.main()
