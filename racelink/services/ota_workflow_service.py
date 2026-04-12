"""Long-running OTA and presets workflows independent of Flask routes."""

from __future__ import annotations

import time


class OTAWorkflowService:
    def __init__(self, *, host_wifi_service, ota_service, presets_service):
        self.host_wifi = host_wifi_service
        self.ota = ota_service
        self.presets = presets_service

    def _restore_host_wifi(self, results, *, host_wifi_restore, host_wifi_initial, wifi_conn_name):
        if host_wifi_restore and (host_wifi_initial is False) and self.host_wifi.radio_enabled():
            try:
                try:
                    self.host_wifi.profile_down(wifi_conn_name, timeout_s=10.0)
                except Exception:
                    pass
                self.host_wifi.set_radio(False)
                results["hostWifi"]["enabled"] = False
                results["hostWifi"]["restored"] = True
            except Exception as ex:
                results["errors"].append(f"Host WiFi restore failed: {ex}")
                results["ok"] = False

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
        task_manager.update(
            meta={
                **meta,
                "stage": "CONNECT_WIFI",
                "message": f'Connecting host WiFi via profile "{wifi["conn_name"]}" (iface {wifi["iface"]}) to SSID "{wifi["ssid"]}"',
            }
        )
        try:
            self.host_wifi.connect_profile(
                wifi["conn_name"],
                wifi["ssid"],
                iface=wifi["iface"],
                bssid=wifi["bssid"],
                timeout_s=wifi["timeout_s"],
            )
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
                self.host_wifi.connect_profile(
                    wifi["conn_name"],
                    wifi["ssid"],
                    iface=wifi["iface"],
                    bssid=wifi["bssid"],
                    timeout_s=wifi["timeout_s"],
                )
                return True
            raise
        return host_wifi_changed

    def download_presets(self, *, rl_instance, task_manager, mac: str, base_url: str, wifi: dict, host_wifi_enable: bool, host_wifi_restore: bool):
        results = {"ok": True, "baseUrl": base_url, "addr": mac, "file": None, "errors": []}
        host_wifi_initial = self.host_wifi.radio_enabled()
        results["hostWifi"] = {"wasEnabled": host_wifi_initial, "enabled": host_wifi_initial, "restored": False}

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

            host_wifi_changed = self._connect_wled_wifi(
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
            except Exception:
                pass
        except Exception as ex:
            results["ok"] = False
            results["errors"].append(str(ex))
        finally:
            self._restore_host_wifi(
                results,
                host_wifi_restore=host_wifi_restore,
                host_wifi_initial=host_wifi_initial,
                wifi_conn_name=wifi["conn_name"],
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
    ):
        results = {"ok": True, "baseUrl": base_url, "devices": [], "errors": []}
        host_wifi_initial = self.host_wifi.radio_enabled()
        results["hostWifi"] = {"wasEnabled": host_wifi_initial, "enabled": host_wifi_initial, "restored": False}
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
                    task_manager.update(meta={"stage": "RACELINK_AP_ON", "index": idx, "total": total, "addr": addr, "retries": retries, "message": "Enable WLED AP via RaceLink"})
                    rl_instance.sendConfig(0x04, data0=1, recv3=self.ota.recv3_bytes_from_addr(str(addr)))
                    self._connect_wled_wifi(
                        task_manager,
                        wifi=wifi,
                        host_wifi_enable=host_wifi_enable,
                        host_wifi_changed=results["hostWifi"]["enabled"] and not host_wifi_initial,
                        results=results,
                        meta={"index": idx, "total": total, "addr": addr, "retries": retries},
                    )
                    task_manager.update(meta={"stage": "WAIT_HTTP", "index": idx, "total": total, "addr": addr, "retries": retries, "message": f"Waiting for WLED /json/info mac to match {expected_mac}"})
                    info = self.ota.wait_for_expected_node(base_url, expected_mac, timeout_s=90.0, poll_s=1.0)
                    if not info:
                        raise RuntimeError(f"Timeout waiting for node (baseUrl={base_url}) to report expected mac {expected_mac}")
                    dev_res["info_before"] = {k: info.get(k) for k in ("mac", "ver", "arch", "name")}

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
                                self.ota.wled_upload_firmware(base_url, fw_info["path"], timeout_s=30.0)
                                ok = True
                                break
                            except Exception as ex:
                                last_err = ex
                                time.sleep(2.0)
                        if not ok:
                            raise RuntimeError(f"Firmware upload failed: {last_err}")
                    dev_res["ok"] = True
                except Exception as ex:
                    dev_res["error"] = str(ex)
                    results["errors"].append(str(ex))
                    if stop_on_error:
                        raise
                finally:
                    try:
                        rl_instance.sendConfig(0x04, data0=0, recv3=self.ota.recv3_bytes_from_addr(str(addr)))
                    except Exception:
                        pass
        except Exception:
            results["ok"] = False
        finally:
            self._restore_host_wifi(
                results,
                host_wifi_restore=host_wifi_restore,
                host_wifi_initial=host_wifi_initial,
                wifi_conn_name=wifi["conn_name"],
            )

        return results
