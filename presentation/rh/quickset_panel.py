from __future__ import annotations

from RHUI import UIField, UIFieldType


def register_quickset_ui(gc):
    effect_options = gc._get_select_options("wled_control", "presetId")
    default_effect = effect_options[0].value if effect_options else "01"
    gc._rhapi.ui.register_panel("rl_quickset", "RaceLink Quickset", "run")
    gc._rhapi.fields.register_option(
        UIField("rl_quickset_group", "Node Group", UIFieldType.SELECT, options=gc.uiGroupList, value=gc.uiGroupList[0].value),
        "rl_quickset",
    )
    gc._rhapi.fields.register_option(
        UIField("rl_quickset_effect", "Color", UIFieldType.SELECT, options=effect_options, value=default_effect),
        "rl_quickset",
    )
    gc._rhapi.fields.register_option(
        UIField("rl_quickset_brightness", "Brightness", UIFieldType.BASIC_INT, value=70),
        "rl_quickset",
    )
    gc._rhapi.ui.register_quickbutton(
        "rl_quickset",
        "run_quickset",
        "Apply",
        gc.groupSwitch,
        args={"manual": True},
    )
