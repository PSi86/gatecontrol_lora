"""RaceLink-native preset store (Phase B).

Manages persistent user-defined presets that freeze a complete
``OPC_CONTROL_ADV`` parameter snapshot (effect mode, sliders, toggles,
palette, colors, plus the advanced-control flags). Presets are referenced
by a stable int ``id`` (primary key for RotorHazard bindings) or a slug
``key`` (used in REST URLs). Apply goes through
:meth:`ControlService.send_rl_preset_by_id` — no shared path with the
legacy WLED-preset pipeline.

Storage is a single JSON file ``~/.racelink/rl_presets.json`` that is written
atomically (temp file + ``os.replace``). The schema is versioned so later
extensions (scenes, metadata) can migrate cleanly.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from ..domain.flags import USER_FLAG_KEYS

logger = logging.getLogger(__name__)

# v1: presets keyed by slug only.
# v2 (Phase C): stable int ``id`` per preset + top-level ``next_id`` counter
# so RotorHazard bindings can address presets by id across renames/reorders.
SCHEMA_VERSION = 2

# Every RL preset has exactly this parameter shape. ``None`` is significant --
# it means "not present in the advanced-control packet" (fieldMask/extMask bit
# stays 0) and is distinct from 0, False, or an empty color.
_PARAM_KEYS = (
    "mode", "speed", "intensity",
    "custom1", "custom2", "custom3",
    "check1", "check2", "check3",
    "palette",
    "color1", "color2", "color3",
    "brightness",
)
# The user-intent flags persisted on an RL preset. Single source of truth
# lives in ``racelink.domain.flags``. Pre-unification files may only carry
# three of these; missing keys read as ``False`` via ``_canonical_flags``.
_FLAG_KEYS = USER_FLAG_KEYS

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str) -> str:
    base = _SLUG_RE.sub("_", (text or "").strip().lower()).strip("_")
    return base or "preset"


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _canonical_color(value) -> Optional[list]:
    if value is None:
        return None
    if isinstance(value, (list, tuple)) and len(value) == 3:
        return [int(value[0]) & 0xFF, int(value[1]) & 0xFF, int(value[2]) & 0xFF]
    raise ValueError(f"invalid color value {value!r}")


def _canonical_params(raw: Optional[dict]) -> Dict[str, Any]:
    """Normalize a params dict to the canonical RL-preset shape.

    Missing keys are filled with ``None`` so the persisted JSON always has a
    complete key set -- downstream consumers can rely on presence without
    defaulting. Types are coerced (``int`` for u8 sliders, ``bool`` for
    checks, RGB list for colors).
    """

    raw = raw or {}
    out: Dict[str, Any] = {}
    for key in _PARAM_KEYS:
        if key not in raw or raw[key] is None:
            out[key] = None
            continue
        val = raw[key]
        if key in ("check1", "check2", "check3"):
            out[key] = bool(val)
        elif key in ("color1", "color2", "color3"):
            out[key] = _canonical_color(val)
        else:
            out[key] = int(val) & 0xFF
    return out


def _canonical_flags(raw: Optional[dict]) -> Dict[str, bool]:
    raw = raw or {}
    return {k: bool(raw.get(k, False)) for k in _FLAG_KEYS}


class RLPresetsService:
    """CRUD for RaceLink-native presets (``~/.racelink/rl_presets.json``)."""

    def __init__(self, *, storage_path: Optional[str] = None):
        self._path = storage_path or os.path.join(os.path.expanduser("~"), ".racelink", "rl_presets.json")
        self._lock = threading.RLock()
        self._cache: Optional[List[dict]] = None
        # Next id to vend from ``create()``. Monotone-increasing; not
        # decremented on delete so ids are stable for RotorHazard bindings.
        # ``None`` means "not loaded yet" — seeded by ``_load()``.
        self._next_id: Optional[int] = None
        # BF2 mutation hook: set by the integrator (e.g. RotorHazard plugin
        # bootstrap) to refresh dependent UI after every persisted change.
        # Fires best-effort after any successful _write_atomic().
        self.on_changed: Optional[Callable[[], None]] = None

    def _fire_changed(self) -> None:
        cb = self.on_changed
        if cb is None:
            return
        try:
            cb()
        except Exception:
            # swallow-ok: listener crash must not undo a persisted write
            logger.exception("RL presets: on_changed listener raised")

    # ---- paths / persistence --------------------------------------------

    @property
    def path(self) -> str:
        return self._path

    def _ensure_dir(self) -> None:
        os.makedirs(os.path.dirname(self._path), exist_ok=True)

    def _load(self) -> List[dict]:
        """Load and normalize presets; seed ``_next_id`` from the data.

        Handles three on-disk shapes transparently:
        - missing file  → empty list, ``_next_id = 0``.
        - schema_v1     → presets without ``id``; assign ``id = index`` and
                          persist a v2 file back on the first write.
        - schema_v2     → read ``id`` per entry and ``next_id`` top-level.
        """
        if not os.path.isfile(self._path):
            self._next_id = 0
            return []
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("RL presets: failed to load %s (%s); starting empty", self._path, exc)
            self._next_id = 0
            return []
        if not isinstance(data, dict):
            logger.warning("RL presets: %s is not a dict; ignoring", self._path)
            self._next_id = 0
            return []
        schema = int(data.get("schema_version", 0) or 0)
        if schema > SCHEMA_VERSION:
            # Forward-compat placeholder — try best-effort load, warn loudly.
            logger.warning(
                "RL presets: schema_version=%s newer than expected %s; loading best-effort",
                schema, SCHEMA_VERSION,
            )
        raw_presets = data.get("presets")
        if not isinstance(raw_presets, list):
            self._next_id = 0
            return []

        out: List[dict] = []
        used_ids: set[int] = set()
        for index, entry in enumerate(raw_presets):
            if not isinstance(entry, dict):
                continue
            key = entry.get("key")
            label = entry.get("label")
            if not isinstance(key, str) or not isinstance(label, str):
                continue
            # Resolve preset id: prefer persisted int, else index (v1 migration).
            raw_id = entry.get("id")
            try:
                preset_id = int(raw_id) if raw_id is not None else index
            except (TypeError, ValueError):
                preset_id = index
            # Guard against duplicate ids in hand-edited files.
            while preset_id in used_ids:
                preset_id += 1
            used_ids.add(preset_id)
            out.append({
                "id": preset_id,
                "key": key,
                "label": label,
                "created": entry.get("created") or _now_iso(),
                "updated": entry.get("updated") or entry.get("created") or _now_iso(),
                "params": _canonical_params(entry.get("params")),
                "flags": _canonical_flags(entry.get("flags")),
            })

        # Seed next_id: prefer the persisted counter, fall back to max(id)+1.
        persisted_next = data.get("next_id")
        if isinstance(persisted_next, int) and persisted_next > (max(used_ids) if used_ids else -1):
            self._next_id = persisted_next
        else:
            self._next_id = (max(used_ids) + 1) if used_ids else 0

        if schema < SCHEMA_VERSION:
            logger.info(
                "RL presets: migrating %s from schema v%d to v%d (%d presets)",
                self._path, schema, SCHEMA_VERSION, len(out),
            )
            # Persist the migrated shape immediately so subsequent loads skip
            # migration and RH bindings stay stable across restarts.
            self._write_atomic(out)
        return out

    def _write_atomic(self, presets: List[dict]) -> None:
        self._ensure_dir()
        # ``next_id`` may not be seeded yet on the very first write in a fresh
        # install; derive it defensively.
        next_id = self._next_id if self._next_id is not None else (
            (max((int(p["id"]) for p in presets if "id" in p), default=-1) + 1)
        )
        payload = {
            "schema_version": SCHEMA_VERSION,
            "next_id": int(next_id),
            "presets": presets,
        }
        tmp = f"{self._path}.tmp.{os.getpid()}.{int(time.time()*1000)}"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp, self._path)

    def _items(self) -> List[dict]:
        if self._cache is None:
            self._cache = self._load()
        return self._cache

    def _invalidate(self) -> None:
        self._cache = None

    # ---- public read API -------------------------------------------------

    def list(self) -> List[dict]:
        """Return a shallow copy of all presets (safe to iterate/mutate)."""
        with self._lock:
            return [dict(p) for p in self._items()]

    def get(self, key: str) -> Optional[dict]:
        """Look up a preset by its slug key (used by REST URLs / editor)."""
        with self._lock:
            for preset in self._items():
                if preset["key"] == key:
                    return dict(preset)
            return None

    def get_by_id(self, preset_id: int) -> Optional[dict]:
        """Look up a preset by stable int id (used by RH bindings / apply)."""
        try:
            pid = int(preset_id)
        except (TypeError, ValueError):
            return None
        with self._lock:
            for preset in self._items():
                if int(preset.get("id", -1)) == pid:
                    return dict(preset)
            return None

    # ---- mutation helpers -----------------------------------------------

    def _unique_key(self, desired: str, existing: set, *, exclude_key: Optional[str] = None) -> str:
        taken = set(existing)
        if exclude_key and exclude_key in taken:
            taken.discard(exclude_key)
        if desired not in taken:
            return desired
        for idx in range(2, 1000):
            candidate = f"{desired}_{idx}"
            if candidate not in taken:
                return candidate
        raise RuntimeError(f"could not derive a unique key from {desired!r}")

    # ---- public write API -----------------------------------------------

    def create(self, *, label: str, params: Optional[dict] = None, flags: Optional[dict] = None,
               key: Optional[str] = None) -> dict:
        """Create a new preset. ``key`` may be provided explicitly; otherwise
        derived from ``label`` via slugification with ``_N``-suffix on collision."""

        label_clean = (label or "").strip()
        if not label_clean:
            raise ValueError("label is required")
        with self._lock:
            items = self._items()  # also ensures _next_id is seeded
            existing_keys = {p["key"] for p in items}
            desired = _slugify(key or label_clean)
            final_key = self._unique_key(desired, existing_keys)
            now = _now_iso()
            # Claim the next id monotonically; _next_id is persisted with the
            # payload so ids don't recycle even after deletes+restart.
            new_id = int(self._next_id or 0)
            self._next_id = new_id + 1
            preset = {
                "id": new_id,
                "key": final_key,
                "label": label_clean,
                "created": now,
                "updated": now,
                "params": _canonical_params(params),
                "flags": _canonical_flags(flags),
            }
            items.append(preset)
            self._write_atomic(items)
            self._invalidate()
        self._fire_changed()
        return dict(preset)

    def update(self, key: str, *, label: Optional[str] = None,
               params: Optional[dict] = None, flags: Optional[dict] = None) -> Optional[dict]:
        """Partial update. Only passed fields are touched; ``updated`` bumps."""
        updated: Optional[dict] = None
        with self._lock:
            items = list(self._items())
            for idx, preset in enumerate(items):
                if preset["key"] != key:
                    continue
                new_entry = dict(preset)
                if label is not None:
                    label_clean = label.strip()
                    if not label_clean:
                        raise ValueError("label must not be empty")
                    new_entry["label"] = label_clean
                if params is not None:
                    new_entry["params"] = _canonical_params(params)
                if flags is not None:
                    new_entry["flags"] = _canonical_flags(flags)
                new_entry["updated"] = _now_iso()
                items[idx] = new_entry
                self._write_atomic(items)
                self._invalidate()
                updated = new_entry
                break
        if updated is None:
            return None
        self._fire_changed()
        return dict(updated)

    def delete(self, key: str) -> bool:
        with self._lock:
            items = [p for p in self._items() if p["key"] != key]
            if len(items) == len(self._items()):
                return False
            self._write_atomic(items)
            self._invalidate()
        self._fire_changed()
        return True

    def duplicate(self, key: str, *, new_label: Optional[str] = None) -> Optional[dict]:
        """Duplicate an existing preset. New key is derived from ``new_label``
        (or from the original label with a ``_copy`` suffix).

        ``create()`` fires ``on_changed`` itself, so no additional trigger here.
        """
        src = self.get(key)
        if src is None:
            return None
        label = (new_label or f"{src['label']} copy").strip()
        return self.create(label=label, params=src["params"], flags=src["flags"])

    def replace_all(self, presets: List[dict]) -> None:
        """Bulk replace (used by tests / future import flows).

        Assigns fresh monotonic ids (from ``_next_id``) so RotorHazard bindings
        never accidentally reuse an id from the old data.
        """
        with self._lock:
            # Ensure _next_id is seeded (touches _items()).
            self._items()
            canonical = []
            seen_keys: set[str] = set()
            for entry in presets:
                key = _slugify(entry.get("key") or entry.get("label") or "")
                if key in seen_keys:
                    key = self._unique_key(key, seen_keys)
                seen_keys.add(key)
                new_id = int(self._next_id or 0)
                self._next_id = new_id + 1
                canonical.append({
                    "id": new_id,
                    "key": key,
                    "label": (entry.get("label") or key).strip(),
                    "created": entry.get("created") or _now_iso(),
                    "updated": entry.get("updated") or _now_iso(),
                    "params": _canonical_params(entry.get("params")),
                    "flags": _canonical_flags(entry.get("flags")),
                })
            self._write_atomic(canonical)
            self._invalidate()
        self._fire_changed()
