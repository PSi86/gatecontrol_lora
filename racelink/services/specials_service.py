"""Helpers for resolving and validating specials metadata and actions."""

from __future__ import annotations

from ..domain import get_dev_type_info, get_specials_config


class SpecialsService:
    """Resolve specials options and actions without coupling to Flask routes."""

    def __init__(self, *, rl_instance):
        self.rl_instance = rl_instance

    def _specials_config(self):
        return get_specials_config(context={"rl_instance": self.rl_instance})

    def get_serialized_config(self):
        """Return UI-safe specials metadata for web/API consumers."""
        return get_specials_config(
            context={"rl_instance": self.rl_instance},
            serialize_ui=True,
        )

    def _device_caps(self, dev) -> set[str]:
        return set(get_dev_type_info(getattr(dev, "dev_type", 0)).get("caps", []))

    def resolve_option(self, dev, key: str):
        for cap in self._device_caps(dev):
            spec = self._specials_config().get(cap, {})
            for opt in spec.get("options", []) or []:
                if opt.get("key") == key:
                    return opt
        return None

    def resolve_action(self, dev, fn_key: str):
        options_by_key = {}
        fn_info = None
        for cap in self._device_caps(dev):
            spec = self._specials_config().get(cap, {})
            for opt in spec.get("options", []) or []:
                key_name = opt.get("key")
                if key_name:
                    options_by_key[key_name] = opt
            for fn in spec.get("functions", []) or []:
                if fn.get("key") == fn_key:
                    fn_info = fn
                    break
            if fn_info:
                break
        return fn_info, options_by_key

    @staticmethod
    def coerce_int(value, *, default=None):
        try:
            return int(value)
        except Exception:
            # swallow-ok: best-effort fallback; caller proceeds with safe default
            return default

    def validate_option_value(self, option_info: dict, value_int: int) -> None:
        min_v = option_info.get("min")
        max_v = option_info.get("max")
        if min_v is not None and value_int < int(min_v):
            raise ValueError(f"value must be >= {min_v}")
        if max_v is not None and value_int > int(max_v):
            raise ValueError(f"value must be <= {max_v}")

    @staticmethod
    def _coerce_color(raw) -> tuple[int, int, int]:
        """Accept ``{r, g, b}``, ``[r, g, b]``, or ``"#RRGGBB"`` and return an RGB tuple."""

        if isinstance(raw, str):
            s = raw.strip()
            if s.startswith("#"):
                s = s[1:]
            if len(s) == 6:
                try:
                    r = int(s[0:2], 16)
                    g = int(s[2:4], 16)
                    b = int(s[4:6], 16)
                    return r & 0xFF, g & 0xFF, b & 0xFF
                except ValueError as ex:
                    raise ValueError(f"invalid hex color {raw!r}") from ex
            raise ValueError(f"invalid color string {raw!r}")
        if isinstance(raw, dict):
            try:
                return int(raw["r"]) & 0xFF, int(raw["g"]) & 0xFF, int(raw["b"]) & 0xFF
            except (KeyError, TypeError, ValueError) as ex:
                raise ValueError(f"invalid color object {raw!r}") from ex
        if isinstance(raw, (list, tuple)) and len(raw) == 3:
            try:
                return int(raw[0]) & 0xFF, int(raw[1]) & 0xFF, int(raw[2]) & 0xFF
            except (TypeError, ValueError) as ex:
                raise ValueError(f"invalid color array {raw!r}") from ex
        raise ValueError(f"unsupported color value {raw!r}")

    @staticmethod
    def _coerce_toggle(raw) -> bool:
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return bool(raw)
        if isinstance(raw, str):
            return raw.strip().lower() in ("1", "true", "yes", "on")
        return bool(raw)

    def coerce_action_params(self, fn_info: dict, options_by_key: dict, params: dict | None):
        """Coerce raw request params into Python types per widget.

        Typing per widget:
        - ``color`` -> ``tuple[int, int, int]``
        - ``toggle`` -> ``bool``
        - ``slider`` / ``select`` / legacy -> ``int`` (min/max validated)

        **Missing values are NOT defaulted** — if a var is absent from the
        request (typical for A12 when the WebUI hides effect-irrelevant fields),
        it stays out of the coerced dict. Downstream services can treat missing
        keys as "leave that slot untouched" (see ``build_control_adv_body`` which
        emits fieldMask/extMask bits only for provided kwargs). Callers that
        need a legacy default (e.g. ``send_wled_control``) already apply
        ``params.get(key, fallback)`` themselves.
        """

        params = params or {}
        ui_meta = fn_info.get("ui") or {}
        coerced: dict = {}
        for var in fn_info.get("vars", []) or []:
            if var not in params:
                continue

            raw_val = params.get(var)
            widget = (ui_meta.get(var) or {}).get("widget")

            if widget == "color":
                coerced[var] = self._coerce_color(raw_val)
                continue

            if widget == "toggle":
                coerced[var] = self._coerce_toggle(raw_val)
                continue

            # Default: numeric slider/select/legacy
            try:
                value_int = int(raw_val)
            except Exception as ex:
                raise ValueError(f"invalid value for {var}") from ex
            # Range bounds: prefer UI widget bounds, fall back to options_by_key (legacy).
            ui_bounds = ui_meta.get(var) or {}
            bounds_src = ui_bounds if ("min" in ui_bounds or "max" in ui_bounds) else options_by_key.get(var, {})
            min_v = bounds_src.get("min")
            max_v = bounds_src.get("max")
            if min_v is not None and value_int < int(min_v):
                raise ValueError(f"{var} must be >= {min_v}")
            if max_v is not None and value_int > int(max_v):
                raise ValueError(f"{var} must be <= {max_v}")
            coerced[var] = value_int
        return coerced
