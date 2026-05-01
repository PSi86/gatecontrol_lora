"""Host-side WiFi operations driving NetworkManager via ``nmcli``.

Used by the OTA workflow to bring the host's WiFi up, connect to
the WLED node's AP, and restore the previous state when done.
Pure subprocess wrapper — the actual interface manipulation is
delegated to ``nmcli`` because it's the only cross-distro tool
we can rely on (works on Raspberry Pi OS / Ubuntu / Debian and
its variants).

Public API:

* ``wifi_interfaces()`` — enumerate available wireless interfaces.
* ``radio_enabled()`` — is the radio on?
* ``set_radio(on: bool)`` — turn the radio on/off.
* ``connect_ap(ssids, password, *, iface, bssid, timeout_s)`` —
  scan for any of the candidate SSIDs and connect via
  ``nmcli dev wifi connect`` with the supplied PSK. Returns the
  matched SSID. NM creates one persistent profile per distinct
  SSID and reuses it on subsequent calls; we deliberately don't
  delete it post-OTA (the secrets and SSID are static, so the
  profile is reused identically by the next run).
* ``disconnect_ap(ssid, timeout_s=...)`` — bring the named
  connection profile down so the operator's normal WiFi
  auto-reconnects after the OTA.
* ``rescan(iface)`` / ``wait_iface_ready(iface, timeout_s)`` —
  helpers used by the connect path.

Threading: blocking subprocess calls. Always invoked from a
task-manager worker thread so the OTA workflow can wait without
blocking the web request thread.

Permissions: ``nmcli`` requires either root, membership of an
appropriate wheel/netdev-style group, or a polkit rule that
authorises the running user. ``scripts/setup_nmcli_polkit.sh``
takes care of this on a fresh Linux install — see that script
plus ``docs/standalone.md`` for the operator-side setup.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
from typing import Iterable, List, Optional, Sequence, Union

logger = logging.getLogger(__name__)


SsidArg = Union[str, Sequence[str]]


def _setup_command_hint() -> str:
    """Return the exact, copy-pasteable command an operator should run
    to fix the polkit denial — including the absolute path to the
    bundled console script.

    Bare ``sudo racelink-setup-nmcli`` fails when the host is installed
    in a venv (the typical pip / piwheel layout used by the
    RotorHazard plugin) because ``sudo``'s default ``secure_path`` does
    not include the venv's ``bin/`` directory. The error message we
    surface to the operator therefore embeds the absolute path the
    running process resolves to. If the script is not on disk for some
    reason we fall back to invoking the module via the venv's Python
    so the command still works.
    """
    script_dir = os.path.dirname(os.path.abspath(sys.executable))
    candidates = [
        os.path.join(script_dir, "racelink-setup-nmcli"),
        os.path.join(script_dir, "racelink-setup-nmcli.exe"),  # Windows
    ]
    for path in candidates:
        if os.path.isfile(path):
            return f"sudo {path}"
    # Console script not in the venv's bin/. Invoke the module directly
    # via the same Python interpreter so we still hit the right install.
    return f"sudo {sys.executable} -m racelink.tools.setup_nmcli_polkit"


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

    @staticmethod
    def _coerce_ssid_list(ssids: SsidArg) -> List[str]:
        if isinstance(ssids, str):
            items: Iterable[str] = [ssids]
        else:
            items = ssids or []
        out = [str(s).strip() for s in items if str(s).strip()]
        return out

    def connect_ap(
        self,
        ssids: SsidArg,
        password: str,
        *,
        iface: str = "",
        bssid: str = "",
        timeout_s: float = 35.0,
    ) -> str:
        """Connect to the first visible SSID from ``ssids`` using ``password``.

        ``nmcli dev wifi connect`` creates (or reuses) one persistent NM
        profile per distinct SSID. The profile is keyed on the SSID — so
        connecting to ten different WLED nodes that all broadcast
        ``WLED_RaceLink_AP`` produces exactly one profile entry, updated
        in place when the password changes. We deliberately don't delete
        the profile after the OTA: it would just churn the NM
        configuration and force a re-authorisation on the next run.

        Returns the SSID we actually connected to (so the caller can show
        it in the operator UI / progress strip). Raises ``RuntimeError``
        on timeout or auth failure.
        """
        candidates = self._coerce_ssid_list(ssids)
        if not candidates:
            raise RuntimeError("no candidate SSIDs supplied")
        if not password:
            raise RuntimeError("AP password missing")
        iface = str(iface or "wlan0").strip()
        bssid = str(bssid or "").strip()

        self.wait_iface_ready(iface, timeout_s=12.0)
        deadline = time.time() + max(5.0, float(timeout_s))
        last_err = None
        while time.time() < deadline:
            try:
                self.rescan(iface)
            except Exception:
                # swallow-ok: scan failures retry on the next loop iteration
                pass

            visible = self.list_ssids(iface)
            matched = next((s for s in candidates if s in visible), None)
            if matched is None:
                time.sleep(0.7)
                continue

            try:
                self._dev_wifi_connect(matched, password, iface=iface, bssid=bssid,
                                       timeout_s=min(60.0, max(15.0, float(timeout_s))))
                return matched
            except Exception as ex:
                last_err = str(ex)
                if "Wi-Fi is disabled" in last_err or "wireless is disabled" in last_err.lower():
                    raise RuntimeError(last_err)
                if "Secrets were required" in last_err or "no secrets provided" in last_err.lower():
                    # NM raises this whenever the PSK auth handshake is
                    # rejected. Two practical causes the operator should
                    # know about: actually-wrong password (config issue)
                    # OR ESP hostapd briefly rate-limiting the host MAC
                    # after a few failed attempts (transient). Naming
                    # both keeps a tired race-day operator from ripping
                    # the firmware open hunting for a wrong-password bug
                    # when the real fix is a 30-second wait.
                    raise RuntimeError(
                        f"AP {matched!r}: authentication failed (Secrets rejected). "
                        "Likely causes: the AP password is wrong, OR the device's "
                        "hostapd is briefly rate-limiting the host after recent "
                        "failed attempts — wait ~30 s and retry. "
                        f"Raw nmcli output: {last_err}"
                    )
                # polkit denial is deterministic — re-trying produces the
                # same denial. Re-raise immediately so the operator sees
                # the actionable hint without waiting for the outer
                # ``timeout_s`` budget.
                if "racelink-setup-nmcli" in last_err:
                    raise
                # Other transient errors — retry next loop tick.
                time.sleep(0.9)

        if last_err:
            raise RuntimeError(
                f"could not connect to any of {candidates}: {last_err}"
            )
        raise RuntimeError(
            f"timeout waiting for one of {candidates} to appear on {iface}"
        )

    def _delete_profile_if_exists(self, ssid: str) -> None:
        """Best-effort: delete the NM connection profile named ``ssid``.

        Called right before ``nmcli dev wifi connect`` to dodge a class
        of NM bugs where a stale profile from a prior OTA makes the
        next connect fail with::

            Error: 802-11-wireless-security.key-mgmt: property is missing.

        Reproduction: OTA device A → succeeds; OTA device B with the
        same SSID a moment later → NM tries to reuse the profile
        created for device A, fails to re-derive ``key-mgmt`` from the
        freshly-cached AP info, and aborts. Forcing a clean profile
        state on every connect makes this deterministic.

        End-state on disk is unchanged from before — exactly one
        profile per SSID, kept by ``nmcli dev wifi connect`` after a
        successful association. We just delete any pre-existing entry
        first instead of trying to reuse it.

        Tolerates the "unknown connection" return — safe no-op when no
        profile exists yet (first OTA on a fresh host).
        """
        proc = self.nmcli_run(
            ["con", "delete", "id", ssid],
            timeout_s=10.0,
        )
        if proc.returncode == 0:
            return
        err = (proc.stderr or proc.stdout or "").lower()
        if "unknown connection" in err or "not found" in err or "no such" in err:
            return
        # Anything else: don't raise. The subsequent connect may still
        # work (e.g. permission issues on ``con delete`` show up later
        # on ``dev wifi connect`` too, with a clearer message). We
        # don't want a delete-step hiccup to mask the real error.

    def _nmcli_connect_once(
        self,
        ssid: str,
        *,
        password: Optional[str],
        iface: str,
        bssid: str,
        wait_s: int,
    ) -> tuple:
        """Single ``nmcli dev wifi connect`` invocation.

        Returns ``(returncode, combined_stderr_stdout)``. Used by
        :meth:`_dev_wifi_connect` so the PSK and open-AP-fallback paths
        share one source of truth for the argv layout. ``password=None``
        omits the ``password`` argument entirely — the right call for
        an open AP (NM rejects ``password <X>`` if the BSS shows no
        security with ``key-mgmt: property is missing``).
        """
        args = [
            "--wait", str(wait_s),
            "dev", "wifi", "connect", ssid,
            "ifname", iface,
        ]
        if password:
            args += ["password", password]
        if bssid:
            args += ["bssid", bssid]
        proc = self.nmcli_run(args, timeout_s=max(15.0, min(70.0, float(wait_s) + 10.0)))
        out = (proc.stderr or proc.stdout or "").strip()
        return proc.returncode, out

    def _dev_wifi_connect(self, ssid: str, password: str, *, iface: str, bssid: str, timeout_s: float) -> None:
        """Run ``nmcli dev wifi connect <ssid> [password <pass>] ...``.

        ``nmcli`` honours ``--wait`` for both the scan and the activation;
        clamp it to a sensible band so a stuck device doesn't hold the
        worker thread for the full caller-supplied timeout (the outer
        retry loop in :meth:`connect_ap` re-issues with a fresh rescan).

        Open-AP fallback: if the PSK connect fails with ``key-mgmt:
        property is missing``, NM is telling us the AP advertises no
        security (open). We retry once without the password rather than
        bubble the error up — handles WLED nodes flashed with an empty
        AP password (the failure mode that broke a fleet OTA where some
        nodes used the default ``wled1234`` and others had cleared it).
        """
        # Pre-delete any stale profile for this SSID so NM creates a
        # fresh one with correct key-mgmt. See
        # ``_delete_profile_if_exists`` for the full failure-mode
        # context. Same on-disk end-state as before (one profile per
        # SSID), just always created fresh per OTA.
        self._delete_profile_if_exists(ssid)

        wait_s = int(max(10.0, min(60.0, float(timeout_s))))
        rc, out = self._nmcli_connect_once(
            ssid, password=password, iface=iface, bssid=bssid, wait_s=wait_s,
        )
        if rc == 0:
            return

        lower = out.lower()
        # Open-AP fallback. The exact string NM emits for a security-mode
        # mismatch is ``Error: 802-11-wireless-security.key-mgmt:
        # property is missing.`` Match on the two keywords so wording
        # variants across NM versions still hit the fallback.
        if "key-mgmt" in lower and "missing" in lower:
            logger.info(
                "AP %r: PSK connect failed with key-mgmt missing; "
                "retrying as open network (no PSK)", ssid,
            )
            # The failed PSK attempt may have left a partial profile —
            # purge it so the open retry creates fresh.
            self._delete_profile_if_exists(ssid)
            rc2, out2 = self._nmcli_connect_once(
                ssid, password=None, iface=iface, bssid=bssid, wait_s=wait_s,
            )
            if rc2 == 0:
                logger.info("AP %r: connected as open network (no PSK)", ssid)
                return
            # Open retry also failed. Surface the original PSK-mode
            # error — it's typically the more informative signal
            # (the AP wasn't actually open, key-mgmt is just stale
            # cache or NM bug; operator should retry the OTA).

        # polkit denial: rc=4 + the literal "Insufficient privileges"
        # / "Not authorized" string on stderr. Translate to an
        # actionable toast pointing at the bundled setup tool — the
        # raw nmcli message is decipherable but the operator usually
        # doesn't recognise it as a polkit issue.
        if "insufficient privileges" in lower or "not authorized" in lower:
            hint = _setup_command_hint()
            raise RuntimeError(
                "nmcli access denied (polkit). Run this command on the "
                "host to grant the running user unattended access, then "
                "restart the host (RotorHazard or racelink-standalone):\n"
                f"  {hint}\n"
                f"Raw nmcli output: {out}"
            )
        raise RuntimeError(f"nmcli dev wifi connect failed ({rc}): {out}")

    def disconnect_ap(self, ssid: str, timeout_s: float = 20.0) -> None:
        """Bring the named NM connection (= SSID) down post-OTA so the
        operator's normal WiFi auto-reconnects. The profile itself is
        kept on disk so the next OTA reuses the stored secrets without
        re-prompting; only the active connection is deactivated.
        """
        ssid = str(ssid or "").strip()
        if not ssid:
            return
        self.nmcli_run(
            ["--wait", str(int(max(5.0, min(40.0, float(timeout_s))))), "con", "down", "id", ssid],
            timeout_s=max(10.0, min(45.0, float(timeout_s) + 10.0)),
        )
