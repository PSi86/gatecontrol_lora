"""Sync command service for RaceLink devices."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class SyncService:
    def __init__(self, controller, gateway_service):
        self.controller = controller
        self.gateway_service = gateway_service

    @property
    def transport(self):
        return getattr(self.controller, "transport", None)

    def send_sync(self, ts24, brightness, recv3=b"\xFF\xFF\xFF"):
        if not self.transport:
            logger.warning("sendSync: communicator not ready")
            return
        self.gateway_service.send_sync(ts24, brightness, recv3=recv3)
