"""Configuration command service for RaceLink devices.

Owns the *post-ACK* application of configuration changes: when a
unicast ``OPC_CONFIG`` send is acknowledged, the matching
``apply_config_update(dev, option, data0)`` call lands here. The
service mutates the device's local state (configByte / specials)
to reflect what the firmware just confirmed, then triggers an
SSE refresh so the WebUI picks up the change.

Public API:

* ``send_config(...)`` — emit one OPC_CONFIG packet via the
  gateway service. **Always unicast.** See
  :meth:`ConfigService.send_config` for the OPC_CONFIG broadcast
  design rule.
* ``apply_config_update(dev, option, data0)`` — invoked from
  :meth:`GatewayService.handle_ack_event` via the controller's
  ``_apply_config_update`` shim; pre-A3 this read the pending-
  config dict directly, post-A3 it goes through
  ``controller.take_pending_config``.

Threading: the apply call lands on the RX reader thread (via the
ACK handler). Mutations to the device state happen under the
state-repository lock if the controller exposes one.
"""

from __future__ import annotations

from typing import Optional

from . import rf_timing


class ConfigService:
    def __init__(self, controller, gateway_service):
        self.controller = controller
        self.gateway_service = gateway_service

    def send_config(
        self,
        option,
        data0=0,
        data1=0,
        data2=0,
        data3=0,
        recv3=b"\xFF\xFF\xFF",
        wait_for_ack: bool = False,
        timeout_s: Optional[float] = None,
    ):
        """Emit one OPC_CONFIG packet via the gateway service.

        **OPC_CONFIG cannot be broadcast — by design.** Different
        device classes (WLED, Startblock, future capabilities) can
        reinterpret the same config-register address according to
        their capability, so a global broadcast would collide. The
        WLED firmware enforces this at the receiver: any OPC_CONFIG
        with ``recv3 == FFFFFF`` is rejected before the option
        handler runs (see ``RaceLink_WLED/src/racelink_wled.cpp``
        and the [Broadcast Ruleset]
        (../../../RaceLink_Docs/docs/reference/broadcast-ruleset.md)
        — the OPC_CONFIG row + "Designed-in special cases" section).
        The Web API ``/api/config`` route enforces the same rule at
        the boundary: a broadcast ``recv3`` returns 400 with
        "broadcast not allowed for config".

        The ``recv3`` parameter therefore must be passed by every
        caller as a concrete 3-byte device address. The default
        ``b"\\xFF\\xFF\\xFF"`` exists only as a defensive sentinel —
        if it ever reaches the wire, the firmware drops the packet
        (and the operator sees the action as a silent no-op). It is
        not a "broadcast me by default" feature.
        """
        if timeout_s is None:
            timeout_s = rf_timing.UNICAST_ATTEMPT_TIMEOUT_S
        return self.gateway_service.send_config(
            option,
            data0=data0,
            data1=data1,
            data2=data2,
            data3=data3,
            recv3=recv3,
            wait_for_ack=wait_for_ack,
            timeout_s=timeout_s,
        )

    def apply_config_update(self, dev, option: int, data0: int) -> None:
        bit_map = {
            0x01: 0,
            0x03: 1,
            0x04: 2,
        }
        bit = bit_map.get(int(option))
        if bit is None:
            return
        mask = 1 << bit
        if int(data0):
            dev.configByte = int(dev.configByte) | mask
        else:
            dev.configByte = int(dev.configByte) & (~mask & 0xFF)
