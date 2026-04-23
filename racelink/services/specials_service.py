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

    def coerce_action_params(self, fn_info: dict, options_by_key: dict, params: dict | None):
        params = params or {}
        coerced = {}
        for var in fn_info.get("vars", []) or []:
            raw_val = params.get(var, None)
            if raw_val is None:
                raw_val = options_by_key.get(var, {}).get("min", 0)
            try:
                value_int = int(raw_val)
            except Exception as ex:
                raise ValueError(f"invalid value for {var}") from ex
            opt_meta = options_by_key.get(var, {})
            min_v = opt_meta.get("min")
            max_v = opt_meta.get("max")
            if min_v is not None and value_int < int(min_v):
                raise ValueError(f"{var} must be >= {min_v}")
            if max_v is not None and value_int > int(max_v):
                raise ValueError(f"{var} must be <= {max_v}")
            coerced[var] = value_int
        return coerced
