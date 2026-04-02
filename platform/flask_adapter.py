"""Backward-compatible import shim for standalone adapter."""

from ..plugins.standalone.flask_adapter import FlaskStandaloneAdapter

__all__ = ["FlaskStandaloneAdapter"]
