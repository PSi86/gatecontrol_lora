"""Discovery service for device identification via the gateway."""

from __future__ import annotations

import logging

from ..transport import LP, mac_last3_from_hex

logger = logging.getLogger(__name__)


class DiscoveryService:
    def __init__(self, controller, gateway_service):
        self.controller = controller
        self.gateway_service = gateway_service

    @property
    def transport(self):
        return getattr(self.controller, "transport", None)

    def discover_devices(self, *, group_filter=255, target_device=None, add_to_group=-1) -> dict:
        transport = self.transport
        if transport is None:
            logger.warning("getDevices: communicator not ready")
            return {"found": 0, "responders": set(), "assigned_group": None}

        self.gateway_service.install_transport_hooks()

        if target_device is None:
            recv3 = b"\xFF\xFF\xFF"
            group_id = int(group_filter) & 0xFF
        else:
            recv3 = mac_last3_from_hex(target_device.addr)
            group_id = int(target_device.groupId) & 0xFF

        found = 0
        responders = set()

        def _collect(ev: dict) -> bool:
            nonlocal found
            try:
                if ev.get("opc") == LP.OPC_DEVICES and ev.get("reply") == "IDENTIFY_REPLY":
                    found += 1
                    mac6 = ev.get("mac6")
                    if isinstance(mac6, (bytes, bytearray)) and len(mac6) == 6:
                        responders.add(bytes(mac6).hex().upper())
                    else:
                        sender3 = ev.get("sender3")
                        sender_hex = self.controller._to_hex_str(sender3)
                        if sender_hex:
                            responders.add(sender_hex.upper())
                    return True
            except Exception:
                # swallow-ok: best-effort fallback; caller proceeds with safe default
                pass
            return False

        logger.debug("GET_DEVICES -> recv3=%s group=%d flags=%d", recv3.hex().upper(), group_id, 0)

        try:
            transport.drain_events(0.0)
        except Exception:
            logger.debug("RaceLink: drain_events before discover raised", exc_info=True)

        # Plan Phase C (revised): GET_DEVICES is the one call where the
        # responder count is genuinely unknown (a fresh device could answer),
        # so we keep the hard ceiling at 5 s. Idle-based termination still
        # lets us return early once the last late-comer has gone quiet for
        # 600 ms.
        self.gateway_service.send_and_collect(
            lambda: transport.send_get_devices(recv3=recv3, group_id=group_id, flags=0),
            _collect,
            idle_timeout_s=0.6,
            max_timeout_s=5.0,
        )

        assigned_group = None
        if add_to_group > 0 and add_to_group < 255:
            assigned_group = int(add_to_group)
            for addr in responders:
                dev = self.controller.getDeviceFromAddress(addr)
                if not dev:
                    continue
                dev.groupId = assigned_group
                self.controller.setNodeGroupId(dev)

        return {"found": found, "responders": responders, "assigned_group": assigned_group}
