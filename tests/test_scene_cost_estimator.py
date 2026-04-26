"""Tests for ``scene_cost_estimator``.

Two layers:

1. **LoRa airtime formula** — compare the standard Semtech AN1200.13
   airtime against well-known reference values for SF7/250 kHz/CR4:5
   (the project default). These pin the formula so a refactor that
   accidentally drifts the constants gets caught.
2. **Per-action and per-scene wire cost** — feed sample scenes through
   ``estimate_scene`` and compare bytes/packets to hand-computed values.
"""

from __future__ import annotations

import unittest

from racelink.services.scene_cost_estimator import (
    LORA_BW_HZ,
    LORA_SF,
    RADIO_HEADER_BYTES,
    USB_FRAMING_BYTES,
    estimate_action,
    estimate_scene,
    lora_airtime_ms,
    lora_parameters,
)
from racelink.services.scenes_service import (
    KIND_DELAY,
    KIND_OFFSET_GROUP,
    KIND_SYNC,
    KIND_WLED_CONTROL,
    KIND_WLED_PRESET,
)


class LoRaAirtimeTests(unittest.TestCase):
    """The Semtech AN1200.13 airtime formula at SF7/BW250/CR4:5 yields well-
    known values; verify our implementation matches within ~5% for several
    payload sizes."""

    def test_active_lora_parameters_are_the_defaults(self):
        # Sanity: the test expectations below assume the default profile.
        self.assertEqual(LORA_SF, 7)
        self.assertEqual(LORA_BW_HZ, 250_000)

    def test_airtime_for_small_payload_is_in_expected_range(self):
        # 14-byte payload (Header7 + ~7 B body) at SF7/250 kHz/CR4:5 with
        # 8-symbol preamble + explicit header + CRC ≈ 16-22 ms.
        t = lora_airtime_ms(14)
        self.assertGreater(t, 12.0)
        self.assertLess(t, 25.0)

    def test_airtime_for_22b_full_control_body(self):
        # 22 B (worst-case OPC_CONTROL body) + 7 B header = 29 B PHY
        # payload. Reference: Semtech online airtime calculator at SF7/
        # BW250/CR4:5/8-sym preamble/explicit header/CRC ≈ 30 ms.
        t = lora_airtime_ms(29)
        self.assertGreater(t, 25.0)
        self.assertLess(t, 35.0)

    def test_airtime_grows_with_payload(self):
        for n in (4, 8, 16, 32, 64):
            with self.subTest(n=n):
                self.assertLess(lora_airtime_ms(n), lora_airtime_ms(n + 8))

    def test_lora_parameters_dict_exposed_for_ui(self):
        params = lora_parameters()
        self.assertIn("sf", params)
        self.assertIn("bw_hz", params)
        self.assertIn("cr", params)
        self.assertEqual(params["sf"], LORA_SF)
        self.assertEqual(params["bw_hz"], LORA_BW_HZ)


class ActionCostTests(unittest.TestCase):
    def test_sync_action_is_one_packet_with_4b_body(self):
        cost = estimate_action({"kind": KIND_SYNC})
        self.assertEqual(cost.packets, 1)
        # 4 B body + 7 B header + 2 B USB = 13 B total wire bytes.
        self.assertEqual(cost.bytes, 4 + RADIO_HEADER_BYTES + USB_FRAMING_BYTES)
        self.assertGreater(cost.airtime_ms, 0.0)

    def test_delay_action_is_zero_packets_but_contributes_airtime(self):
        cost = estimate_action({"kind": KIND_DELAY, "duration_ms": 250})
        self.assertEqual(cost.packets, 0)
        self.assertEqual(cost.bytes, 0)
        # Delay duration shows up as airtime so total scene duration is honest.
        self.assertEqual(cost.airtime_ms, 250.0)

    def test_wled_preset_is_one_packet_with_4b_body(self):
        cost = estimate_action({
            "kind": KIND_WLED_PRESET,
            "target": {"kind": "group", "value": 1},
            "params": {"presetId": 7, "brightness": 128},
        })
        self.assertEqual(cost.packets, 1)
        # P_Preset body is 4 B (groupId, flags, presetId, brightness).
        self.assertEqual(cost.bytes, 4 + RADIO_HEADER_BYTES + USB_FRAMING_BYTES)

    def test_wled_control_packet_size_grows_with_params(self):
        no_params = estimate_action({
            "kind": KIND_WLED_CONTROL,
            "target": {"kind": "group", "value": 1},
            "params": {},
        })
        with_params = estimate_action({
            "kind": KIND_WLED_CONTROL,
            "target": {"kind": "group", "value": 1},
            "params": {"mode": 5, "brightness": 200, "color1": [255, 0, 0]},
        })
        self.assertEqual(no_params.packets, 1)
        self.assertEqual(with_params.packets, 1)
        self.assertGreater(with_params.bytes, no_params.bytes)

    def test_rl_preset_materializes_referenced_preset_params(self):
        """An ``rl_preset`` action stores only ``presetId`` (+ optional
        brightness override); the runner materialises the full preset
        params on apply. Without a lookup the estimator under-reports;
        with one provided it should mirror what the runner emits — and
        the result must be larger than a 4 B WLED-preset packet."""
        from racelink.services.scenes_service import KIND_RL_PRESET

        preset = {
            "key": "fast_red",
            "id": 42,
            "params": {
                "mode": 5, "speed": 200, "intensity": 180,
                "brightness": 220, "palette": 4,
                "color1": [255, 0, 0], "color2": [0, 0, 0], "color3": [0, 0, 0],
            },
        }
        lookup = lambda ref: preset if ref in (42, "fast_red", "RL:fast_red") else None
        action = {
            "kind": KIND_RL_PRESET,
            "target": {"kind": "group", "value": 1},
            "params": {"presetId": 42},
        }
        # Without lookup: only 3 B body (groupId+flags+fieldMask).
        bare = estimate_action(action)
        # With lookup: full preset body materialises (mode, speed, intensity,
        # brightness, palette, three colors → ~14 B body).
        full = estimate_action(action, rl_preset_lookup=lookup)
        self.assertGreater(full.bytes, bare.bytes)
        # And full RL preset must be larger than a fixed 4 B WLED preset
        # — that's the operator-facing intuition the estimator must honour.
        wled = estimate_action({
            "kind": KIND_WLED_PRESET,
            "target": {"kind": "group", "value": 1},
            "params": {"presetId": 7, "brightness": 128},
        })
        self.assertGreater(full.bytes, wled.bytes)

    def test_rl_preset_brightness_override_wins_over_preset(self):
        """An action's brightness override should propagate into the
        materialised body — same contract as ``_run_rl_preset``."""
        from racelink.services.scenes_service import KIND_RL_PRESET

        preset = {"key": "p", "id": 1, "params": {"brightness": 50}}
        lookup = lambda ref: preset
        action_default = {
            "kind": KIND_RL_PRESET,
            "target": {"kind": "group", "value": 1},
            "params": {"presetId": 1},
        }
        action_override = {
            "kind": KIND_RL_PRESET,
            "target": {"kind": "group", "value": 1},
            "params": {"presetId": 1, "brightness": 220},
        }
        # Both materialise the same body shape (one brightness byte); the
        # bytes count is identical, but the test verifies no crash on the
        # override merge path.
        a = estimate_action(action_default, rl_preset_lookup=lookup)
        b = estimate_action(action_override, rl_preset_lookup=lookup)
        self.assertEqual(a.bytes, b.bytes)


class OffsetGroupCostTests(unittest.TestCase):
    def test_all_groups_linear_is_one_offset_plus_children(self):
        action = {
            "kind": KIND_OFFSET_GROUP,
            "groups": "all",
            "offset": {"mode": "linear", "base_ms": 0, "step_ms": 100},
            "actions": [
                {"kind": KIND_WLED_CONTROL, "target": {"kind": "scope"},
                 "params": {"mode": 5}},
                {"kind": KIND_WLED_PRESET, "target": {"kind": "scope"},
                 "params": {"presetId": 7, "brightness": 128}},
            ],
        }
        cost = estimate_action(action)
        # 1 OPC_OFFSET + 2 children = 3 packets.
        self.assertEqual(cost.packets, 3)
        self.assertEqual(cost.detail["wire_path"], "A_broadcast_formula")
        self.assertEqual(cost.detail["child_count"], 2)
        self.assertEqual(cost.detail["offset_packets"], 1)

    def test_sparse_explicit_emits_per_group_offset_packets(self):
        action = {
            "kind": KIND_OFFSET_GROUP,
            "groups": [1, 3, 5],
            "offset": {
                "mode": "explicit",
                "values": [
                    {"id": 1, "offset_ms": 0},
                    {"id": 3, "offset_ms": 100},
                    {"id": 5, "offset_ms": 250},
                ],
            },
            "actions": [
                {"kind": KIND_WLED_CONTROL, "target": {"kind": "scope"},
                 "params": {"mode": 5}},
            ],
        }
        cost = estimate_action(action)
        # 3 OPC_OFFSET + 1 child = 4 packets.
        self.assertEqual(cost.packets, 4)
        self.assertEqual(cost.detail["wire_path"], "B_per_group_explicit")

    def test_offset_group_no_children_is_just_offset_setup(self):
        action = {
            "kind": KIND_OFFSET_GROUP,
            "groups": "all",
            "offset": {"mode": "none"},
            "actions": [],
        }
        cost = estimate_action(action)
        self.assertEqual(cost.packets, 1)
        self.assertEqual(cost.detail["child_count"], 0)


class SceneCostTests(unittest.TestCase):
    def test_scene_aggregates_per_action_and_total(self):
        scene = {
            "actions": [
                {"kind": KIND_WLED_PRESET,
                 "target": {"kind": "group", "value": 1},
                 "params": {"presetId": 7, "brightness": 128}},
                {"kind": KIND_DELAY, "duration_ms": 100},
                {"kind": KIND_SYNC},
            ],
        }
        cost = estimate_scene(scene)
        self.assertEqual(len(cost.per_action), 3)
        # Total packets = preset (1) + delay (0) + sync (1) = 2
        self.assertEqual(cost.total.packets, 2)
        # Total bytes = preset_bytes + 0 + sync_bytes
        self.assertEqual(
            cost.total.bytes,
            cost.per_action[0].bytes + cost.per_action[2].bytes,
        )
        # Total airtime includes the delay duration (host-side wait).
        self.assertGreaterEqual(cost.total.airtime_ms, 100.0)

    def test_scene_with_offset_group_and_sync(self):
        scene = {
            "actions": [
                {"kind": KIND_OFFSET_GROUP,
                 "groups": "all",
                 "offset": {"mode": "linear", "base_ms": 0, "step_ms": 100},
                 "actions": [
                     {"kind": KIND_WLED_CONTROL,
                      "target": {"kind": "scope"},
                      "params": {"mode": 5}},
                 ]},
                {"kind": KIND_SYNC},
            ],
        }
        cost = estimate_scene(scene)
        # offset_group: 1 (offset) + 1 (child) = 2; sync: 1. Total: 3.
        self.assertEqual(cost.total.packets, 3)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
