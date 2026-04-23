"""JSON-based persistence helpers for RaceLink state."""

from __future__ import annotations

import ast
import json
import logging
from typing import Any, Iterable

logger = logging.getLogger(__name__)

CURRENT_SCHEMA_VERSION = 1


def try_parse_legacy_repr(raw: Any) -> list[dict] | None:
    """One-shot migration helper for pre-JSON (Python-repr) option values.

    Plan P1-3: the regular ``load_records`` intentionally no longer falls back
    to ``ast.literal_eval``. But if an operator upgrades an old RotorHazard
    deployment whose ``rl_device_config`` was written in the Python-repr era,
    their devices would silently vanish.

    This function is a one-shot bridge: callers detect a malformed JSON value
    in a legacy option key, pass it here, and -- if it parses as a Python
    literal -- receive the normalized records. The caller is then responsible
    for re-serializing via :func:`dump_records` / :func:`dump_state` and
    overwriting the legacy value. Returns ``None`` if the payload cannot be
    salvaged; the controller then logs a WARNING and falls back to defaults.

    ``ast.literal_eval`` only evaluates literals (dicts, lists, strings,
    numbers, tuples, booleans, None) -- it does not execute arbitrary code.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if text == "":
        return None
    try:
        decoded = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return None
    try:
        return _normalize_records(decoded)
    except TypeError:
        return None


def _as_record(obj: Any) -> dict:
    if isinstance(obj, dict):
        return dict(obj)
    return dict(getattr(obj, "__dict__", {}))


def _normalize_records(value: Any) -> list[dict]:
    if value is None:
        return []
    if isinstance(value, list):
        return [dict(item) for item in value if isinstance(item, dict)]
    if isinstance(value, tuple):
        return [dict(item) for item in value if isinstance(item, dict)]
    raise TypeError("expected a list-like collection of dict records")


def _log_decode_failure(source: str, ex: BaseException, text: str) -> None:
    preview = text[:80]
    src = f" ({source})" if source else ""
    logger.warning(
        "RaceLink: could not decode persisted records%s: %s; first 80 chars=%r",
        src,
        ex,
        preview,
    )


def dump_records(items: Iterable[Any]) -> str:
    """Serialize an iterable of records (dicts or objects) to JSON text."""
    records = [_as_record(item) for item in items]
    return json.dumps(records, ensure_ascii=True)


def load_records(raw: Any, *, default: Any = None, source: str = "") -> list[dict]:
    """Decode a JSON list of records. Returns normalized ``default`` on failure.

    The legacy ``ast.literal_eval`` fallback has been removed: malformed input now
    logs a WARNING at the call site and returns the supplied default rather than
    silently succeeding on Python-repr-formatted payloads.
    """
    if raw is None:
        return _normalize_records(default)
    if isinstance(raw, (list, tuple)):
        return _normalize_records(raw)

    text = str(raw).strip()
    if text == "":
        return []

    try:
        decoded = json.loads(text)
    except Exception as ex:
        # swallow-ok: best-effort fallback; caller proceeds with safe default
        _log_decode_failure(source, ex, text)
        return _normalize_records(default)

    try:
        return _normalize_records(decoded)
    except TypeError as ex:
        _log_decode_failure(source, ex, text)
        return _normalize_records(default)


def dump_state(
    devices: Iterable[Any],
    groups: Iterable[Any],
    *,
    schema_version: int = CURRENT_SCHEMA_VERSION,
) -> str:
    """Serialize the combined atomic state payload (P1-5 / P2-7)."""
    payload = {
        "schema_version": int(schema_version),
        "devices": [_as_record(d) for d in devices],
        "groups": [_as_record(g) for g in groups],
    }
    return json.dumps(payload, ensure_ascii=True)


def load_state(
    raw: Any,
    *,
    default_devices: Any = None,
    default_groups: Any = None,
    source: str = "",
) -> tuple[list[dict], list[dict], int]:
    """Decode the combined atomic state payload.

    Returns ``(devices, groups, schema_version)``. A schema_version of ``0`` means
    the payload was missing or malformed -- callers should treat that as "no
    combined state yet" and fall back to the legacy per-key format.
    """
    if raw is None:
        return (
            _normalize_records(default_devices),
            _normalize_records(default_groups),
            0,
        )

    payload: Any
    if isinstance(raw, dict):
        payload = raw
    else:
        text = str(raw).strip()
        if text == "":
            return (
                _normalize_records(default_devices),
                _normalize_records(default_groups),
                0,
            )
        try:
            payload = json.loads(text)
        except Exception as ex:
            # swallow-ok: best-effort fallback; caller proceeds with safe default
            _log_decode_failure(source, ex, text)
            return (
                _normalize_records(default_devices),
                _normalize_records(default_groups),
                0,
            )

    if not isinstance(payload, dict):
        logger.warning(
            "RaceLink: combined state%s is not an object; falling back to legacy keys",
            f" ({source})" if source else "",
        )
        return (
            _normalize_records(default_devices),
            _normalize_records(default_groups),
            0,
        )

    try:
        version = int(payload.get("schema_version", 0) or 0)
    except Exception:
        # swallow-ok: best-effort fallback; caller proceeds with safe default
        version = 0

    devices_raw = payload.get("devices", default_devices)
    groups_raw = payload.get("groups", default_groups)
    try:
        devices = _normalize_records(devices_raw)
    except TypeError:
        logger.warning("RaceLink: combined state 'devices' is not list-like; using default")
        devices = _normalize_records(default_devices)
    try:
        groups = _normalize_records(groups_raw)
    except TypeError:
        logger.warning("RaceLink: combined state 'groups' is not list-like; using default")
        groups = _normalize_records(default_groups)

    return devices, groups, max(0, version)
