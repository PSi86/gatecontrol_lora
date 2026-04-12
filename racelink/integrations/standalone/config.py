"""Standalone configuration helpers for RaceLink."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


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
        self.ensure_parent_dir()
        with open(self.path, "w", encoding="utf-8") as file_handle:
            json.dump(self.to_dict(), file_handle, indent=2, sort_keys=True)


class StandaloneOptionStore:
    """Simple persistent option storage compatible with the controller's DB usage."""

    def __init__(self, config: StandaloneConfig):
        self.config = config

    def option(self, key: str, default=None):
        return self.config.options.get(key, default)

    def option_set(self, key: str, value) -> None:
        self.config.options[key] = value
        self.config.save()
