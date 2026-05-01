"""Long-running OTA and presets workflows independent of Flask routes."""

from __future__ import annotations

import logging
import time

# Diagnostic logger for the broad-except sweep (2026-04-27 cont.).
# Most error paths in this module accumulate ``results["errors"]`` for
# the operator-facing toast, which is good for visibility but loses
# the traceback. Adding a module logger lets us preserve the full
# stack for the inevitable "OTA failed but I don't know why" support
# session, without adding noise to the operator UI.
logger = logging.getLogger(__name__)


class OTAWorkflowService:
    def __init__(self, *, host_wifi_service, ota_service, presets_service):
        self.host_wifi = host_wifi_service
        self.ota = ota_service
        self.presets = presets_service

    def _restore_host_wifi(self, results, *, host_wifi_restore, host_wifi_initial, ssid):
        """Bring the WLED-AP connection down and turn the host's WiFi
        radio off (if we were the ones who turned it on). ``ssid`` is the
        SSID we connected to during the OTA — used as the NM connection id
        for the ``con down`` call. NM keeps the persistent profile so the
        next OTA reuses the stored secrets without re-prompting.

        ``profile_down`` (now ``disconnect_ap``) failures are surfaced as
        a non-fatal note in ``results["errors"]``: previously they were
        only debug-logged, which left the operator unaware that their
        normal WiFi might not have auto-reconnected. The radio-off step
        runs regardless so a stale connection doesn't keep the radio
        bound to the WLED AP.
        """
        if host_wifi_restore and (host_wifi_initial is False) and self.host_wifi.radio_enabled():
            try:
                try:
                    self.host_wifi.disconnect_ap(ssid, timeout_s=10.0)
                except Exception as ex:
                    # Surface as a soft warning in the toast — the radio
                    # turn-off below still recovers the operator's normal
                    # state, but the operator should know NM didn't
                    # cleanly release the AP.
                    results["errors"].append(
                        f"Host WiFi cleanup: disconnect from {ssid!r} failed: "
                        f"{type(ex).__name__}: {ex}"
                    )
                    # Warning is the actionable single line; the
                    # traceback drops to DEBUG so support sessions can
                    # still pull it (logger config -> DEBUG) without
                    # spamming operators on the standard log level.
                    # Operator-actionable: the message already names
                    # the failure mode. No traceback (DEBUG or
                    # otherwise) — the stack frames don't add anything
                    # to a routine NM cleanup hiccup.
                    logger.warning("disconnect_ap(%r) failed during restore: %s", ssid, ex)
                self.host_wifi.set_radio(False)
                results["hostWifi"]["enabled"] = False
                results["hostWifi"]["restored"] = True
            except Exception as ex:
                # swallow-ok: surfaces via ``results["errors"]``. Add
                # type prefix so the operator sees the failure mode,
                # not just the message; the traceback is DEBUG so the
                # WARNING line stays a clean one-liner.
                results["errors"].append(
                    f"Host WiFi restore failed: {type(ex).__name__}: {ex}"
                )
                results["ok"] = False
                logger.warning("host wifi restore failed: %s", ex)

    def _ensure_wifi_ready(self, task_manager, *, wifi, host_wifi_enable, host_wifi_initial, results, meta):
        host_wifi_changed = False
        if host_wifi_enable and not host_wifi_initial:
            task_manager.update(meta={**meta, "stage": "HOST_WIFI_ON", "message": "Enabling host WiFi radio..."})
            self.host_wifi.set_radio(True)
            host_wifi_changed = True
            self.host_wifi.wait_iface_ready(wifi["iface"], timeout_s=15.0)
            results["hostWifi"]["enabled"] = True
        return host_wifi_changed

    def _connect_wled_wifi(self, task_manager, *, wifi, host_wifi_enable, host_wifi_changed, results, meta):
        """Scan for any of ``wifi["ssids"]`` and connect to the first
        match using ``wifi["password"]``. Returns ``(matched_ssid,
        host_wifi_changed)``: callers stash the SSID for the
        per-device result and the restore path's ``con down`` call.
        """
        ssids = list(wifi["ssids"])
        ssids_label = ", ".join(ssids) if len(ssids) > 1 else (ssids[0] if ssids else "<none>")
        task_manager.update(
            meta={
                **meta,
                "stage": "CONNECT_WIFI",
                "message": f'Connecting host WiFi (iface {wifi["iface"]}) to SSID "{ssids_label}"',
            }
        )
        try:
            matched = self.host_wifi.connect_ap(
                ssids,
                wifi["password"],
                iface=wifi["iface"],
                bssid=wifi["bssid"],
                timeout_s=wifi["timeout_s"],
            )
            return matched, host_wifi_changed
        except Exception as ex:
            message = str(ex)
            if host_wifi_enable and (not host_wifi_changed) and ("Wi-Fi is disabled" in message or "wireless is disabled" in message.lower()):
                task_manager.update(
                    meta={
                        **meta,
                        "stage": "HOST_WIFI_ON",
                        "message": f'Host WiFi appears disabled; enabling on {wifi["iface"]}...',
                    }
                )
                self.host_wifi.set_radio(True)
                results["hostWifi"]["enabled"] = True
                self.host_wifi.wait_iface_ready(wifi["iface"], timeout_s=15.0)
                matched = self.host_wifi.connect_ap(
                    ssids,
                    wifi["password"],
                    iface=wifi["iface"],
                    bssid=wifi["bssid"],
                    timeout_s=wifi["timeout_s"],
                )
                return matched, True
            raise

    def download_presets(self, *, rl_instance, task_manager, mac: str, base_url: str, wifi: dict, host_wifi_enable: bool, host_wifi_restore: bool):
        results = {"ok": True, "baseUrl": base_url, "addr": mac, "file": None, "errors": []}
        host_wifi_initial = self.host_wifi.radio_enabled()
        results["hostWifi"] = {"wasEnabled": host_wifi_initial, "enabled": host_wifi_initial, "restored": False}
        # ``connected_ssid`` is the SSID we actually associated to so the
        # restore path can deactivate it cleanly. Captured below from
        # ``_connect_wled_wifi`` and consumed by ``_restore_host_wifi``.
        connected_ssid = ""

        try:
            host_wifi_changed = self._ensure_wifi_ready(
                task_manager,
                wifi=wifi,
                host_wifi_enable=host_wifi_enable,
                host_wifi_initial=host_wifi_initial,
                results=results,
                meta={"addr": mac},
            )

            task_manager.update(meta={"stage": "RACELINK_AP_ON", "addr": mac, "message": "Enable WLED AP via RaceLink (waiting for ACK)"})
            ok_ap = rl_instance.sendConfig(0x04, data0=1, recv3=self.ota.recv3_bytes_from_addr(mac), wait_for_ack=True, timeout_s=8.0)
            if not ok_ap:
                raise RuntimeError(f"Timeout waiting for CONFIG ACK from {mac}")

            connected_ssid, host_wifi_changed = self._connect_wled_wifi(
                task_manager,
                wifi=wifi,
                host_wifi_enable=host_wifi_enable,
                host_wifi_changed=host_wifi_changed,
                results=results,
                meta={"addr": mac},
            )

            expected_mac = self.ota.expected_mac_hex(mac)
            task_manager.update(meta={"stage": "WAIT_HTTP", "addr": mac, "message": f"Waiting for WLED /json/info mac to match {expected_mac}"})
            info = self.ota.wait_for_expected_node(base_url, expected_mac, timeout_s=90.0, poll_s=1.0)
            if not info:
                raise RuntimeError(f"Timeout waiting for node (baseUrl={base_url}) to report expected mac {expected_mac}")

            task_manager.update(meta={"stage": "DOWNLOAD_PRESETS", "addr": mac, "message": "Downloading presets.json"})
            payload = self.ota.wled_download_presets(base_url, timeout_s=15.0)
            saved = self.presets.save_payload(payload)
            results["file"] = {k: saved[k] for k in ("name", "size", "saved_ts")}
            results["files"] = self.presets.list_files()

            try:
                rl_instance.sendConfig(0x04, data0=0, recv3=self.ota.recv3_bytes_from_addr(mac), wait_for_ack=True, timeout_s=6.0)
            except Exception as ex:
                # swallow-ok: post-presets sendConfig is a "best to do
                # this but the workflow is already done" cleanup step.
                # Single-line debug entry so a stuck-state pattern is
                # still diagnosable without dumping a stack.
                logger.debug("post-presets sendConfig failed for %s: %s", mac, ex)
        except Exception as ex:
            # swallow-ok: surfaces via ``results["errors"]``. WARNING
            # carries the operator-actionable message; we deliberately
            # do NOT print the traceback even at DEBUG — the formatted
            # exception text says everything and a fleet OTA can hit
            # the same expected failure for several devices.
            results["ok"] = False
            results["errors"].append(f"{type(ex).__name__}: {ex}")
            logger.warning("presets workflow failed for %s: %s", mac, ex)
        finally:
            self._restore_host_wifi(
                results,
                host_wifi_restore=host_wifi_restore,
                host_wifi_initial=host_wifi_initial,
                ssid=connected_ssid,
            )

        return results

    def run_firmware_update(
        self,
        *,
        rl_instance,
        task_manager,
        devices_provider,
        macs: list,
        base_url: str,
        fw_info=None,
        presets_info=None,
        cfg_info=None,
        retries: int = 3,
        stop_on_error: bool = False,
        wifi: dict,
        host_wifi_enable: bool,
        host_wifi_restore: bool,
        skip_validation: bool = False,
    ):
        results = {"ok": True, "baseUrl": base_url, "devices": [], "errors": []}
        host_wifi_initial = self.host_wifi.radio_enabled()
        results["hostWifi"] = {"wasEnabled": host_wifi_initial, "enabled": host_wifi_initial, "restored": False}
        # Captured from the most recent successful ``_connect_wled_wifi``
        # call so the finally-block restore knows which NM connection to
        # bring down. Re-set on each device so a multi-device run that
        # connects to nodes broadcasting different SSIDs (mixed-firmware
        # fleet) still releases the right one at the end.
        last_connected_ssid = ""
        # Emit a single workflow-start line so an operator following the
        # log can confirm what was actually scheduled. Without this the
        # only signal a silently-skipped upload leaves is "no error but
        # no firmware change" — which is exactly the failure mode that
        # prompted the /update endpoint fix.
        ops = []
        if fw_info: ops.append(f"firmware ({fw_info['name']}, {fw_info['size']} B)")
        if presets_info: ops.append(f"presets ({presets_info['name']})")
        if cfg_info: ops.append(f"cfg ({cfg_info['name']})")
        logger.info(
            "fw-update workflow: %d device(s) %s, ops=%s",
            len(macs), [str(m) for m in macs],
            ", ".join(ops) if ops else "<none>",
        )
        if not ops:
            # Defensive: the API route should already 400 on this,
            # but if a future caller bypasses that guard we want a
            # loud failure rather than a silent "no work done" run
            # that the operator notices only after the fact.
            raise RuntimeError(
                "firmware-update called with no operations selected "
                "(no fw_info / presets_info / cfg_info); the API route "
                "should have rejected this with 400"
            )
        if fw_info:
            results["fw"] = {k: fw_info[k] for k in ("id", "name", "size", "sha256")}
        if presets_info:
            results["presets"] = {k: presets_info[k] for k in ("name", "size", "sha256")}
        if cfg_info:
            results["cfg"] = {k: cfg_info[k] for k in ("id", "name", "size", "sha256")}

        try:
            self._ensure_wifi_ready(
                task_manager,
                wifi=wifi,
                host_wifi_enable=host_wifi_enable,
                host_wifi_initial=host_wifi_initial,
                results=results,
                meta={"index": 0, "total": len(macs)},
            )

            total = len(macs)
            for idx, addr in enumerate(macs, start=1):
                expected_mac = self.ota.expected_mac_hex(str(addr))
                dev_res = {
                    "addr": addr,
                    "expectedMac": expected_mac,
                    "groupId": self.ota.lookup_group_id_for_addr(str(addr), devices_provider()),
                    "ok": False,
                    "error": None,
                }
                results["devices"].append(dev_res)
                try:
                    task_manager.update(meta={"stage": "RACELINK_AP_ON", "index": idx, "total": total, "addr": addr, "retries": retries, "message": "Enable WLED AP via RaceLink (waiting for ACK)"})
                    # W4: wait for the device to ACK the AP-enable before
                    # starting the WiFi scan/connect — otherwise the host
                    # races into an empty scan list when LoRa latency
                    # delays the device's AP bring-up.
                    ok_ap = rl_instance.sendConfig(
                        0x04, data0=1,
                        recv3=self.ota.recv3_bytes_from_addr(str(addr)),
                        wait_for_ack=True, timeout_s=8.0,
                    )
                    if not ok_ap:
                        raise RuntimeError(
                            f"Timeout waiting for CONFIG ACK from {addr} (AP-enable)"
                        )
                    logger.info("OTA %s: AP-enable ACK received, scanning for SSIDs", addr)
                    matched_ssid, _changed = self._connect_wled_wifi(
                        task_manager,
                        wifi=wifi,
                        host_wifi_enable=host_wifi_enable,
                        host_wifi_changed=results["hostWifi"]["enabled"] and not host_wifi_initial,
                        results=results,
                        meta={"index": idx, "total": total, "addr": addr, "retries": retries},
                    )
                    last_connected_ssid = matched_ssid or last_connected_ssid
                    dev_res["ssid"] = matched_ssid
                    logger.info("OTA %s: connected to SSID %r", addr, matched_ssid)
                    task_manager.update(meta={"stage": "WAIT_HTTP", "index": idx, "total": total, "addr": addr, "retries": retries, "message": f"Waiting for WLED /json/info mac to match {expected_mac}"})
                    info = self.ota.wait_for_expected_node(base_url, expected_mac, timeout_s=90.0, poll_s=1.0)
                    if not info:
                        raise RuntimeError(f"Timeout waiting for node (baseUrl={base_url}) to report expected mac {expected_mac}")
                    dev_res["info_before"] = {k: info.get(k) for k in ("mac", "ver", "arch", "name")}
                    logger.info(
                        "OTA %s: WLED reachable (mac=%s ver=%s name=%r)",
                        addr, info.get("mac"), info.get("ver"), info.get("name"),
                    )

                    if presets_info:
                        self.ota.wled_upload_file(base_url, presets_info["path"], timeout_s=45.0, dest_name="presets.json")
                    if cfg_info:
                        self.ota.wled_upload_file(base_url, cfg_info["path"], timeout_s=45.0, dest_name="cfg.json")
                    if fw_info:
                        ok = False
                        last_err = None
                        for attempt in range(1, retries + 1):
                            try:
                                task_manager.update(
                                    meta={
                                        "stage": "UPLOAD_FW",
                                        "index": idx,
                                        "total": total,
                                        "addr": addr,
                                        "attempt": attempt,
                                        "retries": retries,
                                        "message": f"Uploading firmware (try {attempt}/{retries})",
                                    }
                                )
                                # 60 s reflects the real ESP flash + reboot
                                # cycle better than the legacy 30 s default;
                                # the retry loop still bounds total time.
                                # ``ota_password`` is the WLED OTA password
                                # used by ``wled_upload_firmware``'s 401
                                # auto-unlock fallback; default is WLED's
                                # stock ``"wledota"``. ``skip_validation``
                                # lets the operator bypass WLED's
                                # release-name check for cross-fork
                                # migrations (off by default).
                                self.ota.wled_upload_firmware(
                                    base_url, fw_info["path"],
                                    timeout_s=60.0,
                                    ota_password=wifi.get("ota_password", "wledota"),
                                    skip_validation=skip_validation,
                                )
                                ok = True
                                break
                            except Exception as ex:
                                # swallow-ok: retry loop. ``last_err``
                                # surfaces in the RuntimeError below if
                                # all attempts fail; single-line debug
                                # entry naming the exception is enough
                                # to track intermittent failures across
                                # attempts. No traceback — the
                                # exception's message already carries
                                # the failure mode (HTTP 401, timeout,
                                # WLED rejected the binary, …) and
                                # multiplying it by N attempts × M
                                # devices makes the log unreadable.
                                last_err = ex
                                logger.debug(
                                    "wled_upload_firmware attempt %d/%d failed for %s: %s",
                                    attempt, retries, addr, ex,
                                )
                                # Bail fast on deterministic device-side
                                # OTA failures. HTTP 500 = WLED's
                                # ``Update.write()`` failed (release-name
                                # mismatch, partition layout, chip
                                # variant, bad CRC, …); HTTP 503 = WLED
                                # busy / aborting after a previous
                                # 500. Retrying without changing
                                # parameters (the firmware binary, the
                                # ``skipValidation`` flag, etc.) just
                                # delays the failure by ``retries × 2``
                                # seconds.
                                err_msg = str(ex)
                                if "HTTP 500" in err_msg or "HTTP 503" in err_msg:
                                    break
                                time.sleep(2.0)
                        if not ok:
                            raise RuntimeError(f"Firmware upload failed: {last_err}")
                    dev_res["ok"] = True
                    logger.info("OTA %s: completed successfully", addr)
                except Exception as ex:
                    # Per-device failures are operator-actionable in the
                    # vast majority of cases (wrong password, polkit
                    # denied, WLED OTA lock / Same-network 401, …) and
                    # the formatted exception message already carries
                    # the diagnostic. WARNING is a single line; we
                    # deliberately do NOT print the traceback even at
                    # DEBUG, because RotorHazard typically runs at
                    # DEBUG level and a fleet OTA can hit the same
                    # expected failure mode for half the fleet.
                    dev_res["error"] = f"{type(ex).__name__}: {ex}"
                    results["errors"].append(f"{type(ex).__name__}: {ex}")
                    logger.warning("fw upload failed for %s: %s", addr, ex)
                    if stop_on_error:
                        raise
                finally:
                    try:
                        rl_instance.sendConfig(0x04, data0=0, recv3=self.ota.recv3_bytes_from_addr(str(addr)))
                    except Exception as ex:
                        # swallow-ok: per-device cleanup config send;
                        # workflow is already complete or aborting.
                        logger.debug("post-fw sendConfig failed for %s: %s", addr, ex)
        except Exception as ex:
            # swallow-ok: outer fallback for the per-device loop. The
            # ``stop_on_error=True`` raise above lands here; per-device
            # errors already populated results["errors"]. Single-line
            # WARNING with the exception message — no traceback,
            # matching the per-device path's quietness.
            results["ok"] = False
            logger.warning("firmware-update bulk aborted: %s", ex)
        finally:
            self._restore_host_wifi(
                results,
                host_wifi_restore=host_wifi_restore,
                host_wifi_initial=host_wifi_initial,
                ssid=last_connected_ssid,
            )

        return results
