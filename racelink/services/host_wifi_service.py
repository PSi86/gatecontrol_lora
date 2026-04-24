"""Host-side WiFi operations for RaceLink."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from typing import List


class HostWifiService:
    """Reusable host WiFi helpers independent of Flask routes."""

    def wifi_interfaces(self) -> List[str]:
        base = "/sys/class/net"
        interfaces: List[str] = []
        try:
            for name in os.listdir(base):
                if name.startswith("."):
                    continue
                if os.path.isdir(os.path.join(base, name, "wireless")):
                    interfaces.append(name)
        except Exception:
            # swallow-ok: best-effort fallback; caller proceeds with safe default
            interfaces = []
        if not interfaces:
            try:
                interfaces = [name for name in os.listdir(base) if not name.startswith(".")]
            except Exception:
                # swallow-ok: best-effort fallback; caller proceeds with safe default
                interfaces = []
        return sorted(set(interfaces))

    def nmcli_run(self, args: list, timeout_s: float = 20.0) -> subprocess.CompletedProcess:
        if not shutil.which("nmcli"):
            raise RuntimeError("nmcli not available on host (cannot switch WiFi automatically)")
        return subprocess.run(["nmcli"] + args, capture_output=True, text=True, timeout=max(1.0, timeout_s))

    def radio_enabled(self) -> bool:
        try:
            proc = self.nmcli_run(["-t", "-f", "WIFI", "radio"], timeout_s=6.0)
            if proc.returncode != 0:
                raw = (proc.stderr or proc.stdout or "").lower()
                return "enabled" in raw and "disabled" not in raw
            return (proc.stdout or "").strip().lower() == "enabled"
        except Exception:
            # swallow-ok: best-effort fallback; caller proceeds with safe default
            return False

    def set_radio(self, enabled: bool) -> None:
        onoff = "on" if enabled else "off"
        proc = self.nmcli_run(["radio", "wifi", onoff], timeout_s=12.0)
        if proc.returncode != 0:
            out = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(f"nmcli radio wifi {onoff} failed ({proc.returncode}): {out}")

    def wait_iface_ready(self, iface: str, timeout_s: float = 12.0) -> None:
        iface = (iface or "wlan0").strip()
        deadline = time.time() + max(1.0, float(timeout_s))
        last_state = None
        while time.time() < deadline:
            proc = self.nmcli_run(["-t", "-f", "DEVICE,TYPE,STATE", "dev", "status"], timeout_s=6.0)
            if proc.returncode == 0:
                for line in (proc.stdout or "").splitlines():
                    parts = line.split(":")
                    if len(parts) >= 3 and parts[0] == iface and parts[1] == "wifi":
                        last_state = parts[2]
                        if last_state and last_state.lower() != "unavailable":
                            return
            time.sleep(0.4)
        raise RuntimeError(f"host WiFi iface '{iface}' not ready (state={last_state})")

    def rescan(self, iface: str) -> None:
        iface = (iface or "wlan0").strip()
        self.nmcli_run(["dev", "wifi", "rescan", "ifname", iface], timeout_s=10.0)

    def list_ssids(self, iface: str) -> list:
        iface = (iface or "wlan0").strip()
        proc = self.nmcli_run(["-t", "-f", "SSID", "dev", "wifi", "list", "ifname", iface, "--rescan", "no"], timeout_s=12.0)
        if proc.returncode != 0:
            return []
        return [line.strip() for line in (proc.stdout or "").splitlines() if (line or "").strip()]

    def profile_up(self, conn_name: str, iface: str = "", bssid: str = "", timeout_s: float = 30.0) -> None:
        conn_name = str(conn_name or "").strip()
        iface = str(iface or "wlan0").strip()
        bssid = str(bssid or "").strip()
        if not conn_name:
            raise RuntimeError("WiFi connection profile name missing")
        args = ["--wait", str(int(max(10.0, min(90.0, float(timeout_s))))), "con", "up", "id", conn_name, "ifname", iface]
        if bssid:
            args += ["ap", bssid]
        proc = self.nmcli_run(args, timeout_s=max(12.0, min(95.0, float(timeout_s) + 10.0)))
        if proc.returncode != 0:
            out = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(f"nmcli con up failed ({proc.returncode}): {out}")

    def profile_down(self, conn_name: str, timeout_s: float = 20.0) -> None:
        conn_name = str(conn_name or "").strip()
        if not conn_name:
            return
        self.nmcli_run(
            ["--wait", str(int(max(5.0, min(40.0, float(timeout_s))))), "con", "down", "id", conn_name],
            timeout_s=max(10.0, min(45.0, float(timeout_s) + 10.0)),
        )

    def connect_profile(self, conn_name: str, ssid: str, iface: str = "", bssid: str = "", timeout_s: float = 35.0) -> None:
        ssid = str(ssid or "").strip()
        iface = str(iface or "wlan0").strip()
        bssid = str(bssid or "").strip()
        conn_name = str(conn_name or "").strip()
        if not conn_name:
            raise RuntimeError("WiFi connection profile name missing")

        self.wait_iface_ready(iface, timeout_s=12.0)
        deadline = time.time() + max(5.0, float(timeout_s))
        last_err = None
        while time.time() < deadline:
            try:
                self.rescan(iface)
            except Exception:
                # swallow-ok: best-effort fallback; caller proceeds with safe default
                pass

            if ssid:
                if ssid not in self.list_ssids(iface):
                    time.sleep(0.7)
                    continue

            try:
                self.profile_up(conn_name, iface=iface, bssid=bssid, timeout_s=min(60.0, max(15.0, float(timeout_s))))
                return
            except Exception as ex:
                last_err = str(ex)
                if "No network with SSID" in last_err or "no suitable device" in last_err.lower():
                    time.sleep(0.8)
                    continue
                if "Wi-Fi is disabled" in last_err or "wireless is disabled" in last_err.lower():
                    raise RuntimeError(last_err)
                time.sleep(0.9)

        if last_err:
            raise RuntimeError(f"nmcli profile connect timeout: {last_err}")
        raise RuntimeError("nmcli profile connect timeout")
