"""Discovery service for device identification via the gateway.

Coordinates an OPC_DEVICES broadcast, collects ``IDENTIFY_REPLY``
events from any node that responds within the gateway's RX
window, and reconciles the replies into the device repository
(creating new ``RL_Device`` records, updating existing ones,
preserving operator-set name/groupId for already-known macs).

Public API:

* :meth:`DiscoveryService.discover_devices` — fire one OPC_DEVICES
  on a single ``group_filter`` value, drain the reply window,
  return ``{"found": N, ...}``. The default ``group_filter=255``
  is the historical wire fallback; the operator-facing default is
  ``group_filter=0`` (Unconfigured) and is set by the API caller —
  see ``docs/reference/broadcast-ruleset.md`` for the design rule.
* :meth:`DiscoveryService.discover_devices_in_groups` — fan-out
  helper that loops :meth:`discover_devices` once per group id and
  merges responders. Used by the Web UI's "Discover in: All
  groups" option to reach a fleet whose devices have been
  re-flashed / moved between gateways and may sit in any of the
  known groups. The future
  [group-agnostic re-identification](../../docs/roadmap.md)
  feature would replace the loop with a single packet.

Threading: typically driven by the task manager from a worker
thread (the operator clicks "Discover" → web route → task →
this service). The reply-collection path uses
:meth:`GatewayService.send_and_collect`, which installs a
listener on the transport for the duration of the window.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, List, Optional

from ..transport import LP, mac_last3_from_hex
from . import rf_timing

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
            idle_timeout_s=rf_timing.COLLECT_IDLE_TIMEOUT_S,
            max_timeout_s=rf_timing.COLLECT_MAX_CEILING_S,
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

    def discover_devices_in_groups(
        self,
        *,
        group_ids: Iterable[int],
        add_to_group: int = -1,
    ) -> dict:
        """Sweep discovery across multiple group filters.

        Calls :meth:`discover_devices` once per id in ``group_ids``,
        merges the responder sets, and returns the aggregated result.
        Sequential (one OPC_DEVICES + RX window per id) — matches the
        operator-initiated cadence of the discovery dialog. The future
        [group-agnostic re-identification](../../docs/roadmap.md)
        feature replaces this with a single packet.

        Returns the same shape as :meth:`discover_devices`: ``{"found":
        <total replies across all groups>, "responders": <merged
        set>, "assigned_group": <add_to_group if applied else None>}``.
        """
        merged_responders: set = set()
        total_found = 0
        last_assigned: Optional[int] = None
        for gid in group_ids:
            try:
                gid_int = int(gid)
            except (TypeError, ValueError):
                continue
            if not 0 <= gid_int <= 254:
                continue
            result = self.discover_devices(
                group_filter=gid_int,
                add_to_group=add_to_group,
            )
            total_found += int(result.get("found", 0) or 0)
            responders = result.get("responders") or set()
            merged_responders.update(responders)
            assigned = result.get("assigned_group")
            if assigned is not None:
                last_assigned = assigned
        return {
            "found": total_found,
            "responders": merged_responders,
            "assigned_group": last_assigned,
        }
