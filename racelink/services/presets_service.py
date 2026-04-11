"""Preset file management for RaceLink operations."""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union


class PresetsService:
    """Manage presets.json files independently of Flask routes."""

    def __init__(self, *, option_getter=None, option_setter=None, apply_options=None):
        self._option_getter = option_getter
        self._option_setter = option_setter
        self._apply_options = apply_options

    def presets_dir(self) -> str:
        directory = os.path.join(os.path.expanduser("~"), ".racelink_lora", "presets")
        os.makedirs(directory, exist_ok=True)
        return directory

    def preset_filename(self, ts: Optional[float] = None) -> str:
        dt = datetime.fromtimestamp(ts or time.time())
        return dt.strftime("presets_%Y%m%d_%H%M%S.json")

    def preset_path_for_name(self, name: str) -> Optional[str]:
        base = os.path.basename(name or "")
        if not base:
            return None
        path = os.path.join(self.presets_dir(), base)
        if not os.path.isfile(path):
            return None
        return path

    def sha256_file(self, path: str) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as file_handle:
            for chunk in iter(lambda: file_handle.read(1024 * 256), b""):
                h.update(chunk)
        return h.hexdigest()

    def file_info(self, path: str, name: Optional[str] = None) -> dict:
        filename = name or os.path.basename(path)
        stat = os.stat(path)
        return {
            "name": filename,
            "size": int(stat.st_size),
            "sha256": self.sha256_file(path),
            "saved_ts": float(stat.st_mtime),
            "path": path,
        }

    def list_files(self) -> List[dict]:
        rows: List[dict] = []
        try:
            for name in os.listdir(self.presets_dir()):
                if name.startswith(".") or not name.endswith(".json"):
                    continue
                path = os.path.join(self.presets_dir(), name)
                if not os.path.isfile(path):
                    continue
                stat = os.stat(path)
                rows.append({"name": name, "size": int(stat.st_size), "saved_ts": float(stat.st_mtime)})
        except Exception:
            return []
        rows.sort(key=lambda row: row.get("saved_ts", 0), reverse=True)
        return rows

    def get_current_name(self) -> str:
        try:
            if self._option_getter:
                return str(self._option_getter("rl_wled_presets_file", "") or "")
        except Exception:
            pass
        return ""

    def set_current_name(self, name: str) -> None:
        try:
            if self._option_setter:
                self._option_setter("rl_wled_presets_file", name or "")
        except Exception:
            pass

    def parse_wled_presets_minimal(self, presets: Union[str, bytes, Dict[str, Any]]) -> List[Tuple[int, str]]:
        if isinstance(presets, (str, bytes)):
            data = json.loads(presets)
        elif isinstance(presets, dict):
            data = presets
        else:
            raise TypeError("presets must be dict, str, or bytes")

        out: List[Tuple[int, str]] = []
        for key, preset_obj in data.items():
            try:
                preset_id = int(key)
            except (TypeError, ValueError):
                continue
            if preset_id == 0 or not isinstance(preset_obj, dict):
                continue
            raw_name = preset_obj.get("n", "")
            name = raw_name.strip() if isinstance(raw_name, str) else ""
            if not name:
                name = f"Preset ID {preset_id}"
            out.append((preset_id, name))
        out.sort(key=lambda item: item[0])
        return out

    def apply_options(self, parsed: List[Tuple[int, str]]) -> None:
        if self._apply_options:
            self._apply_options(parsed)

    def apply_from_path(self, path: str) -> bool:
        try:
            with open(path, "rb") as file_handle:
                payload = file_handle.read()
            parsed = self.parse_wled_presets_minimal(payload or b"{}")
            self.apply_options(parsed)
            return True
        except Exception:
            return False

    def ensure_loaded(self) -> None:
        files = self.list_files()
        if not files:
            self.apply_options([])
            self.set_current_name("")
            return
        current = self.get_current_name()
        if current:
            current_path = self.preset_path_for_name(current)
            if current_path and self.apply_from_path(current_path):
                return
        current = files[0]["name"]
        path = self.preset_path_for_name(current)
        if path and self.apply_from_path(path):
            self.set_current_name(current)
            return
        self.apply_options([])

    def store_uploaded_file(self, file_obj) -> dict:
        if not file_obj or not getattr(file_obj, "filename", ""):
            raise ValueError("missing file")
        ts = time.time()
        name = self.preset_filename(ts)
        dst = os.path.join(self.presets_dir(), name)
        if os.path.exists(dst):
            for idx in range(1, 100):
                alt = name.replace(".json", f"_{idx}.json")
                dst = os.path.join(self.presets_dir(), alt)
                if not os.path.exists(dst):
                    name = alt
                    break
        file_obj.save(dst)
        return {"name": name, "size": int(os.path.getsize(dst)), "saved_ts": ts, "path": dst}

    def save_payload(self, payload: bytes) -> dict:
        ts = time.time()
        name = self.preset_filename(ts)
        dst = os.path.join(self.presets_dir(), name)
        if os.path.exists(dst):
            for idx in range(1, 100):
                alt = name.replace(".json", f"_{idx}.json")
                dst = os.path.join(self.presets_dir(), alt)
                if not os.path.exists(dst):
                    name = alt
                    break
        with open(dst, "wb") as file_handle:
            file_handle.write(payload)
        os.utime(dst, (ts, ts))
        return {"name": name, "size": int(os.path.getsize(dst)), "saved_ts": ts, "path": dst}
