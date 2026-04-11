"""Legacy compatibility shim for RotorHazard UI imports now under ``racelink.integrations.rotorhazard``."""

try:
    from .racelink.integrations.rotorhazard.ui import RotorHazardUIAdapter
except ImportError:  # pragma: no cover
    from racelink.integrations.rotorhazard.ui import RotorHazardUIAdapter

__all__ = ["RotorHazardUIAdapter"]
