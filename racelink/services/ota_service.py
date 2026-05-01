"""File-staging + WLED HTTP transfer for the OTA workflow.

Host-side helpers for the multi-step OTA process orchestrated by
:class:`OTAWorkflowService`. This module only knows about *files*
(staging uploads on the host, talking HTTP to a node's WLED
endpoint); the workflow ordering / WiFi orchestration lives in
the workflow service.

Public API (selected):

* ``store_upload(file_obj, kind)`` — accept a file from the
  WebUI form, validate kind ∈ {fw, presets, cfg}, hash it,
  return a stable id.
* ``get_upload(id, expect_kind=None)`` — look up a previously-
  stored upload by id; verify kind if specified.
* ``wled_upload_firmware(base_url, fw_path, timeout_s)`` /
  ``wled_download_presets(base_url, timeout_s)`` — talk to the
  node's WLED ``/update`` and ``/presets.json`` endpoints.
* ``wait_for_expected_node(base_url, expected_mac, timeout_s,
  poll_s)`` — poll ``/json/info`` until the reported MAC
  matches expectations (operator pre-staged the right node).

Threading: file I/O + blocking HTTP. Run from worker threads.
The WLED-side operations check for HTTP 401 (OTA-lock enabled)
and surface a clear operator message rather than a generic
HTTP error.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
from typing import Optional

logger = logging.getLogger(__name__)


class OTAService:
    """Host-side OTA and upload orchestration independent of Flask routes."""

    def __init__(self, *, host_wifi_service, presets_service):
        self.host_wifi = host_wifi_service
        self.presets = presets_service
        self._uploads = {}

    def uploads_dir(self) -> str:
        directory = os.path.join(tempfile.gettempdir(), "racelink_uploads")
        os.makedirs(directory, exist_ok=True)
        return directory

    def sha256_file(self, path: str) -> str:
        return self.presets.sha256_file(path)

    def store_upload(self, file_storage, kind: str) -> dict:
        if not file_storage or not getattr(file_storage, "filename", ""):
            raise ValueError("missing file")
        kind = (kind or "").strip().lower()
        if kind not in ("firmware", "presets", "cfg"):
            raise ValueError("invalid kind")
        filename = os.path.basename(file_storage.filename) or (f"{kind}.bin" if kind == "firmware" else f"{kind}.json")
        upload_id = uuid.uuid4().hex[:12]
        dst = os.path.join(self.uploads_dir(), f"{upload_id}__{filename}")
        file_storage.save(dst)
        info = {
            "id": upload_id,
            "kind": kind,
            "path": dst,
            "name": filename,
            "size": int(os.path.getsize(dst)),
            "sha256": self.sha256_file(dst),
            "uploaded_ts": time.time(),
        }
        self._uploads[upload_id] = info
        return info

    def get_upload(self, upload_id: str, expect_kind: Optional[str] = None) -> Optional[dict]:
        if not upload_id:
            return None
        info = self._uploads.get(upload_id)
        if not info:
            return None
        if expect_kind and info.get("kind") != expect_kind:
            return None
        if not os.path.exists(info.get("path", "")):
            return None
        return info

    def list_uploads(self):
        rows = [
            {k: value.get(k) for k in ("id", "kind", "name", "size", "sha256", "uploaded_ts")}
            for value in self._uploads.values()
            if value and os.path.exists(value.get("path", ""))
        ]
        rows.sort(key=lambda row: row.get("uploaded_ts", 0), reverse=True)
        return rows

    def norm_hex(self, value: str) -> str:
        return re.sub(r"[^0-9a-fA-F]", "", value or "").lower()

    def expected_mac_hex(self, addr: str) -> str:
        hx = self.norm_hex(addr)
        if len(hx) < 12:
            return ""
        return hx[-12:]

    def expected_last3_hex(self, addr: str) -> str:
        hx = self.norm_hex(addr)
        if len(hx) < 6:
            return ""
        return hx[-6:]

    def recv3_bytes_from_addr(self, addr: str) -> bytes:
        last3 = self.expected_last3_hex(addr)
        if len(last3) != 6:
            raise ValueError("invalid addr")
        return bytes.fromhex(last3)

    def lookup_group_id_for_addr(self, addr: str, devices) -> int:
        want = self.expected_mac_hex(addr)
        if not want:
            return 0
        try:
            for dev in devices:
                have = self.expected_mac_hex(str(getattr(dev, "addr", "") or ""))
                if have and have.lower() == want.lower():
                    return int(getattr(dev, "groupId", 0) or 0)
        except Exception:
            # swallow-ok: best-effort fallback; caller proceeds with safe default
            pass
        return 0

    def wled_base_url(self, raw_url: str) -> str:
        value = str(raw_url or "").strip() or "http://4.3.2.1"
        return value.rstrip("/")

    def http_get_bytes(self, url: str, timeout_s: float = 8.0) -> bytes:
        with urllib.request.urlopen(url, timeout=timeout_s) as response:
            return response.read()

    def http_post_json(self, url: str, payload: dict, timeout_s: float = 8.0) -> bytes:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as response:
            return response.read()

    def fetch_wled_info(self, base_url: str, timeout_s: float = 5.0):
        try:
            payload = self.http_get_bytes(f"{base_url}/json/info", timeout_s=timeout_s)
            return json.loads(payload.decode("utf-8", errors="replace") or "{}")
        except Exception:
            # swallow-ok: best-effort fallback; caller proceeds with safe default
            return None

    def wait_for_expected_node(self, base_url: str, expected_mac: str, timeout_s: float = 90.0, poll_s: float = 1.0):
        deadline = time.time() + max(2.0, float(timeout_s))
        expected = (expected_mac or "").lower()
        while time.time() < deadline:
            info = self.fetch_wled_info(base_url, timeout_s=min(8.0, poll_s + 2.0))
            if info:
                mac = self.norm_hex(str(info.get("mac") or "")).lower()
                if expected and mac == expected:
                    return info
            time.sleep(max(0.2, float(poll_s)))
        return None

    def wled_upload_file(
        self,
        base_url: str,
        path: str,
        timeout_s: float = 45.0,
        dest_name: Optional[str] = None,
        *,
        endpoint: str = "/upload",
        skip_validation: bool = False,
    ) -> None:
        """Multipart POST a file to a WLED node.

        ``endpoint`` selects the WLED handler — they are NOT
        interchangeable:

        * ``/upload`` (default) — filesystem files. Saves the upload
          into WLED's SPIFFS so things like ``presets.json`` and
          ``cfg.json`` land where WLED reads them at boot.
        * ``/update`` — firmware OTA. Flashes the device and reboots.
          **Use this for firmware binaries.** Posting a ``.bin`` to
          ``/upload`` silently saves it as a regular file rather than
          flashing it — that was the silent-success failure mode this
          parameter exists to prevent.

        See ``wled00/wled_server.cpp`` in the WLED source for the
        exact handler split (``server.on("/upload", …)`` →
        ``handleUpload``; ``server.on("/update", …)`` →
        ``handleOTAData``).

        ``skip_validation`` (only meaningful for ``endpoint='/update'``)
        adds a multipart text part ``skipValidation=1`` that WLED's OTA
        handler reads at ``ota_update.cpp:139-143`` to bypass the
        compile-time release-name check. Use it for cross-fork
        migrations (stock WLED → RaceLink fork) where the binary's
        ``WLED_RELEASE_NAME`` deliberately differs from the running
        firmware's. Default ``False`` keeps the safety check on.
        """
        boundary = uuid.uuid4().hex
        filename = dest_name or os.path.basename(path)
        ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        with open(path, "rb") as file_handle:
            payload = file_handle.read()
        body: list = []
        # Text fields first (the conventional multipart layout — WLED
        # processes parts as it streams them, so the ``skipValidation``
        # flag must arrive before ``handleOTAData`` starts the flash).
        if skip_validation and endpoint == "/update":
            body.extend([
                f"--{boundary}\r\n".encode("utf-8"),
                b'Content-Disposition: form-data; name="skipValidation"\r\n\r\n',
                b"1",
                b"\r\n",
            ])
        body.extend([
            f"--{boundary}\r\n".encode("utf-8"),
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode("utf-8"),
            f"Content-Type: {ctype}\r\n\r\n".encode("utf-8"),
            payload,
            f"\r\n--{boundary}--\r\n".encode("utf-8"),
        ])
        url = f"{base_url}{endpoint}"
        logger.info(
            "WLED upload start: %s -> %s (%d bytes, kind=%s)",
            filename, url, len(payload),
            "firmware" if endpoint == "/update" else "filesystem",
        )
        req = urllib.request.Request(
            url,
            data=b"".join(body),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as response:
                status = getattr(response, "status", 200)
                # /update reports flash failure inside the HTTP body
                # (HTML-formatted "Update failed!" page) — sometimes
                # with HTTP 500, sometimes with 200 depending on
                # WLED firmware build. Read up to 4 KB so we catch the
                # marker either way.
                body_bytes = b""
                if endpoint == "/update":
                    try:
                        body_bytes = response.read(4096)
                    except Exception:
                        # swallow-ok: best-effort body read; failure to
                        # read just means we fall back to status-code
                        # interpretation below.
                        body_bytes = b""
                if int(status) >= 400:
                    raise RuntimeError(
                        f"HTTP {status} from {endpoint}"
                        + (f": {self._snippet(body_bytes)}" if body_bytes else "")
                    )
                if endpoint == "/update" and self._update_response_indicates_failure(body_bytes):
                    raise RuntimeError(
                        "WLED reported flash failure on /update "
                        f"(HTTP {status} body: {self._snippet(body_bytes)}). "
                        "Common causes: wrong firmware binary for this "
                        "ESP variant, OTA lock / PIN, or insufficient "
                        "free flash."
                    )
                logger.info(
                    "WLED upload done: %s status=%s",
                    url, status,
                )
        except Exception as ex:
            status = getattr(ex, "code", None)
            # urllib's HTTPError exposes ``read()`` for the response
            # body. WLED's ``serveMessage(..., 500, "Update failed!",
            # reason, ...)`` puts the actual flash-failure reason
            # ("Firmware release name mismatch: current='X',
            # uploaded='Y'.", "Bad firmware checksum.", chip variant
            # mismatch, ...) in the body. Surfacing it turns an opaque
            # ``HTTP 500 from /update`` into a self-diagnosing message
            # the operator can act on without reading WLED source.
            err_body = b""
            if hasattr(ex, "read"):
                try:
                    err_body = ex.read() or b""
                except Exception:
                    # swallow-ok: best-effort body read; status alone
                    # is still informative.
                    err_body = b""
            err_snippet = self._snippet(err_body) if err_body else ""
            if status == 401:
                raise RuntimeError(
                    f"HTTP 401 from {endpoint} (Unauthorized). "
                    "WLED OTA lock or `Same network` PIN gate is enabled — "
                    "disable both for the OTA window."
                    + (f" Body: {err_snippet}" if err_snippet else "")
                )
            if status:
                raise RuntimeError(
                    f"HTTP {status} from {endpoint}"
                    + (f": {err_snippet}" if err_snippet else "")
                )
            raise

    @staticmethod
    def _snippet(body_bytes: bytes, *, limit: int = 200) -> str:
        """Decode a short, single-line snippet of the response body for
        error messages — strips the HTML tags / extra whitespace WLED's
        ``serveMessage`` wraps the result in so the operator-facing
        toast stays readable."""
        text = body_bytes.decode("utf-8", errors="replace")
        # Cheap tag strip: WLED's serveMessage emits a tiny HTML page;
        # we only want the human-readable bits for the toast.
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:limit]

    @staticmethod
    def _update_response_indicates_failure(body_bytes: bytes) -> bool:
        """Detect WLED's ``Update failed!`` response. WLED returns this
        on bad-firmware / wrong-chip / OTA-lock / PIN / size mismatches;
        depending on the build it may come back as HTTP 200 or 500. We
        match on the literal status text so a raw HTTP-status check
        alone (which our caller already does for >=400) doesn't miss
        the 200-with-failure-page case.
        """
        if not body_bytes:
            return False
        text = body_bytes.decode("utf-8", errors="replace").lower()
        if "update successful" in text:
            return False
        return "update failed" in text

    def wled_upload_firmware(
        self,
        base_url: str,
        path: str,
        timeout_s: float = 60.0,
        ota_password: str = "wledota",
        skip_validation: bool = False,
    ) -> None:
        """POST a firmware binary to WLED's ``/update`` endpoint.

        ``timeout_s`` defaults to 60 s — a real flash + reboot cycle on
        an ESP32 is 5-30 s of write time plus the reboot, so the legacy
        30 s default occasionally tripped a false timeout right at the
        end of a successful flash. The retry loop in the OTA workflow
        wraps this so a genuinely stuck device still bails out
        promptly via the outer attempt budget.

        **Auto-unlock on HTTP 401**: if the first POST returns 401, we
        POST ``/settings/sec`` with ``OP=<ota_password>`` and retry
        ``/update`` once. WLED's settings handler clears ``otaLock``
        when the password matches AND, as a side effect of the form
        shape (``SU`` argument absent), persistently flips
        ``otaSameSubnet=false`` on the device — the same-subnet gate
        that's the actual blocker for AP+STA-mode fleets where the
        device's ``Network.localIP()`` returns a non-AP address.
        See ``docs/DEVELOPER_GUIDE.md`` "WLED OTA gate matrix" for
        the full picture and the line-pinned references.

        **Do not** route this through :meth:`wled_upload_file` with the
        default endpoint — ``/upload`` is for filesystem files and a
        ``.bin`` posted there is silently saved instead of flashed
        (the bug that prompted this signature split).

        ``skip_validation`` is forwarded into the multipart body as
        ``skipValidation=1``; see :meth:`wled_upload_file` for the gate
        it bypasses (cross-fork ``WLED_RELEASE_NAME`` mismatch).
        """
        try:
            self.wled_upload_file(
                base_url, path,
                timeout_s=timeout_s, endpoint="/update",
                skip_validation=skip_validation,
            )
            return
        except RuntimeError as ex:
            if "HTTP 401" not in str(ex):
                raise
            # /update returned 401 — try the auto-unlock route.
            logger.info(
                "WLED rejected /update with 401; attempting auto-unlock "
                "via POST /settings/sec",
            )
            unlocked = self._wled_attempt_unlock(base_url, ota_password)
            if not unlocked:
                # Unlock POST itself failed — the device may have a
                # settingsPIN that blocks the security page, or the
                # AP-side host has lost connectivity. The original 401
                # is still the most useful signal.
                raise RuntimeError(
                    f"{ex} Auto-unlock POST to /settings/sec also "
                    "failed; check whether settingsPIN is set on the "
                    "device or otaPass differs from the supplied "
                    f"value (default '{ota_password}')."
                )
        # Single retry after unlock.
        try:
            self.wled_upload_file(
                base_url, path,
                timeout_s=timeout_s, endpoint="/update",
                skip_validation=skip_validation,
            )
        except RuntimeError as retry_ex:
            if "HTTP 401" in str(retry_ex):
                raise RuntimeError(
                    f"{retry_ex} /update still 401 after auto-unlock — "
                    "device likely has settingsPIN set, otaPass differs "
                    f"from the supplied value (default '{ota_password}'), "
                    "or the auto-unlock POST didn't actually clear the "
                    "gate. Check the device's WLED Security settings."
                )
            raise

    def _wled_attempt_unlock(self, base_url: str, password: str, *, timeout_s: float = 8.0) -> bool:
        """POST ``/settings/sec`` with ``OP=<password>`` to clear the
        WLED OTA-lock gate AND (via the absent ``SU`` argument) flip
        ``otaSameSubnet=false`` runtime + persistently into the
        device's ``cfg.json``.

        WLED's handler at ``set.cpp:651`` (``otaLock`` clear) and
        ``set.cpp:669`` (``otaSameSubnet = request->hasArg("SU")``) do
        the work; the POST is gated by ``inLocalSubnet`` at
        ``wled_server.cpp:773`` which accepts the AP-side host's
        ``4.3.2.x`` lease whenever the device's AP is active.

        Returns ``True`` on HTTP < 400, ``False`` otherwise. Never
        raises — the caller decides whether the failure is fatal.
        """
        body = urllib.parse.urlencode({"OP": password}).encode("utf-8")
        url = f"{base_url}/settings/sec"
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as response:
                status = int(getattr(response, "status", 200) or 200)
                ok = status < 400
                logger.info(
                    "WLED auto-unlock %s POST %s status=%s",
                    "OK" if ok else "FAIL", url, status,
                )
                return ok
        except Exception as ex:
            # urllib raises HTTPError for >=400 status codes by default.
            # Treat any non-200 as failure but capture the status when
            # available so the operator log shows the real reason.
            status = getattr(ex, "code", None)
            logger.info(
                "WLED auto-unlock FAIL POST %s status=%s err=%s",
                url, status, ex,
            )
            return False

    def wled_download_presets(self, base_url: str, timeout_s: float = 10.0) -> bytes:
        payload = self.http_get_bytes(f"{base_url}/presets.json", timeout_s=timeout_s)
        if not payload:
            raise RuntimeError("Empty presets.json response")
        try:
            json.loads(payload.decode("utf-8", errors="replace") or "{}")
        except Exception as ex:
            raise RuntimeError(f"Invalid presets.json payload: {ex}")
        return payload
