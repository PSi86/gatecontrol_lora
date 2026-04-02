from __future__ import annotations

from typing import Protocol


class HostUIPort(Protocol):
    """Host-specific UI integration hooks used by platform plugins."""

    def register_settings_ui(self) -> None:
        """Register settings panel fields/buttons."""

    def register_quickset_ui(self) -> None:
        """Register quick controls in host runtime UI."""

    def register_actions(self, args=None) -> None:
        """Register host action entries and callbacks."""
