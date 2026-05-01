"""Tests for extracted Flask-handler helpers (plan P2-4)."""

from __future__ import annotations

import threading
import types
import unittest
from unittest.mock import MagicMock

from racelink.web.api import (
    _apply_device_meta_updates,
    _iterate_force_groups,
    _prepare_discover_target,
    _resolve_special_config_request,
)


class _FakeGroupRepo:
    def __init__(self):
        self.items = []

    def append(self, group):
        self.items.append(group)
        return len(self.items) - 1


class _FakeRepo:
    """Minimal duck-type for ``DeviceRepository`` / ``GroupRepository``.

    The production path calls ``.list()`` to enumerate items. The
    helper extracted in 2026-04-29 (``_iterate_force_groups``)
    relies on this method existing on both repos; the bulk-set
    helper does not.
    """

    def __init__(self, items=None):
        self._items = list(items or [])

    def list(self):
        return list(self._items)


class _FakeRlInstance:
    def __init__(self, devices=None, *, set_group_returns=True, groups=None):
        self._devices = devices or {}
        self.set_group_calls = []
        self._set_group_returns = set_group_returns
        # Repos exposed via ``.device_repository`` / ``.group_repository``
        # for the rf-timing batch's ``_iterate_force_groups`` helper.
        # Tests that exercise ``_apply_device_meta_updates`` ignore
        # them; tests that exercise the force-groups iterator pass
        # the device list explicitly via the constructor.
        device_list = list(self._devices.values()) if isinstance(self._devices, dict) else list(self._devices)
        self.device_repository = _FakeRepo(device_list)
        self.group_repository = _FakeRepo(groups if groups is not None else [object(), object(), object()])

    def getDeviceFromAddress(self, mac):
        return self._devices.get(str(mac).upper())

    def setNodeGroupId(self, dev, **kwargs):
        self.set_group_calls.append((dev.addr, dev.groupId))
        # Mirror the production return contract (controller.py
        # 2026-04-29): ``True`` on ACK, ``False`` on timeout. The
        # production path also calls ``dev.mark_offline`` on timeout;
        # tests that exercise that branch can pass ``set_group_returns=False``.
        if not self._set_group_returns and hasattr(dev, "mark_offline"):
            dev.mark_offline("Missing reply (SET_GROUP)")
        return self._set_group_returns


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
        # ``link_online=True`` is critical: as of 2026-04-29 the bulk
        # path skips the wire send for offline devices entirely. To
        # exercise the locking-discipline regression we need devices
        # that actually trigger the blocking ``setNodeGroupId`` call.
        d1 = types.SimpleNamespace(addr="AA", name="a", groupId=0, link_online=True)
        d2 = types.SimpleNamespace(addr="BB", name="b", groupId=0, link_online=True)

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
                return True

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
            outcome = _apply_device_meta_updates(
                ctx, macs=["AA", "BB"], new_group=3, new_name=None
            )
        finally:
            reader_stop.set()
            t.join(timeout=1.0)

        # ``_apply_device_meta_updates`` now returns a dict (2026-04-29);
        # both devices acked so ``changed == 2`` and nothing was skipped.
        self.assertEqual(outcome["changed"], 2)
        self.assertEqual(outcome["skipped_offline"], 0)
        self.assertEqual(outcome["timed_out"], 0)
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
        dev = types.SimpleNamespace(addr="AABBCC", name="old", groupId=0, link_online=True)
        ctx = _fake_ctx(devices={"AABBCC": dev})
        outcome = _apply_device_meta_updates(ctx, macs=["AABBCC"], new_group=None, new_name="new-name")
        self.assertEqual(outcome["changed"], 1)
        self.assertEqual(dev.name, "new-name")

    def test_regroup_multiple_devices(self):
        d1 = types.SimpleNamespace(addr="AA", name="a", groupId=0, link_online=True)
        d2 = types.SimpleNamespace(addr="BB", name="b", groupId=0, link_online=True)
        ctx = _fake_ctx(devices={"AA": d1, "BB": d2})
        outcome = _apply_device_meta_updates(ctx, macs=["AA", "BB"], new_group=3, new_name=None)
        self.assertEqual(outcome["changed"], 2)
        self.assertEqual(outcome["skipped_offline"], 0)
        self.assertEqual(outcome["timed_out"], 0)
        self.assertEqual(d1.groupId, 3)
        self.assertEqual(d2.groupId, 3)
        self.assertEqual(ctx.rl_instance.set_group_calls, [("AA", 3), ("BB", 3)])

    def test_rename_ignored_for_multi_selection(self):
        d1 = types.SimpleNamespace(addr="AA", name="a", groupId=0, link_online=True)
        d2 = types.SimpleNamespace(addr="BB", name="b", groupId=0, link_online=True)
        ctx = _fake_ctx(devices={"AA": d1, "BB": d2})
        outcome = _apply_device_meta_updates(ctx, macs=["AA", "BB"], new_group=None, new_name="collision")
        self.assertEqual(outcome["changed"], 0)
        self.assertEqual(d1.name, "a")

    def test_offline_devices_skip_set_group_send(self):
        """2026-04-29: bulk path skips the SET_GROUP wire send for
        already-offline devices. Host-side groupId still updates;
        the auto-restore mechanism pushes the new value when the
        device next comes back online."""
        d1 = types.SimpleNamespace(addr="AA", name="a", groupId=0, link_online=False)
        d2 = types.SimpleNamespace(addr="BB", name="b", groupId=0, link_online=False)
        ctx = _fake_ctx(devices={"AA": d1, "BB": d2})
        outcome = _apply_device_meta_updates(ctx, macs=["AA", "BB"], new_group=5, new_name=None)
        # No setNodeGroupId calls fired — that's the win (eliminates
        # the 8 s per-offline-device timeout the operator used to
        # stare at a frozen UI for).
        self.assertEqual(ctx.rl_instance.set_group_calls, [])
        # In-memory groupId still updated.
        self.assertEqual(d1.groupId, 5)
        self.assertEqual(d2.groupId, 5)
        # Counts surface the breakdown for the operator-facing toast.
        self.assertEqual(outcome["changed"], 0)
        self.assertEqual(outcome["skipped_offline"], 2)
        self.assertEqual(outcome["timed_out"], 0)
        self.assertEqual(outcome["total"], 2)

    def test_mixed_online_offline_only_online_sent(self):
        """Mixed selection: only the online device gets a SET_GROUP
        sent; the offline one is skipped."""
        d_on = types.SimpleNamespace(addr="ON", name="a", groupId=0, link_online=True)
        d_off = types.SimpleNamespace(addr="OFF", name="b", groupId=0, link_online=False)
        ctx = _fake_ctx(devices={"ON": d_on, "OFF": d_off})
        outcome = _apply_device_meta_updates(ctx, macs=["ON", "OFF"], new_group=7, new_name=None)
        self.assertEqual(ctx.rl_instance.set_group_calls, [("ON", 7)])
        self.assertEqual(d_on.groupId, 7)
        self.assertEqual(d_off.groupId, 7)
        self.assertEqual(outcome["changed"], 1)
        self.assertEqual(outcome["skipped_offline"], 1)

    def test_set_group_timeout_counted_and_marks_offline(self):
        """When ``setNodeGroupId`` returns False (timeout), the
        device is counted as ``timed_out`` and the production path's
        ``mark_offline`` flips ``link_online`` to False."""
        offline_calls: list[str] = []

        def _mark_offline(reason):
            offline_calls.append(reason)
            dev.link_online = False

        dev = types.SimpleNamespace(
            addr="AA", name="a", groupId=0, link_online=True,
            mark_offline=_mark_offline,
        )
        ctx = _fake_ctx(devices={"AA": dev})
        ctx.rl_instance = _FakeRlInstance({"AA": dev}, set_group_returns=False)
        outcome = _apply_device_meta_updates(ctx, macs=["AA"], new_group=4, new_name=None)
        self.assertEqual(outcome["timed_out"], 1)
        self.assertEqual(outcome["changed"], 0)
        # ``setNodeGroupId`` was called (was online at start) and the
        # fake's timeout-path mark_offline flipped the flag.
        self.assertEqual(offline_calls, ["Missing reply (SET_GROUP)"])
        self.assertFalse(dev.link_online)


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


class IterateForceGroupsTests(unittest.TestCase):
    """rf-timing batch (2026-04-29): the ``_iterate_force_groups``
    helper is the sibling of ``_apply_device_meta_updates`` for the
    "Re-sync group config" flow.

    Same skip-offline + tally-result contract as the bulk-set helper,
    but iterates the device repository directly (no per-mac selection)
    and re-pushes each device's *existing* groupId rather than
    mutating it. The ``sanity_check=True`` path also clamps any
    out-of-range groupId back to 0.
    """

    def test_default_includes_offline_devices(self):
        """Default ``skip_offline=False``: offline devices still get
        SET_GROUP sent. Re-sync's operator semantic is "push to ALL"
        so the offline ones are *not* skipped by default. Operators
        opt into the skip behaviour via the WebUI checkbox.

        With the fake's default ``set_group_returns=True`` (mock
        ACK), offline devices count as ``changed`` because the fake
        doesn't model the real per-device timeout — the unit test's
        job here is just to confirm the wire send fires.
        """
        d1 = types.SimpleNamespace(addr="AA", groupId=1, link_online=False)
        d2 = types.SimpleNamespace(addr="BB", groupId=2, link_online=False)
        ctx = _fake_ctx(devices={"AA": d1, "BB": d2})
        outcome = _iterate_force_groups(ctx)
        self.assertEqual(
            ctx.rl_instance.set_group_calls, [("AA", 1), ("BB", 2)],
            "default-include path must dispatch SET_GROUP for offline devices",
        )
        self.assertEqual(outcome["skipped_offline"], 0)
        self.assertEqual(outcome["total"], 2)

    def test_skip_offline_true_skips_offline_devices(self):
        """Operator opt-in: ``skip_offline=True`` keeps offline
        devices off the wire (auto-restore handles them on next
        reply)."""
        d1 = types.SimpleNamespace(addr="AA", groupId=1, link_online=False)
        d2 = types.SimpleNamespace(addr="BB", groupId=2, link_online=False)
        ctx = _fake_ctx(devices={"AA": d1, "BB": d2})
        outcome = _iterate_force_groups(ctx, skip_offline=True)
        self.assertEqual(ctx.rl_instance.set_group_calls, [],
                         "skip_offline=True: setNodeGroupId should not fire")
        self.assertEqual(outcome["changed"], 0)
        self.assertEqual(outcome["skipped_offline"], 2)
        self.assertEqual(outcome["timed_out"], 0)
        self.assertEqual(outcome["total"], 2)

    def test_online_devices_get_set_group_with_existing_id(self):
        """Online devices fire SET_GROUP with their *current* groupId.

        Distinct from bulk-set which mutates groupId from operator
        input — force_groups re-pushes whatever's already stored.
        """
        d1 = types.SimpleNamespace(addr="AA", groupId=1, link_online=True)
        d2 = types.SimpleNamespace(addr="BB", groupId=2, link_online=True)
        ctx = _fake_ctx(devices={"AA": d1, "BB": d2})
        outcome = _iterate_force_groups(ctx)
        self.assertEqual(ctx.rl_instance.set_group_calls, [("AA", 1), ("BB", 2)])
        self.assertEqual(outcome["changed"], 2)
        self.assertEqual(outcome["skipped_offline"], 0)

    def test_sanity_check_clamps_out_of_range_groupid_to_zero(self):
        """Sanity check: a device whose stored groupId points at a
        deleted group is clamped back to 0 (Unconfigured) before
        the SET_GROUP fires. Mirrors the legacy ``forceGroups`` path."""
        # Fixture has 3 groups (default _FakeRlInstance.groups). A
        # device with groupId=5 is out of range and must be clamped.
        d_bad = types.SimpleNamespace(addr="AA", groupId=5, link_online=True)
        ctx = _fake_ctx(devices={"AA": d_bad})
        outcome = _iterate_force_groups(ctx, sanity_check=True)
        self.assertEqual(d_bad.groupId, 0, "out-of-range groupId not clamped")
        self.assertEqual(ctx.rl_instance.set_group_calls, [("AA", 0)])
        self.assertEqual(outcome["changed"], 1)

    def test_sanity_check_clamps_offline_devices_too(self):
        """Sanity-check runs *before* the offline gate: an offline
        device with a stale out-of-range groupId still gets its
        in-memory state corrected, so when it next comes online the
        auto-restore mechanism pushes the *fixed* value."""
        d_bad = types.SimpleNamespace(addr="AA", groupId=99, link_online=False)
        ctx = _fake_ctx(devices={"AA": d_bad})
        _iterate_force_groups(ctx, sanity_check=True, skip_offline=True)
        self.assertEqual(d_bad.groupId, 0, "offline device's groupId not clamped")

    def test_skip_offline_true_counts_timeouts_separately_from_skips(self):
        """``setNodeGroupId`` returning False counts as ``timed_out``;
        offline devices count as ``skipped_offline``. Mixed scenario
        with one of each, with ``skip_offline=True`` so the offline
        device hits the skip branch."""
        d_on = types.SimpleNamespace(addr="ON", groupId=1, link_online=True)
        d_off = types.SimpleNamespace(addr="OFF", groupId=1, link_online=False)
        ctx = _fake_ctx(devices={"ON": d_on, "OFF": d_off})
        # Force the online device to fail its ACK.
        ctx.rl_instance = _FakeRlInstance(
            {"ON": d_on, "OFF": d_off}, set_group_returns=False,
        )
        # Add the mark_offline hook the production path expects.
        d_on.mark_offline = lambda _reason: setattr(d_on, "link_online", False)
        outcome = _iterate_force_groups(ctx, skip_offline=True)
        self.assertEqual(outcome["timed_out"], 1)
        self.assertEqual(outcome["skipped_offline"], 1)
        self.assertEqual(outcome["changed"], 0)
        self.assertEqual(outcome["total"], 2)

    def test_progress_callback_fires_per_device(self):
        d1 = types.SimpleNamespace(addr="AA", groupId=1, link_online=True)
        d2 = types.SimpleNamespace(addr="BB", groupId=2, link_online=True)
        ctx = _fake_ctx(devices={"AA": d1, "BB": d2})
        events: list[tuple] = []

        def _cb(index, total, mac, stage, message):
            events.append((index, total, mac, stage))

        _iterate_force_groups(ctx, progress_cb=_cb)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0][:2], (1, 2))
        self.assertEqual(events[1][:2], (2, 2))
        self.assertEqual(events[0][3], "RESYNC")


if __name__ == "__main__":
    unittest.main()
