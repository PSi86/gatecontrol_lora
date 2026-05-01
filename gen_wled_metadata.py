#!/usr/bin/env python3
"""Generate racelink/domain/wled_effects.py, wled_palettes.py, and
wled_palette_color_rules.py from WLED source.

Reads:
- <WLED>/wled00/FX.h            -- FX_MODE_* IDs (0..219)
- <WLED>/wled00/FX.cpp          -- _data_FX_MODE_*[] PROGMEM strings (display name + slot metadata)
- <WLED>/wled00/FX_fcn.cpp      -- JSON_palette_names[] string literal
- <WLED>/wled00/data/index.js   -- updateSelectedPalette() palette->color-slot rule

Pairs names by the identifier stem (e.g. FX_MODE_BLINK <-> _data_FX_MODE_BLINK) and parses
the WLED effect metadata format to extract per-effect slot info:

    "<Name>@<sx>,<ix>,<c1>,<c2>,<c3>,<o1>,<o2>,<o3>;<col1>,<col2>,<col3>;<pal>;<flags>;<ext>"

Tokens in group 1 are (in order) the labels for sliders speed/intensity/custom1..3 and
toggles check1..3. Tokens in group 2 label the 3 color slots. Group 3 labels the palette.
Empty token = slot unused; "!" = slot used, default WLED label (UI keeps generic name);
any other string = custom label to display.

The palette-color rule encodes which built-in "* Color..." palettes (ids 2..5 in
stock WLED) force-show extra color pickers regardless of the effect's static
metadata. Lives in WLED's webui (``wled00/data/index.js`` -> ``updateSelectedPalette``);
mirrored here so the RL-preset editor stays in sync without manual transcription.

Writes Python modules exposing ``WLED_EFFECTS`` / ``WLED_PALETTES`` as
``[{"value": "<id>", "label": "<name>", "slots": {...}}]`` and
``WLED_PALETTE_COLOR_RULES`` as a small dict consumed by the WebUI.

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
# Palette-conditional color slot rule lives in WLED's webui, not in firmware.
_PAL_COLOR_FN_RE = re.compile(
    r"function\s+updateSelectedPalette\s*\([^)]*\)\s*\{(?P<body>.+?)\n\}",
    re.S,
)
_PAL_COLOR_OUTER_RE = re.compile(
    r"if\s*\(\s*s\s*>\s*(?P<lo>\d+)\s*&&\s*s\s*<\s*(?P<hi>\d+)\s*\)\s*\{(?P<inner>.+?)\}\s*else",
    re.S,
)
_PAL_COLOR_NESTED_RE = re.compile(
    r"if\s*\(\s*s\s*>\s*(?P<thr>\d+)\s*\)\s*cd\[(?P<slot>\d+)\]\.classList\.remove\('hide'\)\s*;"
)
_PAL_COLOR_SLOT0_RE = re.compile(r"cd\[0\]\.classList\.remove\('hide'\)\s*;")

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

    slots: Dict[str, dict] = {name: {"used": False, "label": None} for name in _ALL_SLOT_FIELDS}

    if "@" not in raw:
        # WLED convention: effects without an explicit ``@`` metadata string
        # still react to the default controls — at minimum the three color
        # slots and the palette. "Solid" is the canonical example: it needs
        # Color 1 to render. Sliders/toggles stay ``used=False`` because they
        # require explicit labels to have any meaning in the UI.
        for default_used in ("color1", "color2", "color3", "palette"):
            slots[default_used] = {"used": True, "label": None}
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


def parse_palette_color_rule(index_js: str) -> Dict[str, object]:
    """Extract the palette-conditional color slot rule from WLED's webui.

    Source pattern (``wled00/data/index.js`` -> ``updateSelectedPalette``)::

        if (s > LO && s < HI) {
            cd[0].classList.remove('hide');
            if (s > T1) cd[1].classList.remove('hide');
            if (s > T2) cd[2].classList.remove('hide');
        } else { ... }

    where ``cd[n]`` is color slot ``n`` and ``s`` is the selected palette id.
    The rule says: while ``s in (LO, HI)`` (exclusive), force-show slot 0
    unconditionally, slot 1 when ``s > T1``, slot 2 when ``s > T2``.

    Returns a dict shaped for direct ``JSON.stringify``-style consumption::

        {"force_slot_min_palette": [LO+1, T1+1, T2+1], "max_palette_id": HI-1}

    where ``force_slot_min_palette[n]`` is the lowest palette id that
    force-shows slot ``n`` (inclusive), and palettes above ``max_palette_id``
    fall back to the effect's own slot metadata.

    Raises :class:`RuntimeError` if the source structure no longer matches —
    a deliberate hard stop so a WLED upgrade that reshapes the rule is
    visible at generation time, not silently mistranscribed.
    """

    fn = _PAL_COLOR_FN_RE.search(index_js)
    if not fn:
        raise RuntimeError(
            "updateSelectedPalette() not found in index.js -- WLED webui "
            "structure changed; update gen_wled_metadata.parse_palette_color_rule"
        )
    body = fn.group("body")

    outer = _PAL_COLOR_OUTER_RE.search(body)
    if not outer:
        raise RuntimeError(
            "Outer 'if (s > LO && s < HI)' guard not found inside "
            "updateSelectedPalette(); WLED rule shape changed"
        )
    lo = int(outer.group("lo"))
    hi = int(outer.group("hi"))
    inner = outer.group("inner")

    if not _PAL_COLOR_SLOT0_RE.search(inner):
        raise RuntimeError(
            "Slot-0 unconditional remove('hide') not found inside "
            "updateSelectedPalette(); WLED rule shape changed"
        )

    nested = {int(m.group("slot")): int(m.group("thr")) for m in _PAL_COLOR_NESTED_RE.finditer(inner)}
    if 1 not in nested or 2 not in nested:
        raise RuntimeError(
            "Slot 1 / slot 2 nested 'if (s > T) cd[N].classList.remove' lines "
            "not both found; WLED rule shape changed"
        )

    return {
        "force_slot_min_palette": [lo + 1, nested[1] + 1, nested[2] + 1],
        "max_palette_id": hi - 1,
    }


def _collect_palette_color_rule(wled: pathlib.Path) -> Dict[str, object]:
    index_js = _load(wled / "wled00" / "data" / "index.js")
    return parse_palette_color_rule(index_js)


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


def _emit_palette_color_rule(rule: Dict[str, object], wled: str, ts: str) -> str:
    lines = [_HEADER.format(wled=wled, ts=ts)]
    lines.append("# Source: wled00/data/index.js -- updateSelectedPalette()")
    lines.append("# force_slot_min_palette[n] = lowest palette id that force-shows color slot n (inclusive).")
    lines.append("# max_palette_id            = highest palette id the rule applies to (inclusive).")
    lines.append("WLED_PALETTE_COLOR_RULES = {")
    lines.append(f"    'force_slot_min_palette': {list(rule['force_slot_min_palette'])!r},")
    lines.append(f"    'max_palette_id': {rule['max_palette_id']!r},")
    lines.append("}\n")
    return "\n".join(lines)


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
    ap.add_argument(
        "--out-palette-color-rules",
        default="racelink/domain/wled_palette_color_rules.py",
        help="Output path for the palette-conditional color slot rule module",
    )
    args = ap.parse_args()

    wled = pathlib.Path(args.wled)
    if not (wled / "wled00" / "FX.h").exists():
        raise SystemExit(f"WLED sources not found under {wled}")

    fx = _collect_fx(wled)
    palettes = _collect_palettes(wled)
    palette_color_rule = _collect_palette_color_rule(wled)
    ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    out_fx = pathlib.Path(args.out_effects)
    out_pal = pathlib.Path(args.out_palettes)
    out_rules = pathlib.Path(args.out_palette_color_rules)
    wled_posix = str(wled).replace("\\", "/")
    out_fx.write_text(_emit_effects("WLED_EFFECTS", fx, wled_posix, ts), encoding="utf-8")
    out_pal.write_text(_emit_palettes("WLED_PALETTES", palettes, wled_posix, ts), encoding="utf-8")
    out_rules.write_text(_emit_palette_color_rule(palette_color_rule, wled_posix, ts), encoding="utf-8")

    print(f"Wrote {out_fx} ({len(fx)} effects)")
    print(f"Wrote {out_pal} ({len(palettes)} palettes)")
    print(f"Wrote {out_rules} (palette-color rule: {palette_color_rule})")


if __name__ == "__main__":
    main()
