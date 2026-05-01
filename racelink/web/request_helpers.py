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


# WLED's stock AP password. Default for a fresh fleet that hasn't been
# reconfigured. Operators with a custom password override per-OTA via the
# editor's "AP password" field.
WLED_DEFAULT_AP_PASSWORD = "wled1234"

# WLED's stock OTA password (DEFAULT_OTA_PASS in const.h). Used by the
# host-side auto-unlock POST to /settings/sec — when WLED's /update
# returns 401, the host POSTs `OP=<this>` to clear `otaLock` (and, as
# a side effect of `SU` being absent, flip `otaSameSubnet=false`).
# Operators whose fleet uses a non-default otaPass override per-OTA.
WLED_DEFAULT_OTA_PASSWORD = "wledota"

# Default SSID candidates: newer firmware broadcasts the first, older
# firmware the second. ``connect_ap`` connects to whichever appears
# first on a scan.
DEFAULT_WLED_AP_SSIDS = ["WLED_RaceLink_AP", "WLED-AP"]


def _normalise_ssid_list(raw) -> list:
    """Accept ``None``, a string (comma-separated allowed), or an iterable
    of strings; return a clean list of non-empty SSID candidates in input
    order. Unknown shapes return an empty list (caller substitutes the
    default or 400s)."""
    if raw is None:
        return []
    if isinstance(raw, str):
        items = raw.split(",")
    else:
        try:
            items = list(raw)
        except TypeError:
            return []
    out = []
    seen = set()
    for item in items:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def parse_wifi_options(body, ota_service):
    """Translate the editor's wifi sub-body into the kwargs the OTA
    workflow expects.

    Body shape (all optional):

      * ``wifi.ssids`` (list[str]) or ``wifi.ssid`` / ``wifiSsid`` (str,
        comma-split for back-compat) — candidate AP names. Defaults to
        :data:`DEFAULT_WLED_AP_SSIDS` (newer firmware first).
      * ``wifi.password`` / ``wifiPassword`` — WLED AP password.
        Defaults to :data:`WLED_DEFAULT_AP_PASSWORD`.
      * ``wifi.iface`` / ``wifiIface`` — wireless interface, default
        ``wlan0``.
      * ``wifi.bssid`` / ``wifiBssid`` — pin to a specific BSSID
        (rarely used; useful when more than one node within range
        broadcasts the same SSID).
      * ``wifi.timeoutS`` / ``wifiTimeoutS`` — overall scan+connect
        budget; default 20 s.
      * ``wifi.otaPassword`` / ``wifiOtaPassword`` — WLED OTA password
        for the auto-unlock POST on a 401 from ``/update``. Defaults
        to :data:`WLED_DEFAULT_OTA_PASSWORD` (``"wledota"``).
      * ``hostWifiEnable`` / ``hostWifiRestore`` — control whether the
        host's WiFi radio is touched; defaults true.

    A ``connName`` field is silently ignored: the dynamic
    ``nmcli dev wifi connect`` path supersedes the pre-created NM
    profile flow we used before. Old request bodies that still carry it
    keep working without surfacing an error.

    Raises :class:`RequestParseError` if the resolved SSID list is
    empty after normalisation — the workflow has no AP to look for and
    would otherwise loop until ``timeoutS`` for nothing.
    """
    body = body or {}
    wifi = body.get("wifi") or {}

    ssids = _normalise_ssid_list(
        wifi.get("ssids")
        if "ssids" in wifi
        else (wifi.get("ssid") if "ssid" in wifi else body.get("wifiSsid"))
    )
    if not ssids:
        # No SSID supplied at all → fall back to the built-in default.
        # Explicit-but-empty input (e.g. ``ssids=[]`` or ``ssid=""``)
        # falls through to the 400 below.
        if "ssids" not in wifi and "ssid" not in wifi and "wifiSsid" not in body:
            ssids = list(DEFAULT_WLED_AP_SSIDS)
        else:
            raise RequestParseError(
                "wifi.ssids: at least one SSID required (default: "
                f"{', '.join(DEFAULT_WLED_AP_SSIDS)})"
            )

    password = str(
        wifi.get("password")
        or body.get("wifiPassword")
        or WLED_DEFAULT_AP_PASSWORD
    )

    ota_password = str(
        wifi.get("otaPassword")
        or body.get("wifiOtaPassword")
        or WLED_DEFAULT_OTA_PASSWORD
    )

    return {
        "base_url": ota_service.wled_base_url(body.get("baseUrl") or ""),
        "ssids": ssids,
        "password": password,
        "ota_password": ota_password,
        "iface": str(wifi.get("iface") or body.get("wifiIface") or "wlan0"),
        "bssid": str(wifi.get("bssid") or body.get("wifiBssid") or ""),
        "timeout_s": float(wifi.get("timeoutS") or body.get("wifiTimeoutS") or 20.0),
        "host_wifi_enable": bool(wifi.get("hostWifiEnable") if "hostWifiEnable" in wifi else body.get("hostWifiEnable", True)),
        "host_wifi_restore": bool(wifi.get("hostWifiRestore") if "hostWifiRestore" in wifi else body.get("hostWifiRestore", True)),
    }
