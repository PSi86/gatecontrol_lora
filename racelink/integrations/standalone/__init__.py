"""Standalone runtime integration package placeholder."""

from .config import StandaloneConfig, StandaloneOptionStore

__all__ = [
    "build_standalone_runtime",
    "create_standalone_app",
    "run_standalone",
    "StandaloneConfig",
    "StandaloneOptionStore",
]


def __getattr__(name):
    if name == "build_standalone_runtime":
        from .bootstrap import build_standalone_runtime

        return build_standalone_runtime
    if name in {"create_standalone_app", "run_standalone"}:
        from .webapp import create_standalone_app, run_standalone

        return {
            "create_standalone_app": create_standalone_app,
            "run_standalone": run_standalone,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
