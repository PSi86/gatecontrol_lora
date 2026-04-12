"""OTA and host-side file transfer operations for RaceLink."""

from __future__ import annotations

import json
import mimetypes
import os
import re
import tempfile
import time
import urllib.request
import uuid
from typing import Optional


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

    def wled_upload_file(self, base_url: str, path: str, timeout_s: float = 45.0, dest_name: Optional[str] = None) -> None:
        boundary = uuid.uuid4().hex
        filename = dest_name or os.path.basename(path)
        ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        with open(path, "rb") as file_handle:
            payload = file_handle.read()
        body = [
            f"--{boundary}\r\n".encode("utf-8"),
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode("utf-8"),
            f"Content-Type: {ctype}\r\n\r\n".encode("utf-8"),
            payload,
            f"\r\n--{boundary}--\r\n".encode("utf-8"),
        ]
        req = urllib.request.Request(
            f"{base_url}/upload",
            data=b"".join(body),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as response:
                status = getattr(response, "status", 200)
                if int(status) >= 400:
                    raise RuntimeError(f"HTTP {status} from /upload")
        except Exception as ex:
            status = getattr(ex, "code", None)
            if status == 401:
                raise RuntimeError("HTTP 401 from /upload (Unauthorized). Likely WLED OTA lock is enabled. Disable OTA lock in WLED.")
            if status:
                raise RuntimeError(f"HTTP {status} from /upload")
            raise

    def wled_upload_firmware(self, base_url: str, path: str, timeout_s: float = 30.0) -> None:
        self.wled_upload_file(base_url, path, timeout_s=timeout_s)

    def wled_download_presets(self, base_url: str, timeout_s: float = 10.0) -> bytes:
        payload = self.http_get_bytes(f"{base_url}/presets.json", timeout_s=timeout_s)
        if not payload:
            raise RuntimeError("Empty presets.json response")
        try:
            json.loads(payload.decode("utf-8", errors="replace") or "{}")
        except Exception as ex:
            raise RuntimeError(f"Invalid presets.json payload: {ex}")
        return payload
