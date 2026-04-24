"""Scope tokens for state-change notifications to plugins and SSE clients.

Each ``save_to_db`` / ``on_persistence_changed`` call carries a set of these
tokens so downstream consumers (RotorHazard plugin UI adapter, web SSE layer)
can re-register only the panels / broadcast only the topics that actually
depend on the mutated state.

The tokens are plain strings so they survive serialisation boundaries (SSE,
callback args) without importing the enum class.
"""

from __future__ import annotations

from typing import Iterable

DEVICES = "devices"
DEVICE_MEMBERSHIP = "device_membership"
DEVICE_SPECIALS = "device_specials"
GROUPS = "groups"
EFFECTS = "effects"
FULL = "full"
NONE = "none"

ALL = frozenset(
    {
        DEVICES,
        DEVICE_MEMBERSHIP,
        DEVICE_SPECIALS,
        GROUPS,
        EFFECTS,
        FULL,
        NONE,
    }
)


def normalize_scopes(scopes: Iterable[str] | None) -> set[str]:
    """Return a validated set of scope tokens (unknown tokens are dropped)."""
    if not scopes:
        return {FULL}
    normalized = {str(s) for s in scopes if str(s) in ALL}
    if not normalized:
        return {FULL}
    return normalized


def sse_what_from_scopes(scopes: Iterable[str] | None) -> list[str]:
    """Map a scope set to the topics consumed by the SSE ``refresh`` event.

    ``DEVICE_MEMBERSHIP`` changes which group a device belongs to; the Groups
    sidebar shows the per-group device count, so it has to re-render on
    membership changes as well. Without this the sidebar stays stale until the
    operator manually reloads the page (see plan "Bug Investigation: Bulk
    Set Group").
    """
    resolved = normalize_scopes(scopes)
    if FULL in resolved:
        return ["groups", "devices"]
    if resolved == {NONE}:
        return []
    what: list[str] = []
    if resolved & {DEVICES, DEVICE_MEMBERSHIP, DEVICE_SPECIALS}:
        what.append("devices")
    if resolved & {GROUPS, DEVICE_MEMBERSHIP}:
        what.append("groups")
    if EFFECTS in resolved:
        what.append("effects")
    return what
