"""Tests for extracted Flask-handler helpers (plan P2-4)."""

from __future__ import annotations

import threading
import types
import unittest
from unittest.mock import MagicMock

from racelink.web.api import (
    _apply_device_meta_updates,
    _prepare_discover_target,
    _resolve_special_config_request,
)


class _FakeGroupRepo:
    def __init__(self):
        self.items = []

    def append(self, group):
        self.items.append(group)
        return len(self.items) - 1


class _FakeRlInstance:
    def __init__(self, devices=None):
        self._devices = devices or {}
        self.set_group_calls = []

    def getDeviceFromAddress(self, mac):
        return self._devices.get(str(mac).upper())

    def setNodeGroupId(self, dev, **kwargs):
        self.set_group_calls.append((dev.addr, dev.groupId))


def _fake_ctx(*, devices=None, group_repo=None, rl_grouplist=None):
    class _Group:
        def __init__(self, name, static_group=0, dev_type=0):
            self.name = name
            self.static_group = static_group
            self.dev_type = dev_type

    ctx = types.SimpleNamespace(
        rl_lock=threading.RLock(),
        rl_instance=_FakeRlInstance(devices),
        RL_DeviceGroup=_Group,
        group_repo=group_repo,
        rl_grouplist=rl_grouplist,
        log=lambda _msg: None,
    )
    return ctx


class PrepareDiscoverTargetTests(unittest.TestCase):
    def test_creates_group_when_requested(self):
        repo = _FakeGroupRepo()
        ctx = _fake_ctx(group_repo=repo)
        target, created = _prepare_discover_target(ctx, target_gid=None, new_group_name="Heat 1")
        self.assertEqual(created, 0)
        self.assertEqual(target, 0)
        self.assertEqual(repo.items[0].name, "Heat 1")

    def test_preserves_explicit_target(self):
        repo = _FakeGroupRepo()
        ctx = _fake_ctx(group_repo=repo)
        target, created = _prepare_discover_target(ctx, target_gid=5, new_group_name=None)
        self.assertEqual(target, 5)
        self.assertIsNone(created)
        self.assertEqual(repo.items, [])

    def test_uses_list_fallback_without_group_repo(self):
        fallback = []
        ctx = _fake_ctx(group_repo=None, rl_grouplist=fallback)
        target, created = _prepare_discover_target(ctx, target_gid=None, new_group_name="List Group")
        self.assertEqual(created, 0)
        self.assertEqual(target, 0)
        self.assertEqual(len(fallback), 1)


class ApplyDeviceMetaUpdatesDoesNotHoldLockAcrossBlockingIO(unittest.TestCase):
    """Regression: bulk regroup must release ``ctx.rl_lock`` across
    ``setNodeGroupId`` so a concurrent reader thread (simulating USB-driven
    event dispatch) can acquire the same lock between iterations.

    Without the fix, ``_apply_device_meta_updates`` kept the lock over the
    entire ``for`` loop. In production this stalled the gateway-reader thread
    in ``handle_ack_event`` for device N while waiting for the web thread to
    complete its bulk, causing USB frames for devices N+1..K to queue up in
    pyserial and -- for device N+1 -- time out despite the ACK being present.
    """

    def test_reader_thread_can_acquire_lock_during_bulk(self):
        import time

        lock = threading.RLock()
        d1 = types.SimpleNamespace(addr="AA", name="a", groupId=0)
        d2 = types.SimpleNamespace(addr="BB", name="b", groupId=0)

        reader_acquisitions: list[float] = []
        reader_stop = threading.Event()

        def reader_thread():
            while not reader_stop.is_set():
                acquired = lock.acquire(timeout=0.05)
                if acquired:
                    reader_acquisitions.append(time.monotonic())
                    lock.release()
                time.sleep(0.01)

        class _SlowRlInstance:
            def __init__(self):
                self.set_group_calls: list = []

            def getDeviceFromAddress(self, mac):
                return {"AA": d1, "BB": d2}.get(mac)

            def setNodeGroupId(self, dev, **_kwargs):
                # Simulate the ~600 ms round-trip of a SET_GROUP + ACK.
                # During this sleep the reader thread must be able to
                # acquire ``lock`` at least once -- that is the deadlock the
                # fix targets.
                time.sleep(0.25)
                self.set_group_calls.append((dev.addr, dev.groupId))

        class _Group:
            def __init__(self, *_args, **_kwargs):
                pass

        ctx = types.SimpleNamespace(
            rl_lock=lock,
            rl_instance=_SlowRlInstance(),
            RL_DeviceGroup=_Group,
            group_repo=None,
            rl_grouplist=None,
            log=lambda _msg: None,
        )

        t = threading.Thread(target=reader_thread, daemon=True)
        t.start()
        try:
            changed = _apply_device_meta_updates(
                ctx, macs=["AA", "BB"], new_group=3, new_name=None
            )
        finally:
            reader_stop.set()
            t.join(timeout=1.0)

        self.assertEqual(changed, 2)
        self.assertEqual(d1.groupId, 3)
        self.assertEqual(d2.groupId, 3)
        # The reader must have acquired the lock during the ~500 ms of
        # blocking IO. A single acquisition proves the lock was released
        # between iterations.
        self.assertGreaterEqual(
            len(reader_acquisitions),
            1,
            "reader thread never acquired state lock during bulk -- deadlock regressed",
        )


class ApplyDeviceMetaUpdatesTests(unittest.TestCase):
    def test_rename_single_device(self):
        dev = types.SimpleNamespace(addr="AABBCC", name="old", groupId=0)
        ctx = _fake_ctx(devices={"AABBCC": dev})
        changed = _apply_device_meta_updates(ctx, macs=["AABBCC"], new_group=None, new_name="new-name")
        self.assertEqual(changed, 1)
        self.assertEqual(dev.name, "new-name")

    def test_regroup_multiple_devices(self):
        d1 = types.SimpleNamespace(addr="AA", name="a", groupId=0)
        d2 = types.SimpleNamespace(addr="BB", name="b", groupId=0)
        ctx = _fake_ctx(devices={"AA": d1, "BB": d2})
        changed = _apply_device_meta_updates(ctx, macs=["AA", "BB"], new_group=3, new_name=None)
        self.assertEqual(changed, 2)
        self.assertEqual(d1.groupId, 3)
        self.assertEqual(d2.groupId, 3)
        self.assertEqual(ctx.rl_instance.set_group_calls, [("AA", 3), ("BB", 3)])

    def test_rename_ignored_for_multi_selection(self):
        d1 = types.SimpleNamespace(addr="AA", name="a", groupId=0)
        d2 = types.SimpleNamespace(addr="BB", name="b", groupId=0)
        ctx = _fake_ctx(devices={"AA": d1, "BB": d2})
        changed = _apply_device_meta_updates(ctx, macs=["AA", "BB"], new_group=None, new_name="collision")
        self.assertEqual(changed, 0)
        self.assertEqual(d1.name, "a")


class ResolveSpecialConfigRequestTests(unittest.TestCase):
    def _specials_service(self, *, resolve=None, validate_raises=None):
        svc = MagicMock()
        svc.resolve_option.return_value = resolve
        if validate_raises:
            svc.validate_option_value.side_effect = validate_raises
        return svc

    def test_rejects_missing_fields(self):
        ctx = _fake_ctx()
        ok, payload, status = _resolve_special_config_request(ctx, {"mac": "AA"}, self._specials_service())
        self.assertFalse(ok)
        self.assertEqual(status, 400)
        self.assertIn("missing", payload["error"])

    def test_rejects_broadcast_mac(self):
        ctx = _fake_ctx()
        ok, payload, status = _resolve_special_config_request(
            ctx,
            {"mac": "FFFFFFFFFFFF", "key": "x", "value": 1},
            self._specials_service(),
        )
        self.assertFalse(ok)
        self.assertEqual(status, 400)
        self.assertIn("broadcast", payload["error"])

    def test_rejects_non_int_value(self):
        ctx = _fake_ctx()
        ok, payload, status = _resolve_special_config_request(
            ctx,
            {"mac": "AABBCCDDEEFF", "key": "x", "value": "not-an-int"},
            self._specials_service(),
        )
        self.assertFalse(ok)
        self.assertEqual(status, 400)

    def test_happy_path(self):
        dev = types.SimpleNamespace(addr="AABBCCDDEEFF")
        ctx = _fake_ctx(devices={"AABBCCDDEEFF": dev})
        svc = self._specials_service(resolve={"option": 0x42})
        ok, payload, status = _resolve_special_config_request(
            ctx,
            {"mac": "AABBCCDDEEFF", "key": "startblock_slots", "value": "3"},
            svc,
        )
        self.assertTrue(ok)
        self.assertEqual(status, 200)
        self.assertEqual(payload["option"], 0x42)
        self.assertEqual(payload["value_int"], 3)
        self.assertEqual(payload["mac_str"], "AABBCCDDEEFF")


if __name__ == "__main__":
    unittest.main()
