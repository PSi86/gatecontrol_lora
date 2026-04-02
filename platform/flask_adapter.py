from __future__ import annotations

import logging
from typing import Any

from flask import Flask, jsonify

from ..controller import RaceLink_LoRa
from ..core.repository import InMemoryDeviceRepository
from ..core.event_bus import InMemoryEventBus
from .ports import ConfigStorePort, UINotificationPort

logger = logging.getLogger(__name__)


class InMemoryConfigStore(ConfigStorePort):
    def __init__(self):
        self._data: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value


class FlaskUINotifier(UINotificationPort):
    def notify(self, message: str, level: str = "info") -> None:
        logger.info("[Standalone:%s] %s", level.upper(), message)

    def broadcast_ui(self, panel: str) -> None:
        logger.debug("UI broadcast requested for panel: %s", panel)


class _StandaloneDB:
    def __init__(self, store: InMemoryConfigStore):
        self._store = store

    def option(self, key: str, default: Any = None):
        return self._store.get(key, default)

    def option_set(self, key: str, value: Any):
        self._store.set(key, value)


class _StandaloneUI:
    def __init__(self, notifier: FlaskUINotifier):
        self._notifier = notifier

    def broadcast_ui(self, panel: str):
        self._notifier.broadcast_ui(panel)


class _StandaloneRHAPI:
    """Minimal RHAPI-compatible facade for standalone execution."""

    def __init__(self, config: InMemoryConfigStore, notifier: FlaskUINotifier):
        self.db = _StandaloneDB(config)
        self.ui = _StandaloneUI(notifier)


class FlaskStandaloneAdapter:
    def __init__(self):
        self.repository = InMemoryDeviceRepository()
        self.event_bus = InMemoryEventBus()
        self.config_store = InMemoryConfigStore()
        self.ui = FlaskUINotifier()
        self.rhapi = _StandaloneRHAPI(self.config_store, self.ui)
        self.rl_instance: RaceLink_LoRa | None = None

    def create_app(self) -> Flask:
        app = Flask("racelink-standalone")

        @app.get("/health")
        def health():
            return jsonify({"ok": True})

        @app.get("/api/devices")
        def devices():
            devices = self.repository.all() if hasattr(self.repository, "all") else []
            return jsonify({"count": len(devices)})

        return app

    def initialize(self) -> RaceLink_LoRa:
        self.rl_instance = RaceLink_LoRa(
            self.rhapi,
            "RaceLink_LoRa",
            "RaceLink Standalone",
            repository=self.repository,
        )
        return self.rl_instance
