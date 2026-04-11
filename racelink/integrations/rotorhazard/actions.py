"""RotorHazard-specific action and quickset adapter methods."""

from __future__ import annotations

import logging

from EventActions import ActionEffect
from RHUI import UIField, UIFieldType

from ....data import RL_FLAG_HAS_BRI, RL_FLAG_POWER_ON, get_specials_config

logger = logging.getLogger(__name__)


class RotorHazardActionsMixin:
    def registerActions(self, args=None):
        logger.debug("Registering RaceLink Actions")

        if args and "register_fn" in args:
            self.controller.action_reg_fn = args["register_fn"]
            logger.debug("Saved Actions Register Function in RaceLink Instance")

        if not args and self.controller.action_reg_fn:
            effect_options = self._get_select_options("wled_control", "presetId")
            default_effect = effect_options[0].value if effect_options else "01"
            for effect in [
                ActionEffect(
                    "RaceLink Action",
                    self.groupSwitch,
                    [
                        UIField(
                            "rl_action_group",
                            "Node Group",
                            UIFieldType.SELECT,
                            options=self.controller.uiGroupList,
                            value=self.controller.uiGroupList[0].value,
                        ),
                        UIField("rl_action_effect", "Color", UIFieldType.SELECT, options=effect_options, value=default_effect),
                        UIField("rl_action_brightness", "Brightness", UIFieldType.BASIC_INT, value=70),
                    ],
                    name="gcaction",
                )
            ]:
                self.controller.action_reg_fn(effect)

            specials = get_specials_config(context={"rhapi": self.rhapi, "gc": self.controller})
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
                            options = self.rl_createUiDevList(capabilities=[cap_key], outputGroups=False)["devices"]
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
                            options = self.rl_createUiDevList(capabilities=[cap_key], outputDevices=False)["groups"]
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
                            select_options = self._get_select_options(fn_key, var)
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
                        if not fields:
                            continue
                        label = f"{cap_label} by Device" if fn_label == cap_label else f"{fn_label} by Device"
                        self.controller.action_reg_fn(
                            ActionEffect(label, self._make_special_action_handler(fn_key, "device"), fields, name=f"rl_special_{fn_key}_device")
                        )
                    if allow_broadcast:
                        fields = _build_fields("group")
                        if not fields:
                            continue
                        label = f"{cap_label} by Group" if fn_label == cap_label else f"{fn_label} by Group"
                        self.controller.action_reg_fn(
                            ActionEffect(label, self._make_special_action_handler(fn_key, "group"), fields, name=f"rl_special_{fn_key}_group")
                        )

    def _make_special_action_handler(self, fn_key: str, mode: str):
        def _handler(action, args=None):
            return self.specialAction(action, fn_key, mode)

        return _handler

    def specialAction(self, action, fn_key: str, mode: str):
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
                target_device = self.controller.getDeviceFromAddress(target_addr)
        else:
            try:
                target_group = int(action.get(f"rl_special_{fn_key}_group"))
            except Exception:
                target_group = None

        comm_name = fn_info.get("comm")
        if not comm_name:
            logger.warning("specialAction: missing comm function for %s", fn_key)
            return

        comm_fn = getattr(self.controller, comm_name, None)
        if not callable(comm_fn):
            logger.warning("specialAction: comm function missing: %s", comm_name)
            return

        logger.debug("RL: specialAction %s (%s)", fn_key, cap_key or "unknown")
        try:
            comm_fn(targetDevice=target_device, targetGroup=target_group, params=params)
        except Exception:
            logger.exception("RL: specialAction failed: %s", fn_key)

    def nodeSwitch(self, action, args=None):
        if "rl_action_device" in action:
            logger.debug("Action triggered")
            targetDevice = self.controller.getDeviceFromAddress(action["rl_action_device"])
            if targetDevice is None:
                logger.warning("nodeSwitch: device not found: %r", action["rl_action_device"])
                return
            targetDevice.brightness = int(action["rl_action_brightness"])
            targetDevice.presetId = int(action["rl_action_effect"])
            targetDevice.flags = (RL_FLAG_POWER_ON if int(action["rl_action_brightness"]) > 0 else 0) | RL_FLAG_HAS_BRI
            self.controller.sendRaceLink(targetDevice)

        if "manual" in action:
            logger.debug("Manual triggered")
            targetDevice = self.controller.getDeviceFromAddress(self.rhapi.db.option("rl_quickset_device", None))
            if targetDevice is None:
                logger.warning("nodeSwitch(manual): device not found in DB option")
                return
            targetDevice.brightness = int(self.rhapi.db.option("rl_quickset_brightness", None))
            targetDevice.presetId = int(self.rhapi.db.option("rl_quickset_effect", None))
            targetDevice.flags = (RL_FLAG_POWER_ON if int(self.rhapi.db.option("rl_quickset_brightness", None)) > 0 else 0) | RL_FLAG_HAS_BRI
            self.controller.sendRaceLink(targetDevice)

    def groupSwitch(self, action, args=None):
        if "rl_action_group" in action:
            logger.debug("Action triggered")
            targetGroup = int(action["rl_action_group"])
            targetBrightness = int(action["rl_action_brightness"])
            targetEffect = int(action["rl_action_effect"])
            targetFlags = (RL_FLAG_POWER_ON if int(action["rl_action_brightness"]) > 0 else 0) | RL_FLAG_HAS_BRI
            self.controller.sendGroupControl(targetGroup, targetFlags, targetEffect, targetBrightness)

        if "manual" in action:
            logger.debug("Manual triggered")
            targetGroup = int(self.rhapi.db.option("rl_quickset_group", None))
            targetBrightness = int(self.rhapi.db.option("rl_quickset_brightness", None))
            targetEffect = int(self.rhapi.db.option("rl_quickset_effect", None))
            targetFlags = (RL_FLAG_POWER_ON if int(self.rhapi.db.option("rl_quickset_brightness", None)) > 0 else 0) | RL_FLAG_HAS_BRI
            self.controller.sendGroupControl(targetGroup, targetFlags, int(targetEffect), targetBrightness)
