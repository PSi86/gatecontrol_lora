from __future__ import annotations

import logging

from .providers import MockPollingRaceEventAdapter, MockRaceProvider


class MockPluginRuntime:
    """Minimal, inert plugin runtime used as a plugin development reference."""

    def __init__(self, race_provider: MockRaceProvider | None = None, event_adapter: MockPollingRaceEventAdapter | None = None):
        self._logger = logging.getLogger(__name__)
        self.race_provider = race_provider or MockRaceProvider()
        self.event_adapter = event_adapter or MockPollingRaceEventAdapter(logger=self._logger)
        self._started = False

    @classmethod
    def build(cls, **_kwargs) -> "MockPluginRuntime":
        """Build a standalone mock runtime without host dependencies."""
        return cls()

    def start(self) -> "MockPluginRuntime":
        if self._started:
            return self
        self._started = True
        self._logger.info("Mock plugin started")
        self.event_adapter.start()
        return self

    def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        self.event_adapter.stop()
        self._logger.info("Mock plugin stopped")
