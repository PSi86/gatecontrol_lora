"""Configuration command service for RaceLink devices.

Owns the *post-ACK* application of configuration changes: when a
unicast ``OPC_CONFIG`` send is acknowledged, the matching
``apply_config_update(dev, option, data0)`` call lands here. The
service mutates the device's local state (configByte / specials)
to reflect what the firmware just confirmed, then triggers an
SSE refresh so the WebUI picks up the change.

Public API:

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
