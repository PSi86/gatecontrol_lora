"""HTTP-based sink scaffold for future outbound RaceLink integrations."""

from __future__ import annotations

from typing import Any, Dict

from ...core import AppEvent, DataSink


class HttpSink(DataSink):
    """Prepared HTTP sink for later forwarding RaceLink events outward."""

    sink_name = "http"

    def __init__(self, *, endpoint: str = "", session=None):
        self.endpoint = str(endpoint or "").strip()
        self.session = session

    def describe(self) -> Dict[str, Any]:
        return {"name": self.sink_name, "kind": "http", "endpoint": self.endpoint}

    def publish(self, event: AppEvent) -> None:
        return None
