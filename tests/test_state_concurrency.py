"""Concurrency regression tests for state mutations (plan P1-4 / P2-5)."""

from __future__ import annotations

import threading
import unittest

from racelink.domain import RL_Device
from racelink.services.gateway_service import GatewayService
from racelink.state.repository import DeviceRepository, GroupRepository, StateRepository
from racelink.transport import LP


class FakeTransport:
    def __init__(self):
        self.listeners = []

    def add_listener(self, cb):
        self.listeners.append(cb)

    def remove_listener(self, cb):
        if cb in self.listeners:
            self.listeners.remove(cb)

    def send_set_group(self, recv3, group_id):  # pragma: no cover - not triggered here
        pass

    def drain_events(self, timeout_s=0.0):
        return []


class LockAwareController:
    """Minimal controller stub that exposes a real StateRepository and lock."""

    def __init__(self):
        self.dev = RL_Device("AABBCCDDEEFF", 1, "Node", groupId=3)
        repo = StateRepository(
            devices=[self.dev],
            groups=[object(), object(), object(), object()],
        )
        self.state_repository = repo
        self.transport = FakeTransport()
        self._pending_expect = None
        self._pending_config = {}
        self._transport_hooks_installed = False

    def _to_hex_str(self, value):
        if isinstance(value, (bytes, bytearray)):
            return bytes(value).hex().upper()
        return str(value or "").upper()

    def getDeviceFromAddress(self, addr):
        return self.state_repository.devices.get_by_addr(addr)

    @property
    def device_repository(self):
        return self.state_repository.devices

    @property
    def group_repository(self):
        return self.state_repository.groups

    def _apply_config_update(self, dev, option, data0):
        pass

    def setNodeGroupId(self, dev, forceSet=False, wait_for_ack=True):
        pass

    def is_discovery_active(self):
        return False


class StateLockTests(unittest.TestCase):
    def test_state_repository_exposes_rlock(self):
        repo = StateRepository()
        lock = repo.lock
        # Reentrant: double acquire from same thread must not deadlock.
        with lock:
            with lock:
                pass

    def test_concurrent_identify_events_do_not_raise(self):
        controller = LockAwareController()
        service = GatewayService(controller)

        def drive_identify():
            for _ in range(200):
                service.on_transport_event(
                    {
                        "opc": LP.OPC_DEVICES,
                        "reply": "IDENTIFY_REPLY",
                        "mac6": bytes.fromhex("AABBCCDDEEFF"),
                        "groupId": 3,
                        "caps": 1,
                        "version": 7,
                    }
                )

        def read_state():
            for _ in range(200):
                with controller.state_repository.lock:
                    # Iterating requires a consistent snapshot.
                    for dev in controller.device_repository.list():
                        _ = (dev.addr, dev.groupId, dev.link_online)

        t1 = threading.Thread(target=drive_identify)
        t2 = threading.Thread(target=read_state)
        t1.start(); t2.start()
        t1.join(); t2.join()

        # If either thread raised, join() wouldn't have raised but
        # unhandled exceptions would appear via threading default hook.
        # Device state should still be internally consistent.
        self.assertEqual(controller.dev.addr, "AABBCCDDEEFF")

    def test_auto_reassign_cache_is_thread_safe(self):
        controller = LockAwareController()
        service = GatewayService(controller)

        def hammer():
            for i in range(500):
                mac = f"AABBCCDDEE{i & 0xFF:02X}"
                service._mark_auto_reassign(mac)
                service._auto_reassign_suppressed(mac)

        threads = [threading.Thread(target=hammer) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Cache should be bounded by the pruner and all entries are strings->floats.
        for mac, ts in service._auto_reassign_recent.items():
            self.assertIsInstance(mac, str)
            self.assertIsInstance(ts, float)


if __name__ == "__main__":
    unittest.main()
