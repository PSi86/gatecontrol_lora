from __future__ import annotations

from typing import Any, Callable, Protocol


class EventBusPort(Protocol):
    """Abstraction for event based communication."""

    def subscribe(self, event_name: str, handler: Callable[[Any], None]) -> None: ...

    def publish(self, event_name: str, payload: Any = None) -> None: ...


class ConfigStorePort(Protocol):
    """Abstraction over persisted key-value configuration."""

    def get(self, key: str, default: Any = None) -> Any: ...

    def set(self, key: str, value: Any) -> None: ...


class UINotificationPort(Protocol):
    """Unified UI/notification surface used by adapters."""

    def notify(self, message: str, level: str = "info") -> None: ...

    def broadcast_ui(self, panel: str) -> None: ...


class RacePilotDataProviderPort(Protocol):
    """Read-only access to race and pilot related runtime data."""

    def get_current_heat_slot_list(self) -> list[tuple[int, str, str]]: ...
