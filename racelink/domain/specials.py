"""Special-function metadata and helpers for RaceLink domain types."""

from __future__ import annotations

from .device_types import RL_Dev_Type
from .models import RL_Device, RL_DeviceGroup


def _normalize_select_options(raw_options) -> list[dict]:
    options: list[dict] = []
    for opt in raw_options or []:
        if isinstance(opt, dict):
            value = opt.get("value", opt.get("key"))
            label = opt.get("label", opt.get("name", value))
        else:
            value = getattr(opt, "value", opt)
            label = getattr(opt, "label", getattr(opt, "name", value))
        if value is None:
            continue
        options.append({"value": str(value), "label": str(label)})
    return options


def effect_select_options(*, context=None, **_kwargs) -> list[dict]:
    ctx = context or {}
    rl_instance = ctx.get("rl_instance") or ctx.get("gc")
    effect_list = None
    if rl_instance is not None:
        effect_list = getattr(rl_instance, "uiEffectList", None)
    if effect_list is None:
        effect_list = ctx.get("uiEffectList") or ctx.get("effect_list")
    return _normalize_select_options(effect_list)


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
                "key": "wled_control",
                "label": "WLED Control",
                "comm": "sendWledControl",
                "vars": ["presetId", "brightness"],
                "ui": {
                    "presetId": {"generator": effect_select_options},
                },
                "type": "control",
                "unicast": True,
                "broadcast": True,
            }
        ],
    },
    "LEDMATRIX": {"label": "Matrix", "options": [], "functions": []},
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
