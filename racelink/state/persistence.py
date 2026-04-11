"""JSON-based persistence helpers with tolerant legacy decoding."""

from __future__ import annotations

import ast
import json


def _as_record(obj):
    if isinstance(obj, dict):
        return dict(obj)
    return dict(getattr(obj, "__dict__", {}))


def _normalize_records(value):
    if value is None:
        return []
    if isinstance(value, list):
        return [dict(item) for item in value if isinstance(item, dict)]
    if isinstance(value, tuple):
        return [dict(item) for item in value if isinstance(item, dict)]
    raise TypeError("expected a list-like collection of dict records")


def dump_records(items) -> str:
    records = [_as_record(item) for item in items]
    return json.dumps(records, ensure_ascii=True)


def load_records(raw, *, default=None):
    if raw is None:
        return _normalize_records(default)
    if isinstance(raw, (list, tuple)):
        return _normalize_records(raw)

    text = str(raw).strip()
    if text == "":
        return []

    try:
        decoded = json.loads(text)
        return _normalize_records(decoded)
    except Exception:
        pass

    try:
        decoded = ast.literal_eval(text)
        return _normalize_records(decoded)
    except Exception:
        return _normalize_records(default)
