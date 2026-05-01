"""Unit tests for ``racelink/web/dto.py`` helpers.

The serializers in this module are the contract between the domain
objects and the JSON the WebUI consumes. A regression here breaks the
UI silently — the fields just go missing — so it's worth pinning the
shape directly rather than only via the route-level smoke tests.
"""

from __future__ import annotations

import unittest

from racelink.domain import RL_Device, RL_Dev_Type
from racelink.web.dto import (
    group_caps_counts,
    group_counts,
    serialize_device,
    wled_count,
)


def _wled_device(addr: str, *, group_id: int) -> RL_Device:
    return RL_Device(
        addr,
        RL_Dev_Type.NODE_WLED_REV5,
        f"WLED-{addr[-4:]}",
        groupId=group_id,
        caps=RL_Dev_Type.NODE_WLED_REV5,
    )


def _startblock_device(addr: str, *, group_id: int) -> RL_Device:
    return RL_Device(
        addr,
        RL_Dev_Type.NODE_WLED_STARTBLOCK_REV3,
        f"SB-{addr[-4:]}",
        groupId=group_id,
        caps=RL_Dev_Type.NODE_WLED_STARTBLOCK_REV3,
    )


class GroupCountsTests(unittest.TestCase):
    def test_counts_devices_by_groupId(self):
        devs = [
            _wled_device("AABBCC112233", group_id=1),
            _wled_device("AABBCC112244", group_id=1),
            _wled_device("AABBCC112255", group_id=2),
        ]
        self.assertEqual(group_counts(devs), {1: 2, 2: 1})

    def test_empty_input_returns_empty(self):
        self.assertEqual(group_counts([]), {})


class GroupCapsCountsTests(unittest.TestCase):
    """C5: per-group capability counts power the scene editor's
    target-dropdown filter. The output shape is
    ``{groupId: {capability: count}}`` — capabilities derived from
    the device's ``dev_type`` (see RL_DEV_TYPE_INFO)."""

    def test_wled_node_contributes_wled_only(self):
        result = group_caps_counts([_wled_device("AABBCC000001", group_id=3)])
        self.assertEqual(result, {3: {"WLED": 1}})

    def test_startblock_node_contributes_both_caps(self):
        """A startblock node has caps=[STARTBLOCK, WLED] — both counts
        increment for the same group."""
        result = group_caps_counts([_startblock_device("AABBCC000002", group_id=4)])
        self.assertEqual(result, {4: {"STARTBLOCK": 1, "WLED": 1}})

    def test_mixed_groups_aggregate_independently(self):
        devs = [
            _wled_device("AABBCC000001", group_id=1),
            _wled_device("AABBCC000002", group_id=1),
            _startblock_device("AABBCC000003", group_id=1),
            _wled_device("AABBCC000004", group_id=2),
        ]
        result = group_caps_counts(devs)
        self.assertEqual(result[1], {"WLED": 3, "STARTBLOCK": 1})
        self.assertEqual(result[2], {"WLED": 1})

    def test_empty_input_returns_empty(self):
        self.assertEqual(group_caps_counts([]), {})

    def test_unknown_dev_type_contributes_no_caps(self):
        """A device with an unrecognised dev_type gets no caps —
        the group entry stays empty rather than crashing."""
        unknown = RL_Device("AABBCC000099", 99, "Unknown", groupId=5, caps=99)
        result = group_caps_counts([unknown])
        # Group 5 is either absent (no caps to record) or present
        # with an empty cap-dict — both are acceptable. Verify no
        # crash and no spurious caps.
        self.assertNotIn("WLED", result.get(5, {}))
        self.assertNotIn("STARTBLOCK", result.get(5, {}))


class SerializeDeviceTests(unittest.TestCase):
    def test_includes_dev_type_caps_for_C5(self):
        """The C5 frontend filter on the device dropdown reads
        ``dev_type_caps`` from the serialized device — verify it
        round-trips."""
        sb = _startblock_device("AABBCC000123", group_id=1)
        out = serialize_device(sb)
        self.assertIn("dev_type_caps", out)
        self.assertEqual(set(out["dev_type_caps"]), {"STARTBLOCK", "WLED"})


class WledCountTests(unittest.TestCase):
    def test_counts_only_wled_capable_devices(self):
        devs = [
            _wled_device("AABBCC000001", group_id=1),
            _startblock_device("AABBCC000002", group_id=2),
        ]
        # Both nodes have WLED capability.
        self.assertEqual(wled_count(devs), 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
