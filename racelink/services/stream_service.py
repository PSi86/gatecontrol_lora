"""Streaming payload service for RaceLink."""

from __future__ import annotations


class StreamService:
    def __init__(self, controller, gateway_service):
        self.controller = controller
        self.gateway_service = gateway_service

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
