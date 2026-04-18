"""Primary package surface for the refactored RaceLink architecture."""

from ._version import VERSION, __version__, get_version
from .app import RaceLinkApp
from .core import AppEvent, DataSink, EventSource, NullSink, NullSource

__all__ = [
    "AppEvent",
    "DataSink",
    "EventSource",
    "NullSink",
    "NullSource",
    "RaceLinkApp",
    "VERSION",
    "__version__",
    "get_version",
]
