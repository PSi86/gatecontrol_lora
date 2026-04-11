"""Streaming payload service for RaceLink."""

from __future__ import annotations


class StreamService:
    def __init__(self, controller, gateway_service):
        self.controller = controller
        self.gateway_service = gateway_service

    @staticmethod
    def build_ctrl(start: bool, stop: bool, packets_left: int) -> int:
        ctrl = (0x80 if start else 0x00) | (0x40 if stop else 0x00)
        return ctrl | (int(packets_left) & 0x3F)

    def send_stream(
        self,
        payload: bytes,
        groupId=None,
        device=None,
        retries: int = 2,
        timeout_s: float = 8.0,
    ) -> dict[str, int]:
        return self.gateway_service.send_stream(
            payload,
            groupId=groupId,
            device=device,
            retries=retries,
            timeout_s=timeout_s,
        )
