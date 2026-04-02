from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable


class InMemoryEventBus:
    """Simple in-process event bus used for internal domain events."""

    def __init__(self):
        self._listeners: dict[str, list[Callable[[Any], None]]] = defaultdict(list)

    def subscribe(self, event_name: str, handler: Callable[[Any], None]) -> None:
        self._listeners[event_name].append(handler)

    def publish(self, event_name: str, payload: Any = None) -> None:
        for handler in list(self._listeners.get(event_name, [])):
            handler(payload)
