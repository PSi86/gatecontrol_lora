"""Request parsing helpers shared by the RaceLink web API."""

from __future__ import annotations

from typing import Any, Mapping, Optional


class RequestParseError(ValueError):
    """Raised by the require_*/optional_* helpers when a request body
    field is missing, null, or not coercible to the requested type.

    Subclasses :class:`ValueError` so existing route handlers that wrap
    a parsing block in ``except ValueError as ex:`` keep working —
    they'll see the same shape of error they would for a builder-side
    validation failure (e.g. ``_canonical_target`` raising). New code
    can catch :class:`RequestParseError` specifically when it wants to
    distinguish "client sent garbage" from "server-side validation
    failed".

    The message is intentionally short and operator-readable; it ends
    up in the route's 400 response body verbatim.
    """


def require_int(body: Any, key: str, *,
                min: Optional[int] = None,
                max: Optional[int] = None,
                label: Optional[str] = None) -> int:
    """Extract a required int field from a JSON body.

    Raises :class:`RequestParseError` (a ``ValueError`` subclass) when
    the body isn't a dict, the key is missing, the value is ``None``,
    or it can't be coerced to ``int``. Optional ``min`` / ``max``
    enforce a closed range.

    ``label`` is the human-readable name used in error messages; it
    defaults to the dict key. Use it when the wire field name and the
    operator-facing concept differ (e.g. ``key="id"``, ``label="group id"``).

    Typical usage in a Flask route::

        try:
            gid = require_int(body, "id", min=0, max=254, label="group id")
        except RequestParseError as ex:
            return jsonify({"ok": False, "error": str(ex)}), 400
    """
    name = label or key
    if not isinstance(body, Mapping) or key not in body:
        raise RequestParseError(f"missing field: {name}")
    raw = body[key]
    if raw is None:
        raise RequestParseError(f"field {name} must not be null")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise RequestParseError(
            f"field {name} must be an integer, got {raw!r}"
        ) from exc
    if min is not None and value < min:
        raise RequestParseError(f"{name} {value} below minimum {min}")
    if max is not None and value > max:
        raise RequestParseError(f"{name} {value} above maximum {max}")
    return value


def optional_int(body: Any, key: str, default: Optional[int] = None, *,
                 min: Optional[int] = None,
                 max: Optional[int] = None,
                 label: Optional[str] = None) -> Optional[int]:
    """Like :func:`require_int` but returns ``default`` when the key is
    absent or its value is ``None``. A coercion failure or bound
    violation still raises :class:`RequestParseError` — partial input
    is treated as a hard error rather than silently falling back."""
    name = label or key
    if not isinstance(body, Mapping) or key not in body:
        return default
    raw = body[key]
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise RequestParseError(
            f"field {name} must be an integer, got {raw!r}"
        ) from exc
    if min is not None and value < min:
        raise RequestParseError(f"{name} {value} below minimum {min}")
    if max is not None and value > max:
        raise RequestParseError(f"{name} {value} above maximum {max}")
    return value


def parse_recv3_from_addr(addr_str):
    if addr_str is None:
        return None
    try:
        value = str(addr_str)
    except Exception:
        # swallow-ok: best-effort fallback; caller proceeds with safe default
        return None
    hexchars = "0123456789abcdefABCDEF"
    value = "".join(ch for ch in value if ch in hexchars)
    if len(value) < 6:
        return None
    try:
        return bytes.fromhex(value[-6:])
    except Exception:
        # swallow-ok: best-effort fallback; caller proceeds with safe default
        return None


def parse_wifi_options(body, ota_service):
    body = body or {}
    wifi = body.get("wifi") or {}
    return {
        "base_url": ota_service.wled_base_url(body.get("baseUrl") or ""),
        "ssid": str(wifi.get("ssid") or body.get("wifiSsid") or "WLED-AP"),
        "iface": str(wifi.get("iface") or body.get("wifiIface") or "wlan0"),
        "conn_name": str(wifi.get("connName") or body.get("wifiConnName") or "racelink-wled-ap"),
        "bssid": str(wifi.get("bssid") or body.get("wifiBssid") or ""),
        "timeout_s": float(wifi.get("timeoutS") or body.get("wifiTimeoutS") or 35.0),
        "host_wifi_enable": bool(wifi.get("hostWifiEnable") if "hostWifiEnable" in wifi else body.get("hostWifiEnable", True)),
        "host_wifi_restore": bool(wifi.get("hostWifiRestore") if "hostWifiRestore" in wifi else body.get("hostWifiRestore", True)),
    }
