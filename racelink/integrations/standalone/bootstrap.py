"""Standalone runtime bootstrap helpers for RaceLink."""

from __future__ import annotations

from ...core import NullSink, NullSource
from .config import StandaloneConfig
from .webapp import create_standalone_app


def build_standalone_runtime(*, config=None):
    """Build the minimal standalone runtime wiring using local configuration."""

    cfg = config if isinstance(config, StandaloneConfig) else StandaloneConfig.load(config)
    app, rl_app = create_standalone_app(cfg)

    return {
        "config": cfg,
        "flask_app": app,
        "race_link_app": rl_app,
        "event_source": rl_app.event_source if getattr(rl_app, "event_source", None) else NullSource(),
        "data_sink": rl_app.data_sink if getattr(rl_app, "data_sink", None) else NullSink(),
    }
