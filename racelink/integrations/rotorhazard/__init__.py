"""RotorHazard integration package for RaceLink."""

__all__ = ["initialize"]


def __getattr__(name):
    if name == "initialize":
        from .plugin import initialize

        return initialize
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
