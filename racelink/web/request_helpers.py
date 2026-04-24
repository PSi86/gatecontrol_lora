"""Request parsing helpers shared by the RaceLink web API."""

from __future__ import annotations


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
