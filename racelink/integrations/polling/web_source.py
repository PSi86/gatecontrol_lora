"""Polling-based web source scaffold for future standalone integrations."""

from __future__ import annotations

from typing import Any, Dict, Optional

from ...core import EventSource


class PollingWebSource(EventSource):
    """Prepared polling source that can later pull data from web resources."""

    source_name = "polling-web"

    def __init__(self, *, base_url: str = "", interval_s: float = 5.0, session=None):
        self.base_url = str(base_url or "").strip()
        self.interval_s = float(interval_s)
        self.session = session

    def describe(self) -> Dict[str, Any]:
        return {
            "name": self.source_name,
            "kind": "polling",
            "base_url": self.base_url,
            "interval_s": self.interval_s,
        }

    def snapshot(self) -> Dict[str, Any]:
        return {"base_url": self.base_url, "interval_s": self.interval_s}

    def poll_once(self) -> Optional[Dict[str, Any]]:
        return None
