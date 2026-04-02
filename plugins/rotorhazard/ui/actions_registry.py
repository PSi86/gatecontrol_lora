from __future__ import annotations

import logging

from EventActions import ActionEffect
from RHUI import UIField, UIFieldType

from ....data import get_specials_config

logger = logging.getLogger(__name__)


def register_actions(gc, args=None):
    logger.debug("Registering RaceLink Actions")

    if args and "register_fn" in args:
        gc.action_reg_fn = args["register_fn"]
        logger.debug("Saved Actions Register Function in RaceLink Instance")

    if args or not gc.action_reg_fn:
        return

    effect_options = gc._get_select_options("wled_control", "presetId")
    default_effect = effect_options[0].value if effect_options else "01"
    gc.action_reg_fn(
        ActionEffect(
            "RaceLink Action",
            gc.groupSwitch,
            [
                UIField(
                    "rl_action_group",
                    "Node Group",
                    UIFieldType.SELECT,
                    options=gc.uiGroupList,
                    value=gc.uiGroupList[0].value,
                ),
                UIField("rl_action_effect", "Color", UIFieldType.SELECT, options=effect_options, value=default_effect),
                UIField("rl_action_brightness", "Brightness", UIFieldType.BASIC_INT, value=70),
            ],
            name="gcaction",
        )
    )

    specials = get_specials_config(context={"rhapi": gc._rhapi, "gc": gc})
    for cap_key, cap_info in specials.items():
        funcs = cap_info.get("functions", []) or []
        if not funcs:
            continue
        cap_label = cap_info.get("label", cap_key)
        options_by_key = {opt.get("key"): opt for opt in cap_info.get("options", [])}
        for fn_info in funcs:
            fn_key = fn_info.get("key")
            fn_label = fn_info.get("label") or cap_label
            vars_list = fn_info.get("vars", []) or []
            allow_unicast = bool(fn_info.get("unicast"))
            allow_broadcast = bool(fn_info.get("broadcast"))
            fn_type = fn_info.get("type", "control")

            if fn_type != "control":
                continue

            def _build_fields(mode):
                fields = []
                if mode == "device":
                    options = gc.rl_createUiDevList(capabilities=[cap_key], outputGroups=False)["devices"]
                    if not options:
                        return None
                    fields.append(
                        UIField(
                            f"rl_special_{fn_key}_device",
                            "Device",
                            UIFieldType.SELECT,
                            options=options,
                            value=options[0].value if options else "",
                        )
                    )
                else:
                    options = gc.rl_createUiDevList(capabilities=[cap_key], outputDevices=False)["groups"]
                    if not options:
                        return None
                    fields.append(
                        UIField(
                            f"rl_special_{fn_key}_group",
                            "Group",
                            UIFieldType.SELECT,
                            options=options,
                            value=options[0].value if options else "",
                        )
                    )

                for var in vars_list:
                    opt_meta = options_by_key.get(var, {})
                    label = opt_meta.get("label", var)
                    default_val = opt_meta.get("min", 0)
                    select_options = gc._get_select_options(fn_key, var)
                    if select_options:
                        default_select = select_options[0].value
                        if default_val is not None:
                            for opt in select_options:
                                try:
                                    if int(opt.value) == int(default_val):
                                        default_select = opt.value
                                        break
                                except Exception:
                                    if str(opt.value) == str(default_val):
                                        default_select = opt.value
                                        break
                        fields.append(
                            UIField(
                                f"rl_special_{fn_key}_{var}",
                                label,
                                UIFieldType.SELECT,
                                options=select_options,
                                value=default_select,
                            )
                        )
                        continue
                    fields.append(UIField(f"rl_special_{fn_key}_{var}", label, UIFieldType.BASIC_INT, value=default_val))
                return fields

            if allow_unicast:
                fields = _build_fields("device")
                if fields:
                    label = f"{cap_label} by Device" if fn_label == cap_label else f"{fn_label} by Device"
                    gc.action_reg_fn(
                        ActionEffect(
                            label,
                            _make_special_action_handler(gc, fn_key, "device"),
                            fields,
                            name=f"rl_special_{fn_key}_device",
                        )
                    )
            if allow_broadcast:
                fields = _build_fields("group")
                if fields:
                    label = f"{cap_label} by Group" if fn_label == cap_label else f"{fn_label} by Group"
                    gc.action_reg_fn(
                        ActionEffect(
                            label,
                            _make_special_action_handler(gc, fn_key, "group"),
                            fields,
                            name=f"rl_special_{fn_key}_group",
                        )
                    )


def _make_special_action_handler(gc, fn_key: str, mode: str):
    def _handler(action, args=None):
        return special_action(gc, action, fn_key, mode)

    return _handler


def special_action(gc, action, fn_key: str, mode: str):
    specials = get_specials_config()
    fn_info = None
    cap_key = None
    for cap, info in specials.items():
        for fn in info.get("functions", []) or []:
            if fn.get("key") == fn_key:
                fn_info = fn
                cap_key = cap
                break
        if fn_info:
            break
    if not fn_info:
        logger.warning("specialAction: function not found: %s", fn_key)
        return

    vars_list = fn_info.get("vars", []) or []
    params = {}
    for var in vars_list:
        key = f"rl_special_{fn_key}_{var}"
        try:
            params[var] = int(action.get(key, 0))
        except Exception:
            params[var] = action.get(key, 0)

    target_device = None
    target_group = None
    if mode == "device":
        target_addr = action.get(f"rl_special_{fn_key}_device")
        if target_addr:
            target_device = gc.getDeviceFromAddress(target_addr)
    else:
        try:
            target_group = int(action.get(f"rl_special_{fn_key}_group"))
        except Exception:
            target_group = None

    comm_name = fn_info.get("comm")
    if not comm_name:
        logger.warning("specialAction: missing comm function for %s", fn_key)
        return

    comm_fn = getattr(gc, comm_name, None)
    if not callable(comm_fn):
        logger.warning("specialAction: comm function missing: %s", comm_name)
        return

    logger.debug("RL: specialAction %s (%s)", fn_key, cap_key or "unknown")
    try:
        comm_fn(targetDevice=target_device, targetGroup=target_group, params=params)
    except Exception:
        logger.exception("RL: specialAction failed: %s", fn_key)
