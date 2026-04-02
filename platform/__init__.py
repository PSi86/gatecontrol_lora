"""Deprecated platform package kept as compatibility layer."""

from .flask_adapter import FlaskStandaloneAdapter
from .rh_adapter import RotorHazardAdapter

__all__ = ["FlaskStandaloneAdapter", "RotorHazardAdapter"]
