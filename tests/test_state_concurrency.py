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

    # A3: real-controller-shaped pending-config helpers backed by a
    # ``threading.Lock`` so the regression test below can race them.
    def __post_init__(self):  # pragma: no cover - kept for documentation
        pass

    @property
    def _pending_config_lock(self):
        # Lazy-init so existing tests that build the fake without
        # touching pending_config keep working unchanged.
        lock = self.__dict__.get("_pcfg_lock")
        if lock is None:
            lock = threading.Lock()
            self.__dict__["_pcfg_lock"] = lock
        return lock

    def stash_pending_config(self, recv3_hex: str, option: int, data0: int) -> None:
        with self._pending_config_lock:
            self._pending_config[recv3_hex] = {
                "option": int(option) & 0xFF,
                "data0": int(data0) & 0xFF,
            }

    def take_pending_config(self, recv3_hex: str):
        with self._pending_config_lock:
            return self._pending_config.pop(recv3_hex, None)

    # A5: same lock-on-the-fake pattern for _pending_expect so the
    # cross-thread regression test below can race set/read/clear.
    @property
    def _pending_expect_lock(self):
        lock = self.__dict__.get("_pexp_lock")
        if lock is None:
            lock = threading.Lock()
            self.__dict__["_pexp_lock"] = lock
        return lock

    def set_pending_expect(self, dev, rule, opcode7, sender_last3, ts):
        with self._pending_expect_lock:
            self._pending_expect = {
                "dev": dev,
                "rule": rule,
                "opcode7": int(opcode7),
                "sender_last3": str(sender_last3 or "").upper(),
                "ts": float(ts),
            }

    def read_pending_expect(self):
        with self._pending_expect_lock:
            return self._pending_expect

    def clear_pending_expect_if(self, expected) -> bool:
        with self._pending_expect_lock:
            if self._pending_expect is expected:
                self._pending_expect = None
                return True
            return False

    def clear_pending_expect(self) -> None:
        with self._pending_expect_lock:
            self._pending_expect = None


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

    def test_pending_config_stash_take_is_thread_safe(self):
        """A3: stashing from the TX/web thread while popping from the RX
        thread must not lose updates or raise. The unprotected dict
        previously could ``RuntimeError`` on iteration or silently lose
        a write+pop pair on the same key."""
        controller = LockAwareController()

        N = 500
        observed: list[dict | None] = []
        observed_lock = threading.Lock()

        def stasher():
            for i in range(N):
                controller.stash_pending_config(f"DDEE{i & 0xFFFF:04X}", 0x04, i & 0xFF)

        def popper():
            for i in range(N):
                got = controller.take_pending_config(f"DDEE{i & 0xFFFF:04X}")
                with observed_lock:
                    observed.append(got)

        t1 = threading.Thread(target=stasher)
        t2 = threading.Thread(target=popper)
        t1.start(); t2.start()
        t1.join(); t2.join()

        # Every stash must have been popped exactly once; any popper-
        # before-stasher race shows up as ``None`` so we drain the
        # leftovers afterwards. The combined set must be size N.
        for i in range(N):
            leftover = controller.take_pending_config(f"DDEE{i & 0xFFFF:04X}")
            if leftover is not None:
                observed.append(leftover)
        non_none = [x for x in observed if x is not None]
        self.assertEqual(len(non_none), N)

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

    def test_pending_expect_compare_and_clear_preserves_fresh_stamp(self):
        """A5 invariant: the RX-thread's CAS-clear must NOT wipe an
        expectation that the TX-thread freshly stamped after the RX
        snapshot. Without compare-and-clear the lost-update would
        silently drop a newly-tracked unicast request."""
        controller = LockAwareController()

        rule_old = type("Rule", (), {"name": "OLD"})()
        rule_new = type("Rule", (), {"name": "NEW"})()

        controller.set_pending_expect(controller.dev, rule_old, 0x10, "AABBCC", 1.0)

        # RX-thread snapshot of the OLD expectation.
        p_old = controller.read_pending_expect()
        self.assertIs(p_old["rule"], rule_old)

        # Simulate a TX-thread restamp BEFORE the RX-thread tries its
        # clear. With CAS this restamp survives.
        controller.set_pending_expect(controller.dev, rule_new, 0x20, "DDEEFF", 2.0)

        # The RX matcher's compare-and-clear must NOT clear here — the
        # stored expectation has changed identity since p_old was read.
        cleared = controller.clear_pending_expect_if(p_old)
        self.assertFalse(cleared,
                         "CAS-clear must refuse when the expectation has been restamped")

        # And the freshly-stamped expectation is still present.
        p_now = controller.read_pending_expect()
        self.assertIsNotNone(p_now)
        self.assertIs(p_now["rule"], rule_new)
        self.assertEqual(p_now["opcode7"], 0x20)

    def test_pending_expect_set_and_clear_under_concurrent_threads(self):
        """A5 stress: TX thread re-stamps continuously while the RX
        thread reads + CAS-clears. Verify no exception is raised and
        the final state is internally consistent."""
        controller = LockAwareController()
        rule = type("Rule", (), {"name": "X"})()

        errors: list[Exception] = []

        def tx_stamper():
            try:
                for i in range(500):
                    controller.set_pending_expect(
                        controller.dev, rule, 0x10, "AABBCC", float(i),
                    )
            except Exception as e:
                errors.append(e)

        def rx_matcher():
            try:
                for _ in range(500):
                    p = controller.read_pending_expect()
                    if p is not None:
                        controller.clear_pending_expect_if(p)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=tx_stamper)
        t2 = threading.Thread(target=rx_matcher)
        t1.start(); t2.start()
        t1.join(); t2.join()

        self.assertEqual(errors, [], f"concurrent set/clear raised: {errors}")
        # Final state: either the TX winner is still stamped, or the RX
        # cleared the very last one. Both are valid; the invariant is
        # "no crash, no torn dict".
        final = controller.read_pending_expect()
        if final is not None:
            self.assertIs(final["rule"], rule)
            self.assertEqual(final["opcode7"], 0x10)

    def test_locked_device_repo_iteration_does_not_raise_under_concurrent_mutation(self):
        """A6: iterating ``device_repository.list()`` under
        ``state_repository.lock`` must remain safe while another thread
        appends/pops under the same (re-entrant) lock."""
        controller = LockAwareController()
        repo = controller.device_repository

        with controller.state_repository.lock:
            for i in range(20):
                repo.append(RL_Device(f"AAAA{i:08X}", 1, f"Seed{i}", groupId=3))

        errors: list[Exception] = []

        def reader():
            try:
                for _ in range(200):
                    with controller.state_repository.lock:
                        for dev in repo.list():
                            _ = (dev.addr, dev.groupId, dev.flags)
            except Exception as e:
                errors.append(e)

        def writer():
            try:
                for i in range(200):
                    with controller.state_repository.lock:
                        repo.append(RL_Device(f"BBBB{i:08X}", 1, "X", groupId=4))
                        if len(repo.list()) > 30:
                            repo.list().pop()
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=reader)
        t2 = threading.Thread(target=writer)
        t1.start(); t2.start()
        t1.join(); t2.join()

        self.assertEqual(errors, [],
                         f"locked iteration raised under concurrent mutation: {errors}")


if __name__ == "__main__":
    unittest.main()
