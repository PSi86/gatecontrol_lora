"""RaceLink application container.

RL-003 introduces a central place for dependency wiring without changing the
active runtime behavior yet.
"""

from __future__ import annotations

from .core import NullSink, NullSource


class RaceLinkApp:
    """Container for the currently wired RaceLink runtime dependencies."""

    def __init__(
        self,
        *,
        controller,
        transport=None,
        state_repository=None,
        services=None,
        integrations=None,
        event_source=None,
        data_sink=None,
    ):
        self.controller = controller
        self.transport = transport
        self.state_repository = state_repository
        self.services = services or {}
        self.integrations = integrations or {}
        self.event_source = event_source or NullSource()
        self.data_sink = data_sink or NullSink()

    @property
    def rl_instance(self):
        """Compatibility alias for the existing controller-centric runtime."""
        return self.controller

    @property
    def device_repository(self):
        return self.state_repository.devices if self.state_repository else None

    @property
    def group_repository(self):
        return self.state_repository.groups if self.state_repository else None
