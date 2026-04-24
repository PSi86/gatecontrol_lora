"""Standalone configuration helpers for RaceLink."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _default_config_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".racelink", "standalone_config.json")


@dataclass
class StandaloneConfig:
    """Local configuration for standalone RaceLink runtime."""

    path: str = field(default_factory=_default_config_path)
    host: str = "127.0.0.1"
    port: int = 5077
    debug: bool = False
    options: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Optional[str] = None) -> "StandaloneConfig":
        cfg_path = path or _default_config_path()
        if not os.path.exists(cfg_path):
            return cls(path=cfg_path)
        with open(cfg_path, "r", encoding="utf-8") as file_handle:
            raw = json.load(file_handle) or {}
        return cls(
            path=cfg_path,
            host=str(raw.get("host", "127.0.0.1")),
            port=int(raw.get("port", 5077) or 5077),
            debug=bool(raw.get("debug", False)),
            options=dict(raw.get("options", {}) or {}),
        )

    def ensure_parent_dir(self) -> None:
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "host": self.host,
            "port": int(self.port),
            "debug": bool(self.debug),
            "options": dict(self.options or {}),
        }

    def save(self) -> None:
        """Persist the config atomically via temp-file + rename."""
        self.ensure_parent_dir()
        parent = os.path.dirname(self.path) or "."
        fd, tmp_path = tempfile.mkstemp(prefix=".standalone_config-", suffix=".tmp", dir=parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file_handle:
                json.dump(self.to_dict(), file_handle, indent=2, sort_keys=True)
                file_handle.flush()
                try:
                    os.fsync(file_handle.fileno())
                except OSError:
                    # fsync may be unsupported on some filesystems; the rename
                    # below is still the durability-critical step.
                    pass
            os.replace(tmp_path, self.path)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass


class StandaloneOptionStore:
    """Persistent option storage compatible with the controller's DB usage.

    Writes are debounced (plan P3-4): calling ``option_set`` repeatedly from a
    discovery flow no longer triggers N disk writes. A single save fires after
    ``debounce_seconds`` of idleness. Call :meth:`flush` on shutdown to
    guarantee the last update hits disk.
    """

    def __init__(self, config: StandaloneConfig, *, debounce_seconds: float = 0.25):
        self.config = config
        self._debounce_seconds = float(debounce_seconds)
        self._lock = threading.Lock()
        self._dirty = False
        self._timer: threading.Timer | None = None

    def option(self, key: str, default=None):
        return self.config.options.get(key, default)

    def option_set(self, key: str, value) -> None:
        with self._lock:
            self.config.options[key] = value
            self._dirty = True
            if self._debounce_seconds <= 0:
                self._flush_locked()
                return
            self._schedule_locked()

    def flush(self) -> None:
        """Persist any pending changes immediately."""
        with self._lock:
            self._flush_locked()

    # -- internal helpers ------------------------------------------------

    def _schedule_locked(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(self._debounce_seconds, self._on_timer)
        self._timer.daemon = True
        self._timer.start()

    def _on_timer(self) -> None:
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if not self._dirty:
            return
        try:
            self.config.save()
            self._dirty = False
        except Exception:
            logger.exception("RaceLink: standalone config save failed")
