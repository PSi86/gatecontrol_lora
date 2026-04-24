"""Typed contract for the host API consumed by RaceLink (plan P2-1).

RaceLink_Host runs in several environments -- the standalone Flask app, the
RotorHazard plugin, and in principle any future host that provides a similar
services surface. It only reaches into the host through a small, well-defined
set of callables: an options/key-value store, an optional UI notifier, and an
optional event-registration hub. This module captures that surface as
``typing.Protocol`` classes so the standalone shim and any plugin adapters
share a single, authoritative spec that static checkers can verify.

Runtime code keeps duck-typing (``getattr``/``hasattr`` guards) because a host
may legitimately omit optional surfaces (``ui``, ``events``) -- e.g. the
standalone shim does not implement UI notifications.
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Protocol, runtime_checkable


@runtime_checkable
class HostOptionStore(Protocol):
    """Persistent key-value store exposed as ``hostapi.db``.

    Both the RotorHazard DB-backed options table and the standalone
    ``StandaloneOptionStore`` satisfy this protocol.
    """

    def option(self, key: str, default: Any = None) -> Any: ...
    def option_set(self, key: str, value: Any) -> None: ...


@runtime_checkable
class HostUiNotifier(Protocol):
    """Optional UI surface exposed as ``hostapi.ui``.

    ``message_notify`` is used for one-shot toast messages; ``broadcast_ui``
    triggers a panel refresh. Hosts without a UI layer should omit this
    attribute entirely rather than providing stub no-ops, so callers can
    detect the absence via ``getattr``.
    """

    def message_notify(self, message: str) -> None: ...
    def broadcast_ui(self, panel: str) -> None: ...


@runtime_checkable
class HostEventBus(Protocol):
    """Optional event-registration surface exposed as ``hostapi.events``.

    RaceLink subscribes to lifecycle events (startup, race start/finish/stop,
    shutdown) through ``events.on(...)``. Hosts that do not emit events omit
    this attribute.
    """

    def on(self, event: Any, handler: Callable[..., Any]) -> Any: ...


@runtime_checkable
class HostApi(Protocol):
    """Minimum surface RaceLink_Host expects from its embedding host.

    ``ui`` and ``events`` are ``Optional`` because the standalone shim -- and
    any future headless embedding -- legitimately omits them.

    Instances of this protocol are stored as ``controller._host_api``.

    Note: the translator is invoked as ``host_api.__(text)`` -- i.e. a method
    literally named ``__`` on the host object, not the Python dunder
    ``__call__``. This mirrors RotorHazard's convention and is how gettext
    aliases are typically exposed.
    """

    db: HostOptionStore
    ui: Optional[HostUiNotifier]
    events: Optional[HostEventBus]

    def __(self, text: str) -> str:
        """Translator hook invoked as ``host_api.__(text)``."""
        ...
