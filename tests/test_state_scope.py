"""Tests for ``racelink.domain.state_scope`` (plan "Bulk Set Group" fix #2)."""

from __future__ import annotations

import unittest

from racelink.domain import state_scope
from racelink.domain.state_scope import (
    DEVICE_MEMBERSHIP,
    DEVICE_SPECIALS,
    DEVICES,
    EFFECTS,
    FULL,
    GROUPS,
    NONE,
    normalize_scopes,
    sse_what_from_scopes,
)


class NormalizeScopesTests(unittest.TestCase):
    def test_empty_becomes_full(self):
        self.assertEqual(normalize_scopes(None), {FULL})
        self.assertEqual(normalize_scopes([]), {FULL})

    def test_unknown_tokens_are_dropped(self):
        self.assertEqual(normalize_scopes(["bogus"]), {FULL})

    def test_known_tokens_pass_through(self):
        self.assertEqual(
            normalize_scopes([DEVICES, GROUPS]),
            {DEVICES, GROUPS},
        )


class SseTopicMappingTests(unittest.TestCase):
    def test_full_scope_emits_both(self):
        self.assertEqual(sse_what_from_scopes([FULL]), ["groups", "devices"])

    def test_none_scope_emits_nothing(self):
        self.assertEqual(sse_what_from_scopes([NONE]), [])

    def test_devices_only(self):
        self.assertEqual(sse_what_from_scopes([DEVICES]), ["devices"])

    def test_groups_only(self):
        self.assertEqual(sse_what_from_scopes([GROUPS]), ["groups"])

    def test_device_membership_refreshes_both_devices_and_groups(self):
        """Regression: bulk regroup must refresh the Groups sidebar too.

        A membership change moves devices between groups; the sidebar's
        per-group counts therefore depend on ``DEVICE_MEMBERSHIP``.
        """
        self.assertEqual(
            sse_what_from_scopes([DEVICE_MEMBERSHIP]),
            ["devices", "groups"],
        )

    def test_device_specials_does_not_refresh_groups(self):
        """Specials (e.g. startblock config) don't move devices between groups."""
        self.assertEqual(
            sse_what_from_scopes([DEVICE_SPECIALS]),
            ["devices"],
        )

    def test_effects_emits_effects_topic(self):
        self.assertEqual(sse_what_from_scopes([EFFECTS]), ["effects"])

    def test_combined_scopes_preserve_order_devices_then_groups(self):
        self.assertEqual(
            sse_what_from_scopes([GROUPS, DEVICE_MEMBERSHIP]),
            ["devices", "groups"],
        )


class ModuleApiTests(unittest.TestCase):
    def test_module_reexports_match_internal_names(self):
        self.assertIs(state_scope.DEVICE_MEMBERSHIP, DEVICE_MEMBERSHIP)
        self.assertIs(state_scope.sse_what_from_scopes, sse_what_from_scopes)


if __name__ == "__main__":
    unittest.main()
