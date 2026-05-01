"""Special-function metadata and helpers for RaceLink domain types."""

from __future__ import annotations

from .device_types import RL_Dev_Type
from .models import RL_Device, RL_DeviceGroup
from .wled_deterministic import is_deterministic
from .wled_effects import WLED_EFFECTS
from .wled_palette_color_rules import WLED_PALETTE_COLOR_RULES
from .wled_palettes import WLED_PALETTES


def _normalize_select_options(raw_options) -> list[dict]:
    """Normalize select-option entries to ``{value, label}`` (plus optional ``slots``).

    ``slots`` carries WLED effect metadata (A12): which per-effect fields are used
    and their custom labels. It is only forwarded for entries that explicitly
    provide it so unrelated selects (e.g. startblock options) stay unchanged.

    ``deterministic`` (bool) is similarly forwarded if explicitly set on the
    raw entry, so the WLED effect-mode list can ship the deterministic flag
    end-to-end (see ``wled_effect_mode_options`` for the source of the flag).
    Other selects (preset list, palette list, startblock options) leave it
    unset, so the frontend renders them with no marker.
    """
    options: list[dict] = []
    for opt in raw_options or []:
        if isinstance(opt, dict):
            value = opt.get("value", opt.get("key"))
            label = opt.get("label", opt.get("name", value))
            extra_slots = opt.get("slots")
            extra_det = opt.get("deterministic")
        else:
            value = getattr(opt, "value", opt)
            label = getattr(opt, "label", getattr(opt, "name", value))
            extra_slots = getattr(opt, "slots", None)
            extra_det = getattr(opt, "deterministic", None)
        if value is None:
            continue
        entry: dict = {"value": str(value), "label": str(label)}
        if isinstance(extra_slots, dict):
            entry["slots"] = extra_slots
        if extra_det is not None:
            entry["deterministic"] = bool(extra_det)
        options.append(entry)
    return options


def wled_preset_select_options(*, context=None, **_kwargs) -> list[dict]:
    """Return the classical WLED preset list (from the uploaded ``presets.json``).

    Pre-rename: ``effect_select_options`` — misleading because the values are
    WLED preset ids (OPC_PRESET payload), not effect-mode indices.

    RL-native presets follow a separate pipeline (see :class:`RLPresetsService`
    and :meth:`ControlService.send_rl_preset_by_id`) and are **not** mixed in
    here — that was the Phase-B design that Phase C reverted.
    """
    ctx = context or {}
    rl_instance = ctx.get("rl_instance") or ctx.get("gc")
    preset_list = None
    if rl_instance is not None:
        preset_list = getattr(rl_instance, "uiPresetList", None)
    if preset_list is None:
        preset_list = ctx.get("uiPresetList") or ctx.get("preset_list")
    return _normalize_select_options(preset_list)


def wled_effect_mode_options(*, context=None, **_kwargs) -> list[dict]:
    """Return the WLED effect-mode select options with the deterministic
    subset marked + sorted to the top.

    "Deterministic" here means the effect's pixel output depends only on
    inputs the master can synchronise via OPC_OFFSET + OPC_SYNC: the
    19-effect set in :mod:`racelink.domain.wled_deterministic` (audited
    against the WLED LoRa fork's ``FX.cpp``). These render identically
    across nodes; everything else drifts. The frontend renders the
    ``deterministic`` flag as a leading ``*`` marker in the dropdown.

    Sort order: deterministic-first (preserving original WLED order
    within the group), then non-deterministic (preserving original
    order). The numeric ``value`` payload is unchanged — only the
    presentation order shifts, so existing scenes that reference an
    effect by id continue to work.
    """
    ctx = context or {}
    rl_instance = ctx.get("rl_instance") or ctx.get("gc")
    override = None
    if rl_instance is not None:
        override = getattr(rl_instance, "uiWledEffectModeList", None)
    raw = override or WLED_EFFECTS
    normalised = _normalize_select_options(raw)
    # Tag every entry with the deterministic flag so the frontend can
    # render the marker consistently. Done after _normalize_select_options
    # so any explicit deterministic= field on the raw entry (test/override
    # path) is preserved; we only fill it in when missing.
    for entry in normalised:
        if "deterministic" not in entry:
            entry["deterministic"] = is_deterministic(entry["value"])
    # Stable sort: deterministic (True) before non-deterministic (False).
    # Python's sort is stable, so within each bucket the original WLED
    # order is preserved.
    normalised.sort(key=lambda e: 0 if e.get("deterministic") else 1)
    return normalised


def wled_palette_options(*, context=None, **_kwargs) -> list[dict]:
    ctx = context or {}
    rl_instance = ctx.get("rl_instance") or ctx.get("gc")
    override = None
    if rl_instance is not None:
        override = getattr(rl_instance, "uiWledPaletteList", None)
    return _normalize_select_options(override or WLED_PALETTES)


def rl_preset_select_options(*, context=None, **_kwargs) -> list[dict]:
    """Return RaceLink-native presets as ``{value, label}`` options.

    ``value`` is the stable int id (stringified) from :class:`RLPresetsService`;
    the Specials UI stores that value and the control service (D2) resolves it
    back via ``sendRlPresetById``. Falls back to a single disabled-looking
    placeholder when no RL presets exist yet.
    """
    ctx = context or {}
    rl_instance = ctx.get("rl_instance") or ctx.get("gc")
    rl_service = getattr(rl_instance, "rl_presets_service", None) if rl_instance else None
    if rl_service is None:
        return [{"value": "0", "label": "— no RL presets —"}]
    try:
        presets = rl_service.list()
    except Exception:
        # swallow-ok: keep UI responsive even if the store is transiently broken
        return [{"value": "0", "label": "— no RL presets —"}]
    if not presets:
        return [{"value": "0", "label": "— no RL presets —"}]
    return [{"value": str(p["id"]), "label": str(p["label"])} for p in presets]


RL_SPECIALS = {
    "STARTBLOCK": {
        "label": "Startblock",
        "options": [
            {"key": "startblock_slots", "label": "Number Of Slots", "option": 0x8C, "min": 1, "max": 8},
            {"key": "startblock_first_slot", "label": "First Slot", "option": 0x8D, "min": 1, "max": 8},
        ],
        "functions": [
            {
                "key": "startblock_control",
                "label": "Startblock Control",
                "comm": "sendStartblockControl",
                "vars": [],
                "type": "control",
                "unicast": True,
                "broadcast": True,
            }
        ],
    },
    "WLED": {
        "label": "WLED",
        "options": [],
        "functions": [
            {
                # "WLED Preset" — classical numeric preset id, OPC_PRESET.
                # Pre-rename: key="wled_control", label="WLED Control".
                "key": "wled_preset",
                "label": "WLED Preset",
                "comm": "sendWledPreset",
                "vars": ["presetId", "brightness"],
                "ui": {
                    "presetId": {"widget": "select", "generator": wled_preset_select_options},
                },
                "type": "control",
                "unicast": True,
                "broadcast": True,
            },
            {
                # "WLED Control" — applies a RaceLink-native preset (OPC_CONTROL,
                # variable length) selected by its stable int id. Phase D:
                # structurally identical to wled_preset above, only the preset
                # source differs (RL presets via RLPresetsService instead of
                # WLED presets.json). Direct 14-field parameter editing lives
                # only in the RL-preset editor (dlgRlPresets), not here.
                "key": "wled_control",
                "label": "WLED Control",
                "comm": "sendWledControl",
                "vars": ["presetId", "brightness"],
                "ui": {
                    "presetId": {"widget": "select", "generator": rl_preset_select_options},
                },
                "type": "control",
                "unicast": True,
                "broadcast": True,
            },
        ],
    },
    "LEDMATRIX": {"label": "Matrix", "options": [], "functions": []},
}


# ----------------------------------------------------------------------
# RL-preset editor schema (Phase D)
# ----------------------------------------------------------------------
# The RL-preset editor in the WebUI (dlgRlPresets) builds a 14-field parameter
# form using the same widget types as the Specials dialog. After Phase D the
# Specials ``wled_control`` action no longer carries those vars (it is now a
# preset picker), so the editor cannot reuse its schema. This standalone
# constant defines the editor form independently; ``serialize_editor_schema``
# resolves the generators into concrete options for the REST layer.

RL_PRESET_EDITOR_SCHEMA = {
    "vars": [
        "mode", "speed", "intensity",
        "custom1", "custom2", "custom3",
        "check1", "check2", "check3",
        "palette",
        "color1", "color2", "color3",
        "brightness",
    ],
    "ui": {
        "mode":       {"widget": "select", "generator": wled_effect_mode_options},
        "palette":    {"widget": "select", "generator": wled_palette_options},
        "speed":      {"widget": "slider", "min": 0, "max": 255},
        "intensity":  {"widget": "slider", "min": 0, "max": 255},
        "custom1":    {"widget": "slider", "min": 0, "max": 255},
        "custom2":    {"widget": "slider", "min": 0, "max": 255},
        "custom3":    {"widget": "slider", "min": 0, "max": 31},
        "brightness": {"widget": "slider", "min": 0, "max": 255},
        "check1":     {"widget": "toggle"},
        "check2":     {"widget": "toggle"},
        "check3":     {"widget": "toggle"},
        "color1":     {"widget": "color"},
        "color2":     {"widget": "color"},
        "color3":     {"widget": "color"},
    },
    # User-intent flags persistable on an RL preset. Mirrors
    # ``racelink.domain.flags.USER_FLAG_KEYS``; POWER_ON/HAS_BRI are
    # derived host-side from brightness at emit-time and never stored.
    "flags": [
        {"key": "arm_on_sync",   "label": "Arm on SYNC"},
        {"key": "force_tt0",     "label": "Force TT=0"},
        {"key": "force_reapply", "label": "Force reapply"},
        {"key": "offset_mode",   "label": "Offset mode"},
    ],
}


def serialize_rl_preset_editor_schema(*, context: dict | None = None) -> dict:
    """Return the RL-preset editor schema with generators resolved to options."""
    ui_out: dict = {}
    for var_key, ui_info in (RL_PRESET_EDITOR_SCHEMA.get("ui") or {}).items():
        ui_copy = dict(ui_info)
        generator = ui_copy.get("generator")
        if callable(generator):
            ui_copy.pop("generator", None)
            ui_copy["options"] = generator(context=context or {})
        ui_out[var_key] = ui_copy
    return {
        "vars": list(RL_PRESET_EDITOR_SCHEMA.get("vars") or []),
        "ui": ui_out,
        "flags": [dict(f) for f in (RL_PRESET_EDITOR_SCHEMA.get("flags") or [])],
        # Auto-extracted from WLED's wled00/data/index.js -> updateSelectedPalette().
        # The frontend mirrors this rule when re-evaluating color slot
        # visibility on palette change. Generated by gen_wled_metadata.py.
        "palette_color_rules": dict(WLED_PALETTE_COLOR_RULES),
    }


def get_specials_config(*, context: dict | None = None, serialize_ui: bool = False) -> dict:
    data = {}
    for cap, info in RL_SPECIALS.items():
        options = [dict(opt) for opt in info.get("options", [])]
        functions = []
        for fn in info.get("functions", []):
            fn_copy = dict(fn)
            ui_meta = {}
            for var_key, ui_info in (fn.get("ui") or {}).items():
                ui_copy = dict(ui_info)
                generator = ui_copy.get("generator")
                if callable(generator):
                    if serialize_ui:
                        ui_copy.pop("generator", None)
                        ui_copy["options"] = generator(context=context or {})
                    else:
                        ui_copy["generator"] = generator
                ui_meta[var_key] = ui_copy
            if ui_meta:
                fn_copy["ui"] = ui_meta
            functions.append(fn_copy)
        data[cap] = {
            **{k: v for k, v in info.items() if k not in {"options", "functions"}},
            "options": options,
            "functions": functions,
        }
    return data


def create_device(*, dev_type: int, specials: dict | None = None, **kwargs) -> RL_Device:
    from .capabilities import build_specials_state

    dev = RL_Device(dev_type=dev_type, **kwargs)
    dev.specials = build_specials_state(dev_type, specials)
    return dev
