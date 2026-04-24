#!/usr/bin/env python3
"""Generate racelink/domain/wled_effects.py and wled_palettes.py from WLED source.

Reads:
- <WLED>/wled00/FX.h            -- FX_MODE_* IDs (0..219)
- <WLED>/wled00/FX.cpp          -- _data_FX_MODE_*[] PROGMEM strings (display name + slot metadata)
- <WLED>/wled00/FX_fcn.cpp      -- JSON_palette_names[] string literal

Pairs names by the identifier stem (e.g. FX_MODE_BLINK <-> _data_FX_MODE_BLINK) and parses
the WLED effect metadata format to extract per-effect slot info:

    "<Name>@<sx>,<ix>,<c1>,<c2>,<c3>,<o1>,<o2>,<o3>;<col1>,<col2>,<col3>;<pal>;<flags>;<ext>"

Tokens in group 1 are (in order) the labels for sliders speed/intensity/custom1..3 and
toggles check1..3. Tokens in group 2 label the 3 color slots. Group 3 labels the palette.
Empty token = slot unused; "!" = slot used, default WLED label (UI keeps generic name);
any other string = custom label to display.

Writes Python modules exposing ``WLED_EFFECTS`` / ``WLED_PALETTES`` as
``[{"value": "<id>", "label": "<name>", "slots": {...}}]`` -- slot info is consumed by
the WebUI (A12) to hide unused fields and re-label used ones per selected effect.

Usage:
  py gen_wled_metadata.py
  py gen_wled_metadata.py --wled "C:/path/to/WLED"
"""

from __future__ import annotations

import argparse
import datetime as _dt
import pathlib
import re
from typing import Dict, List, Tuple

DEFAULT_WLED_DIR = r"C:\Users\psima\Dev\WLED LoRa\WLED"

_FX_ID_RE = re.compile(r"^#define\s+FX_MODE_(\w+)\s+(\d+)\s*$", re.M)
# Capture the full metadata payload (incl. "@..." slot spec), not just the name.
_FX_DATA_RE = re.compile(
    r'_data_FX_MODE_(\w+)\[\]\s*PROGMEM\s*=\s*"([^"]*)"',
    re.S,
)
_PAL_NAMES_RE = re.compile(
    r'JSON_palette_names\[\]\s*PROGMEM\s*=\s*R"=====\(\s*\[(.*?)\]\s*\)=====";',
    re.S,
)

# Position in group-1 tokens -> RaceLink field name.
_GROUP1_FIELDS = ("speed", "intensity", "custom1", "custom2", "custom3", "check1", "check2", "check3")
_GROUP2_FIELDS = ("color1", "color2", "color3")
_ALL_SLOT_FIELDS = _GROUP1_FIELDS + _GROUP2_FIELDS + ("palette",)


def parse_fx_metadata(raw: str) -> Tuple[str, Dict[str, dict]]:
    """Parse a ``_data_FX_MODE_*`` payload into ``(name, slots)``.

    ``slots`` maps every RaceLink field name to ``{"used": bool, "label": str|None}``.
    ``label`` is only set when the effect supplies a custom non-"!" label; a "!" token
    means "used, keep the generic UI label" and leaves ``label`` as ``None``.
    """

    # Default: no slot is used (applies when the raw string has no "@" spec,
    # e.g. "Solid"). The WebUI can still show generic controls if it wants to.
    slots: Dict[str, dict] = {name: {"used": False, "label": None} for name in _ALL_SLOT_FIELDS}

    if "@" not in raw:
        return raw.strip(), slots

    name, _, spec = raw.partition("@")
    name = name.strip()

    # Trailing groups after group 4 are parser hints (e.g. ";;m12=0"), ignored.
    groups = spec.split(";")

    def _classify(tok: str) -> dict:
        t = tok.strip()
        if not t:
            return {"used": False, "label": None}
        if t == "!":
            return {"used": True, "label": None}
        return {"used": True, "label": t}

    # Group 1: sliders (0..4) + toggles (5..7)
    if len(groups) >= 1:
        tokens = groups[0].split(",")
        for i, field in enumerate(_GROUP1_FIELDS):
            if i < len(tokens):
                slots[field] = _classify(tokens[i])

    # Group 2: colors (0..2)
    if len(groups) >= 2:
        tokens = groups[1].split(",")
        for i, field in enumerate(_GROUP2_FIELDS):
            if i < len(tokens):
                slots[field] = _classify(tokens[i])

    # Group 3: palette (single token)
    if len(groups) >= 3:
        tokens = groups[2].split(",")
        if tokens:
            slots["palette"] = _classify(tokens[0])

    return name, slots


def _load(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def _collect_fx(wled: pathlib.Path) -> List[Tuple[int, str, Dict[str, dict]]]:
    fx_h = _load(wled / "wled00" / "FX.h")
    fx_cpp = _load(wled / "wled00" / "FX.cpp")

    ids: Dict[str, int] = {}
    for m in _FX_ID_RE.finditer(fx_h):
        ids[m.group(1)] = int(m.group(2))

    # Parse every _data_FX_MODE_* string into (label, slots)
    meta: Dict[str, Tuple[str, Dict[str, dict]]] = {}
    for m in _FX_DATA_RE.finditer(fx_cpp):
        stem = m.group(1)
        raw = m.group(2)
        if raw:
            meta[stem] = parse_fx_metadata(raw)

    # Default slots (everything unused) for effects that have no _data_ entry.
    empty_slots = {name: {"used": False, "label": None} for name in _ALL_SLOT_FIELDS}

    out: List[Tuple[int, str, Dict[str, dict]]] = []
    for stem, idx in ids.items():
        if stem in meta:
            label, slots = meta[stem]
        else:
            # Fallback: human-readable from the stem (e.g. COLOR_WIPE_RANDOM -> "Color Wipe Random")
            label = " ".join(part.capitalize() for part in stem.split("_"))
            slots = dict(empty_slots)
        out.append((idx, label, slots))

    out.sort(key=lambda p: p[0])
    # Deduplicate by id (keep first)
    seen: set[int] = set()
    unique: List[Tuple[int, str, Dict[str, dict]]] = []
    for idx, label, slots in out:
        if idx in seen:
            continue
        seen.add(idx)
        unique.append((idx, label, slots))
    return unique


def _collect_palettes(wled: pathlib.Path) -> List[Tuple[int, str]]:
    # Fixed built-in palettes 0..5 are hardcoded in WLED (Default, *Random Cycle, ...).
    # JSON_palette_names[] starts at index 0 with "Default" (see FX_fcn.cpp).
    fx_fcn = _load(wled / "wled00" / "FX_fcn.cpp")
    m = _PAL_NAMES_RE.search(fx_fcn)
    if not m:
        raise RuntimeError("Could not locate JSON_palette_names[] in FX_fcn.cpp")
    block = m.group(1)
    names = [s.strip() for s in re.findall(r'"([^"]+)"', block)]
    return list(enumerate(names))


_HEADER = (
    '"""Auto-generated WLED effect/palette metadata. Do not edit by hand.\n\n'
    "Generated by gen_wled_metadata.py from the WLED checkout at:\n"
    "  {wled}\n"
    "Regenerate when upgrading the bundled WLED firmware version.\n"
    '"""\n\n'
    "from __future__ import annotations\n\n"
    "# Generated: {ts}\n"
)


def _emit_palettes(list_name: str, items: List[Tuple[int, str]], wled: str, ts: str) -> str:
    lines = [_HEADER.format(wled=wled, ts=ts)]
    lines.append(f"{list_name} = [")
    for idx, label in items:
        lines.append(f"    {{'value': '{idx}', 'label': {label!r}}},")
    lines.append("]\n")
    return "\n".join(lines)


def _format_slots(slots: Dict[str, dict]) -> str:
    """Emit slots dict as a compact single-line Python literal."""
    parts = []
    for field in _ALL_SLOT_FIELDS:
        info = slots.get(field, {"used": False, "label": None})
        if info.get("label") is None:
            parts.append(f"'{field}': {{'used': {bool(info['used'])}}}")
        else:
            parts.append(f"'{field}': {{'used': {bool(info['used'])}, 'label': {info['label']!r}}}")
    return "{" + ", ".join(parts) + "}"


def _emit_effects(list_name: str, items: List[Tuple[int, str, Dict[str, dict]]], wled: str, ts: str) -> str:
    lines = [_HEADER.format(wled=wled, ts=ts)]
    lines.append(f"{list_name} = [")
    for idx, label, slots in items:
        lines.append(
            f"    {{'value': '{idx}', 'label': {label!r}, 'slots': {_format_slots(slots)}}},"
        )
    lines.append("]\n")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wled", default=DEFAULT_WLED_DIR, help="Path to WLED checkout root")
    ap.add_argument(
        "--out-effects",
        default="racelink/domain/wled_effects.py",
        help="Output path for effect metadata module",
    )
    ap.add_argument(
        "--out-palettes",
        default="racelink/domain/wled_palettes.py",
        help="Output path for palette metadata module",
    )
    args = ap.parse_args()

    wled = pathlib.Path(args.wled)
    if not (wled / "wled00" / "FX.h").exists():
        raise SystemExit(f"WLED sources not found under {wled}")

    fx = _collect_fx(wled)
    palettes = _collect_palettes(wled)
    ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    out_fx = pathlib.Path(args.out_effects)
    out_pal = pathlib.Path(args.out_palettes)
    wled_posix = str(wled).replace("\\", "/")
    out_fx.write_text(_emit_effects("WLED_EFFECTS", fx, wled_posix, ts), encoding="utf-8")
    out_pal.write_text(_emit_palettes("WLED_PALETTES", palettes, wled_posix, ts), encoding="utf-8")

    print(f"Wrote {out_fx} ({len(fx)} effects)")
    print(f"Wrote {out_pal} ({len(palettes)} palettes)")


if __name__ == "__main__":
    main()
