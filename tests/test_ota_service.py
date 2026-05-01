"""Unit tests for the WLED OTA upload paths.

The bug that prompted this file: ``wled_upload_firmware`` historically
delegated to ``wled_upload_file`` which posts to WLED's ``/upload``
endpoint — but ``/upload`` is the filesystem handler. Firmware
binaries posted there are silently saved as files rather than flashed.
The endpoint must be ``/update`` for OTA. These tests pin that
contract so a future refactor that reintroduces the wrong endpoint
fails loudly here, before the operator notices that flashes don't
actually flash.
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from unittest import mock


class _FakeResponse:
    """Stand-in for ``urllib.request.urlopen``'s context-manager
    response. Configurable status + body; returns the body bytes from
    ``read(...)`` so the failure-detection path can match on
    ``Update failed!``.
    """

    def __init__(self, status: int = 200, body: bytes = b""):
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, n=-1):
        if n is None or n < 0:
            return self._body
        out = self._body[:n]
        self._body = self._body[n:]
        return out


class _FakeHTTPError(Exception):
    """Mimics ``urllib.error.HTTPError`` — exposes ``.code`` and a
    ``read()`` returning the response body. Used for tests that
    exercise the body-capture path in ``wled_upload_file``'s outer
    ``except Exception``: real urllib raises HTTPError on 4xx/5xx,
    and our service code reads the body via ``ex.read()``.
    """

    def __init__(self, code: int, body: bytes = b"", msg: str = "HTTP error"):
        super().__init__(f"HTTP {code}: {msg}")
        self.code = code
        self._body = body

    def read(self) -> bytes:
        return self._body


def _make_service():
    from racelink.services.ota_service import OTAService

    class _StubPresets:
        @staticmethod
        def sha256_file(path):
            return "0" * 64

    return OTAService(host_wifi_service=None, presets_service=_StubPresets())


def _write_tmp(payload: bytes, suffix: str = ".bin") -> str:
    """Write a tempfile and return its path. Caller owns cleanup."""
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "wb") as fh:
        fh.write(payload)
    return path


class WledUploadEndpointRoutingTests(unittest.TestCase):
    """The WLED HTTP server has two separate handlers — ``/upload``
    (filesystem) and ``/update`` (firmware OTA). Firmware MUST go to
    ``/update``; the silent-success failure mode of posting a .bin to
    ``/upload`` was the regression these tests pin against."""

    def setUp(self):
        self.svc = _make_service()
        self._tmpfiles = []

    def tearDown(self):
        for p in self._tmpfiles:
            try:
                os.unlink(p)
            except OSError:
                pass

    def _capture_urlopen(self, response: _FakeResponse) -> mock.MagicMock:
        """Replace ``urllib.request.urlopen`` with a mock that records
        the request and returns ``response``. Returns the mock so tests
        can assert on the captured request."""
        from racelink.services import ota_service
        m = mock.MagicMock(return_value=response)
        return mock.patch.object(ota_service.urllib.request, "urlopen", m), m

    def test_wled_upload_firmware_targets_slash_update(self):
        path = _write_tmp(b"\x00" * 32, suffix=".bin")
        self._tmpfiles.append(path)
        # ``/update`` returns "Update successful!" on success; the body
        # is HTML but our snippet check just looks for the marker.
        ctx, urlopen = self._capture_urlopen(
            _FakeResponse(status=200, body=b"Update successful! Rebooting...")
        )
        with ctx:
            self.svc.wled_upload_firmware("http://4.3.2.1", path, timeout_s=5.0)
        self.assertEqual(urlopen.call_count, 1)
        request_arg = urlopen.call_args.args[0]
        # The URL must end with /update — the silent-no-op bug was that
        # firmware was being posted to /upload (filesystem handler).
        self.assertTrue(
            request_arg.full_url.endswith("/update"),
            f"firmware must POST to /update, got {request_arg.full_url}",
        )
        self.assertNotIn("/upload", request_arg.full_url)

    def test_wled_upload_file_default_targets_slash_upload(self):
        path = _write_tmp(b'{"ps":[]}', suffix=".json")
        self._tmpfiles.append(path)
        ctx, urlopen = self._capture_urlopen(
            _FakeResponse(status=200, body=b"OK")
        )
        with ctx:
            self.svc.wled_upload_file(
                "http://4.3.2.1", path,
                timeout_s=5.0, dest_name="presets.json",
            )
        request_arg = urlopen.call_args.args[0]
        # Filesystem files keep the /upload contract — operators rely
        # on this for presets.json / cfg.json.
        self.assertTrue(
            request_arg.full_url.endswith("/upload"),
            f"filesystem upload must POST to /upload, got {request_arg.full_url}",
        )

    def test_wled_upload_file_explicit_endpoint_override(self):
        path = _write_tmp(b"binary", suffix=".bin")
        self._tmpfiles.append(path)
        ctx, urlopen = self._capture_urlopen(
            _FakeResponse(status=200, body=b"Update successful!")
        )
        with ctx:
            self.svc.wled_upload_file(
                "http://4.3.2.1", path,
                timeout_s=5.0, endpoint="/update",
            )
        request_arg = urlopen.call_args.args[0]
        self.assertTrue(request_arg.full_url.endswith("/update"))


class WledUpdateFailureDetectionTests(unittest.TestCase):
    """WLED's ``/update`` handler may return HTTP 200 with an HTML body
    that contains ``Update failed!`` (depends on firmware build / point
    of failure). The status-code check alone misses this case — the
    body inspection in :meth:`OTAService._update_response_indicates_failure`
    catches it. Without this check, a flash failure looks like a
    successful OTA in the operator toast."""

    def setUp(self):
        self.svc = _make_service()
        self._tmpfiles = []

    def tearDown(self):
        for p in self._tmpfiles:
            try:
                os.unlink(p)
            except OSError:
                pass

    def test_http_200_with_update_failed_body_raises(self):
        from racelink.services import ota_service

        path = tempfile.mkstemp(suffix=".bin")[1]
        self._tmpfiles.append(path)
        with open(path, "wb") as fh:
            fh.write(b"\x00" * 32)

        # Realistic WLED failure HTML — the marker appears as part of a
        # ``serveMessage`` HTML wrapper.
        body = (
            b"<html><body><h1>Update failed!</h1>"
            b"<p>Bad firmware checksum.</p></body></html>"
        )
        m = mock.MagicMock(return_value=_FakeResponse(status=200, body=body))
        with mock.patch.object(ota_service.urllib.request, "urlopen", m):
            with self.assertRaises(RuntimeError) as cm:
                self.svc.wled_upload_firmware("http://4.3.2.1", path, timeout_s=5.0)
        self.assertIn("flash failure", str(cm.exception).lower())
        # The snippet should carry the actual reason WLED reported so
        # the operator can act on it without digging through logs.
        self.assertIn("checksum", str(cm.exception).lower())

    def test_update_successful_body_does_not_raise(self):
        from racelink.services import ota_service

        path = tempfile.mkstemp(suffix=".bin")[1]
        self._tmpfiles.append(path)
        with open(path, "wb") as fh:
            fh.write(b"\x00" * 32)

        body = b"<html><body>Update successful! Rebooting...</body></html>"
        m = mock.MagicMock(return_value=_FakeResponse(status=200, body=body))
        with mock.patch.object(ota_service.urllib.request, "urlopen", m):
            # Should not raise
            self.svc.wled_upload_firmware("http://4.3.2.1", path, timeout_s=5.0)

    def test_helper_recognises_update_failed_marker(self):
        # Direct check of the marker helper so refactors that move the
        # match logic still pin the contract.
        from racelink.services.ota_service import OTAService
        self.assertTrue(
            OTAService._update_response_indicates_failure(b"<p>Update failed!</p>")
        )
        self.assertFalse(
            OTAService._update_response_indicates_failure(b"<p>Update successful!</p>")
        )
        self.assertFalse(
            OTAService._update_response_indicates_failure(b"")
        )

    def test_helper_treats_successful_marker_as_authoritative(self):
        # Edge case: HTML that mentions both. WLED won't actually emit
        # this, but if a build did, "successful" wins (we err on the
        # side of NOT raising for an ambiguous body).
        from racelink.services.ota_service import OTAService
        body = b"<p>If the update fails, retry. Update successful!</p>"
        self.assertFalse(OTAService._update_response_indicates_failure(body))


class WledAutoUnlockOn401Tests(unittest.TestCase):
    """When ``/update`` returns 401, ``wled_upload_firmware`` POSTs
    ``/settings/sec`` with ``OP=<password>`` and retries once. The
    POST has two effects on WLED's settings handler:

    * ``OP=<password>`` clears ``otaLock`` if the password matches
      (set.cpp:651).
    * ``SU`` argument absent → ``otaSameSubnet=false`` unconditionally
      (set.cpp:669).

    Together they cover both gates that produce a 401 in practice
    (OTA-lock and same-subnet). These tests pin the success +
    failed-unlock + persistent-401 paths so the recovery sequence
    can't regress without firing here first.
    """

    def setUp(self):
        self.svc = _make_service()
        self._tmpfiles = []

    def tearDown(self):
        for p in self._tmpfiles:
            try:
                os.unlink(p)
            except OSError:
                pass

    def _fw_tmp(self) -> str:
        path = tempfile.mkstemp(suffix=".bin")[1]
        self._tmpfiles.append(path)
        with open(path, "wb") as fh:
            fh.write(b"\x00" * 64)
        return path

    def test_401_triggers_auto_unlock_then_retries(self):
        # First /update POST returns 401 → service POSTs /settings/sec
        # (returns 200) → retries /update (returns 200 with success body).
        # Total: 3 HTTP calls; caller sees no exception.
        from racelink.services import ota_service
        path = self._fw_tmp()
        responses = [
            _FakeResponse(status=401, body=b"locked"),
            _FakeResponse(status=200, body=b"OK"),
            _FakeResponse(status=200, body=b"Update successful!"),
        ]
        m = mock.MagicMock(side_effect=responses)
        with mock.patch.object(ota_service.urllib.request, "urlopen", m):
            self.svc.wled_upload_firmware("http://4.3.2.1", path, timeout_s=5.0)
        self.assertEqual(m.call_count, 3)
        # Second call must be the unlock POST to /settings/sec.
        unlock_req = m.call_args_list[1].args[0]
        self.assertTrue(unlock_req.full_url.endswith("/settings/sec"))
        self.assertEqual(unlock_req.method, "POST")
        # Body must carry OP=<password>; default is "wledota".
        body_bytes = unlock_req.data or b""
        self.assertIn(b"OP=wledota", body_bytes)
        # Third call is the /update retry.
        retry_req = m.call_args_list[2].args[0]
        self.assertTrue(retry_req.full_url.endswith("/update"))

    def test_401_with_failed_unlock_raises_with_diagnostic(self):
        # First /update 401; unlock POST itself fails (e.g. 500) →
        # service raises with a hint pointing at PIN/non-default
        # password as the likely remaining cause.
        from racelink.services import ota_service
        path = self._fw_tmp()
        responses = [
            _FakeResponse(status=401, body=b"locked"),
            _FakeResponse(status=500, body=b"oops"),
        ]
        m = mock.MagicMock(side_effect=responses)
        with mock.patch.object(ota_service.urllib.request, "urlopen", m):
            with self.assertRaises(RuntimeError) as cm:
                self.svc.wled_upload_firmware("http://4.3.2.1", path, timeout_s=5.0)
        msg = str(cm.exception)
        self.assertIn("HTTP 401", msg)
        self.assertIn("Auto-unlock POST to /settings/sec also", msg)
        self.assertIn("settingsPIN", msg)
        # Service should NOT have retried /update after a failed
        # unlock — that would just deterministically 401 again.
        self.assertEqual(m.call_count, 2)

    def test_401_persists_after_unlock_raises_with_pin_hint(self):
        # Unlock succeeds but retry /update still 401 → device
        # likely has settingsPIN set, or the otaPass differs from the
        # supplied default. The raise must name both possibilities so
        # the operator can act without digging through WLED docs.
        from racelink.services import ota_service
        path = self._fw_tmp()
        responses = [
            _FakeResponse(status=401, body=b"locked"),
            _FakeResponse(status=200, body=b"OK"),
            _FakeResponse(status=401, body=b"still locked"),
        ]
        m = mock.MagicMock(side_effect=responses)
        with mock.patch.object(ota_service.urllib.request, "urlopen", m):
            with self.assertRaises(RuntimeError) as cm:
                self.svc.wled_upload_firmware("http://4.3.2.1", path, timeout_s=5.0)
        msg = str(cm.exception)
        self.assertIn("still 401 after auto-unlock", msg)
        self.assertIn("settingsPIN", msg)
        self.assertIn("otaPass", msg)
        self.assertEqual(m.call_count, 3)

    def test_custom_ota_password_in_unlock_body(self):
        # Operator-supplied non-default OTA password reaches the
        # /settings/sec POST verbatim (no defaulting to "wledota").
        from racelink.services import ota_service
        path = self._fw_tmp()
        responses = [
            _FakeResponse(status=401, body=b"locked"),
            _FakeResponse(status=200, body=b"OK"),
            _FakeResponse(status=200, body=b"Update successful!"),
        ]
        m = mock.MagicMock(side_effect=responses)
        with mock.patch.object(ota_service.urllib.request, "urlopen", m):
            self.svc.wled_upload_firmware(
                "http://4.3.2.1", path,
                timeout_s=5.0, ota_password="custom-pass-42",
            )
        unlock_req = m.call_args_list[1].args[0]
        self.assertIn(b"OP=custom-pass-42", unlock_req.data or b"")

    def test_non_401_error_does_not_trigger_unlock(self):
        # 500 / 502 / network errors are NOT auto-recovered — they're
        # not the same-subnet/OTA-lock gate, and unlocking wouldn't
        # help. Verify the service raises directly without making the
        # unlock POST.
        from racelink.services import ota_service
        path = self._fw_tmp()
        m = mock.MagicMock(return_value=_FakeResponse(status=500, body=b""))
        with mock.patch.object(ota_service.urllib.request, "urlopen", m):
            with self.assertRaises(RuntimeError) as cm:
                self.svc.wled_upload_firmware("http://4.3.2.1", path, timeout_s=5.0)
        self.assertIn("HTTP 500", str(cm.exception))
        # Single call only: no unlock attempt, no retry.
        self.assertEqual(m.call_count, 1)


class WledUpdateBodyCaptureTests(unittest.TestCase):
    """When WLED rejects /update with a non-200 status, the response
    body carries the actual reason (release-name mismatch, chip
    variant, partition, OTA lock, …). The service must include the
    body snippet in the raised RuntimeError so the operator toast
    self-diagnoses without having to read WLED source.
    """

    def setUp(self):
        self.svc = _make_service()
        self._tmpfiles = []

    def tearDown(self):
        for p in self._tmpfiles:
            try:
                os.unlink(p)
            except OSError:
                pass

    def _fw_tmp(self) -> str:
        path = tempfile.mkstemp(suffix=".bin")[1]
        self._tmpfiles.append(path)
        with open(path, "wb") as fh:
            fh.write(b"\x00" * 64)
        return path

    def test_500_response_body_appears_in_raised_message(self):
        # Realistic WLED release-name mismatch body — produced by
        # serveMessage(request, 500, "Update failed!", reason, ...).
        body = (
            b"<html><body><h1>Update failed!</h1>"
            b"<p>Firmware release name mismatch: "
            b"current='ESP32S3', uploaded='RaceLink_Node_V4_TYPE_12'.</p>"
            b"</body></html>"
        )
        from racelink.services import ota_service
        path = self._fw_tmp()
        m = mock.MagicMock(side_effect=_FakeHTTPError(500, body=body))
        with mock.patch.object(ota_service.urllib.request, "urlopen", m):
            with self.assertRaises(RuntimeError) as cm:
                self.svc.wled_upload_firmware("http://4.3.2.1", path, timeout_s=5.0)
        msg = str(cm.exception)
        # Status code present + the WLED reason snippet present.
        self.assertIn("HTTP 500", msg)
        self.assertIn("release name mismatch", msg.lower())
        self.assertIn("RaceLink_Node_V4_TYPE_12", msg)

    def test_401_body_also_captured(self):
        # 401 path also reads the body so we surface "Client is not on
        # local subnet." vs "Please unlock OTA in security settings!"
        # explicitly. Note: this 401 specifically tests the case where
        # the auto-unlock path itself raises (the unlock POST also
        # 401'd) — the simpler "first 401 → unlock → retry" success
        # path is covered by WledAutoUnlockOn401Tests.
        from racelink.services import ota_service
        path = self._fw_tmp()
        # /update 401 → wled_upload_firmware tries unlock → unlock 401
        # too → final raise carries the original 401 message including
        # the body snippet from /update.
        m = mock.MagicMock(side_effect=[
            _FakeHTTPError(401, body=b"Please unlock OTA in security settings!"),
            _FakeHTTPError(401, body=b""),  # unlock POST also 401
        ])
        with mock.patch.object(ota_service.urllib.request, "urlopen", m):
            with self.assertRaises(RuntimeError) as cm:
                self.svc.wled_upload_firmware("http://4.3.2.1", path, timeout_s=5.0)
        msg = str(cm.exception)
        self.assertIn("HTTP 401", msg)
        self.assertIn("unlock OTA in security settings", msg)


class WledSkipValidationTests(unittest.TestCase):
    """The ``skipValidation=1`` multipart text part bypasses WLED's
    ``WLED_RELEASE_NAME`` check (ota_update.cpp:139-143). Default off
    — operators tick the dialog checkbox only for cross-fork
    migrations. Tests pin both the field-present and field-absent
    branches so a refactor that flips the default doesn't slip past.
    """

    def setUp(self):
        self.svc = _make_service()
        self._tmpfiles = []

    def tearDown(self):
        for p in self._tmpfiles:
            try:
                os.unlink(p)
            except OSError:
                pass

    def _fw_tmp(self) -> str:
        path = tempfile.mkstemp(suffix=".bin")[1]
        self._tmpfiles.append(path)
        with open(path, "wb") as fh:
            fh.write(b"\x00" * 64)
        return path

    def _captured_body(self, urlopen_mock) -> bytes:
        # urlopen receives a Request; .data is the multipart payload.
        request = urlopen_mock.call_args.args[0]
        return request.data or b""

    def test_skip_validation_true_adds_form_field(self):
        from racelink.services import ota_service
        path = self._fw_tmp()
        m = mock.MagicMock(return_value=_FakeResponse(
            status=200, body=b"Update successful! Rebooting..."))
        with mock.patch.object(ota_service.urllib.request, "urlopen", m):
            self.svc.wled_upload_firmware(
                "http://4.3.2.1", path, timeout_s=5.0,
                skip_validation=True,
            )
        body = self._captured_body(m)
        # Multipart text part must be present and carry the literal "1"
        # value that WLED's getParam("skipValidation") matches against.
        self.assertIn(b'name="skipValidation"', body)
        self.assertIn(b"\r\n\r\n1\r\n", body)

    def test_skip_validation_false_omits_form_field(self):
        # Default behaviour: no skipValidation part in the body, the
        # WLED safety check stays on. Pinned so a refactor that flips
        # the default fires here.
        from racelink.services import ota_service
        path = self._fw_tmp()
        m = mock.MagicMock(return_value=_FakeResponse(
            status=200, body=b"Update successful! Rebooting..."))
        with mock.patch.object(ota_service.urllib.request, "urlopen", m):
            self.svc.wled_upload_firmware(
                "http://4.3.2.1", path, timeout_s=5.0,
                # skip_validation defaults to False
            )
        body = self._captured_body(m)
        self.assertNotIn(b"skipValidation", body)

    def test_skip_validation_only_applies_to_update_endpoint(self):
        # /upload (filesystem files) doesn't have a skipValidation
        # gate — including the field would just confuse the WLED
        # filesystem handler. Belt-and-suspenders pin: even with
        # skip_validation=True, the field stays out of /upload bodies.
        from racelink.services import ota_service
        path = self._fw_tmp()
        m = mock.MagicMock(return_value=_FakeResponse(
            status=200, body=b"OK"))
        with mock.patch.object(ota_service.urllib.request, "urlopen", m):
            self.svc.wled_upload_file(
                "http://4.3.2.1", path,
                timeout_s=5.0, endpoint="/upload",
                skip_validation=True,  # ignored for /upload
            )
        body = self._captured_body(m)
        self.assertNotIn(b"skipValidation", body)


if __name__ == "__main__":
    unittest.main()
