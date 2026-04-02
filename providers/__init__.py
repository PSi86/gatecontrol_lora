"""Race provider adapters."""

from .mock_provider import MockRaceProvider
from .rotorhazard_provider import RotorHazardRaceProvider

__all__ = ["MockRaceProvider", "RotorHazardRaceProvider"]
