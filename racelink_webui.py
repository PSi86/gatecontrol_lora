"""Legacy compatibility shim for the RaceLink web blueprint in ``racelink.web``."""

try:
    from .racelink.web import register_rl_blueprint  # type: ignore
except Exception:  # pragma: no cover
    from racelink.web import register_rl_blueprint  # type: ignore

__all__ = ["register_rl_blueprint"]
