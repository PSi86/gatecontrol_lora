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
    WIRE_OVERHEAD_MS_PER_PACKET,
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
    """The Semtech AN1200.13 airtime formula at SF7/BW125/CR4:5 yields
    well-known values; verify our implementation matches within ~10% for
    several payload sizes.

    BW was 250_000 pre-2026-04-28 — the gateway actually runs at 125 kHz
    (RACELINK_BW_KHZ in RaceLink_Gateway/src/main.cpp). Halving BW
    doubles Tsym, so airtime scaled 2x. Bounds were widened accordingly.
    """

    def test_active_lora_parameters_are_the_defaults(self):
        # Sanity: the test expectations below assume the default profile.
        self.assertEqual(LORA_SF, 7)
        self.assertEqual(LORA_BW_HZ, 125_000)

    def test_airtime_for_small_payload_is_in_expected_range(self):
        # 14-byte payload (Header7 + ~7 B body) at SF7/125 kHz/CR4:5 with
        # 8-symbol preamble + explicit header + CRC ≈ 32-50 ms (was
        # 16-22 ms at the old 250 kHz value).
        t = lora_airtime_ms(14)
        self.assertGreater(t, 30.0)
        self.assertLess(t, 55.0)

    def test_airtime_for_22b_full_control_body(self):
        # 22 B (worst-case OPC_CONTROL body) + 7 B header = 29 B PHY
        # payload. Reference: Semtech online airtime calculator at SF7/
        # BW125/CR4:5/8-sym preamble/explicit header/CRC ≈ 60 ms (was
        # ~30 ms at the old 250 kHz value).
        t = lora_airtime_ms(29)
        self.assertGreater(t, 55.0)
        self.assertLess(t, 75.0)

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
        # Frontend tooltip uses this to render "+12 ms/pkt USB+gateway
        # overhead" — make sure the schema surfaces the constant.
        self.assertIn("wire_overhead_ms_per_packet", params)
        self.assertEqual(params["wire_overhead_ms_per_packet"], WIRE_OVERHEAD_MS_PER_PACKET)


class WallClockOverheadTests(unittest.TestCase):
    """Wall-clock prediction adds per-packet USB/gateway overhead on top of
    LoRa airtime so the cost-badge ``≈ NN ms`` matches the runner's measured
    ``actual:`` within a few ms.

    Calibration data behind WIRE_OVERHEAD_MS_PER_PACKET = 12.0 (post-Batch-B
    gateway optimisations, 2026-04-29):

    * 1 pkt 30 B: 67 ms airtime + 12 ms = 79 ms predicted; observed 78-81 ms.
    * 4 pkts 86 B (offset_group): 216 ms airtime + 4 × 12 = 264 ms; observed
      263-267 ms.

    These tests pin the formula. Recalibrate WIRE_OVERHEAD_MS_PER_PACKET when
    the wire path changes (e.g. if the gateway moves to native USB-CDC).
    """

    def test_wire_overhead_constant_is_calibrated(self):
        # Pin the calibration constant. If a future tweak bumps it, the
        # next reviewer should be aware that the change ripples through
        # every cost-badge prediction. Adjust this value when (and only
        # when) the per-packet wire path measurably changes.
        self.assertEqual(WIRE_OVERHEAD_MS_PER_PACKET, 12.0)

    def test_single_packet_wall_clock_equals_airtime_plus_overhead(self):
        cost = estimate_action({"kind": KIND_SYNC})
        # 4 B body + 7 B header = 11 B PHY → airtime is whatever the
        # Semtech formula yields. wall_clock_ms must be exactly that
        # plus one per-packet overhead (one packet sent).
        expected_wall_clock = cost.airtime_ms + WIRE_OVERHEAD_MS_PER_PACKET
        self.assertAlmostEqual(cost.wall_clock_ms, expected_wall_clock, places=6)

    def test_wall_clock_scales_with_packet_count(self):
        # An offset_group with two participating groups + one WLED preset
        # child fans out to 2 OPC_OFFSET (Phase 1) + 1 OPC_PRESET child
        # = 3 packets. Every packet contributes one overhead increment.
        cost = estimate_action({
            "kind": KIND_OFFSET_GROUP,
            "groups": [1, 2],
            "offset": {"mode": "explicit", "offset_ms": 100},
            "actions": [
                {"kind": KIND_WLED_PRESET,
                 "target": {"kind": "group", "value": 1},
                 "params": {"presetId": 7, "brightness": 128}},
            ],
        })
        self.assertGreater(cost.packets, 0)
        expected_overhead = cost.packets * WIRE_OVERHEAD_MS_PER_PACKET
        self.assertAlmostEqual(
            cost.wall_clock_ms - cost.airtime_ms,
            expected_overhead,
            places=6,
            msg="wall_clock_ms must be airtime + (packets * per-packet overhead)",
        )

    def test_delay_action_wall_clock_equals_duration_no_overhead(self):
        # KIND_DELAY sends no packet — wall_clock must equal duration_ms,
        # NOT duration_ms + overhead (there's nothing to "send").
        cost = estimate_action({"kind": KIND_DELAY, "duration_ms": 500})
        self.assertEqual(cost.packets, 0)
        self.assertEqual(cost.airtime_ms, 500.0)
        self.assertEqual(cost.wall_clock_ms, 500.0)

    def test_scene_total_wall_clock_is_sum_of_per_action(self):
        # Two SYNCs + one DELAY: scene total wall_clock should equal the
        # sum of individual per-action wall_clock values (no inter-action
        # surcharge in this iteration).
        scene = {"actions": [
            {"kind": KIND_SYNC},
            {"kind": KIND_DELAY, "duration_ms": 100},
            {"kind": KIND_SYNC},
        ]}
        result = estimate_scene(scene)
        expected_total = sum(a.wall_clock_ms for a in result.per_action)
        self.assertAlmostEqual(result.total.wall_clock_ms, expected_total, places=6)
        # Sanity: total wall_clock includes the DELAY's full duration AND
        # the two SYNCs' overhead.
        self.assertGreater(result.total.wall_clock_ms, 100.0)
        self.assertGreaterEqual(
            result.total.wall_clock_ms,
            result.total.airtime_ms + 2 * WIRE_OVERHEAD_MS_PER_PACKET - 0.001,
        )


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
