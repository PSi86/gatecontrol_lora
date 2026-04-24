"""Status polling service for current device state via the gateway."""

from __future__ import annotations

import logging

from ..transport import LP, mac_last3_from_hex

logger = logging.getLogger(__name__)


class StatusService:
    def __init__(self, controller, gateway_service):
        self.controller = controller
        self.gateway_service = gateway_service

    @property
    def transport(self):
        return getattr(self.controller, "transport", None)

    def get_status(self, *, group_filter=255, target_device=None) -> dict:
        transport = self.transport
        if transport is None:
            logger.warning("getStatus: communicator not ready")
            return {"updated": 0, "responders": set(), "got_closed": False}

        self.gateway_service.install_transport_hooks()

        if target_device is None:
            recv3 = b"\xFF\xFF\xFF"
            group_id = int(group_filter) & 0xFF
            sender_filter = None
        else:
            recv3 = mac_last3_from_hex(target_device.addr)
            group_id = int(target_device.groupId) & 0xFF
            sender_filter = recv3.hex().upper()

        updated = 0
        responders = set()

        def _collect(ev: dict) -> bool:
            nonlocal updated
            try:
                if ev.get("opc") == LP.OPC_STATUS and ev.get("reply") == "STATUS_REPLY":
                    if sender_filter:
                        sender3 = ev.get("sender3")
                        if isinstance(sender3, (bytes, bytearray)) and bytes(sender3).hex().upper() != sender_filter:
                            return False
                    updated += 1
                    try:
                        mac6 = ev.get("mac6")
                        if isinstance(mac6, (bytes, bytearray)) and len(mac6) == 6:
                            responders.add(bytes(mac6).hex().upper())
                        else:
                            sender3 = ev.get("sender3")
                            if isinstance(sender3, (bytes, bytearray)) and len(sender3) == 3:
                                responders.add(bytes(sender3).hex().upper())
                    except Exception:
                        # swallow-ok: best-effort fallback; caller proceeds with safe default
                        pass
                    return True
            except Exception:
                # swallow-ok: best-effort fallback; caller proceeds with safe default
                pass
            return False

        try:
            transport.drain_events(0.0)
        except Exception:
            logger.debug("RaceLink: drain_events before get_status raised", exc_info=True)

        # Plan Phase C (revised): expected-count is known from the device
        # repository. Early-exit on count; otherwise 600 ms idle-timeout; max
        # ceiling scales with the target population.
        if target_device is not None:
            expected_count = 1
        elif group_filter == 255:
            expected_count = len(list(self.controller.device_repository.list()))
        else:
            expected_count = sum(
                1
                for dev in self.controller.device_repository.list()
                if int(getattr(dev, "groupId", 0)) == int(group_filter)
            )

        max_timeout_s = self.gateway_service.compute_collect_max_timeout(
            expected_count, ceiling_s=5.0
        )

        self.gateway_service.send_and_collect(
            lambda: transport.send_get_status(recv3=recv3, group_id=group_id, flags=0),
            _collect,
            expected=expected_count if expected_count > 0 else None,
            idle_timeout_s=0.6,
            max_timeout_s=max_timeout_s,
        )

        # After the collection window, any device in the targeted set that
        # did not respond is considered offline. This logic used to be gated
        # on ``got_closed`` -- now that the Host owns the clock, the window
        # is always "closed" by the time we reach here.
        if target_device is not None:
            if updated == 0:
                try:
                    target_device.mark_offline("Missing reply (STATUS)")
                except Exception:
                    # swallow-ok: best-effort fallback; caller proceeds with safe default
                    pass
        else:
            if group_filter == 255:
                targets = list(self.controller.device_repository.list())
            else:
                targets = [
                    dev
                    for dev in self.controller.device_repository.list()
                    if int(getattr(dev, "groupId", 0)) == int(group_filter)
                ]
            for dev in targets:
                try:
                    mac = (dev.addr or "").upper()
                    if not mac:
                        continue
                    if mac not in responders and mac[-6:] not in responders:
                        dev.mark_offline("Missing reply (STATUS)")
                except Exception:
                    # swallow-ok: best-effort fallback; caller proceeds with safe default
                    pass

        return {"updated": updated, "responders": responders, "got_closed": True}
