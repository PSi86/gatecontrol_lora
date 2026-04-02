from __future__ import annotations

import logging

from ...data import RL_Device

logger = logging.getLogger(__name__)


class ConfigService:
    def __init__(self, transport_coordinator, device_lookup):
        self._transport = transport_coordinator
        self._device_lookup = device_lookup
        self._pending_config = {}

    def send_config(
        self,
        option,
        data0=0,
        data1=0,
        data2=0,
        data3=0,
        recv3=b"\xFF\xFF\xFF",
        wait_for_ack: bool = False,
        timeout_s: float = 6.0,
    ):
        if not self._transport.ensure_ready("sendConfig"):
            return False if wait_for_ack else None
        recv3_hex = recv3.hex().upper() if isinstance(recv3, (bytes, bytearray)) else ""
        dev = None
        if recv3_hex and recv3_hex != "FFFFFF":
            self._pending_config[recv3_hex] = {"option": int(option) & 0xFF, "data0": int(data0) & 0xFF}
            dev = self._device_lookup.get_device_by_address(recv3_hex)
            if dev and wait_for_ack:
                dev.ack_clear()

        def _send():
            self._transport.lora.send_config(
                recv3=recv3,
                option=int(option) & 0xFF,
                data0=int(data0) & 0xFF,
                data1=int(data1) & 0xFF,
                data2=int(data2) & 0xFF,
                data3=int(data3) & 0xFF,
            )

        if wait_for_ack:
            if not dev:
                _send()
                return False
            events, _ = self._transport.send_and_wait_for_reply(recv3, self._transport.lp.OPC_CONFIG, _send, timeout_s=timeout_s)
            if not events:
                return False
            return bool(int(events[-1].get("ack_status", 1)) == 0)

        _send()
        return True

    @staticmethod
    def apply_config_update(dev: RL_Device, option: int, data0: int) -> None:
        bit_map = {0x01: 0, 0x03: 1, 0x04: 2}
        bit = bit_map.get(int(option))
        if bit is None:
            return
        mask = 1 << bit
        if int(data0):
            dev.configByte = int(dev.configByte) | mask
        else:
            dev.configByte = int(dev.configByte) & (~mask & 0xFF)
