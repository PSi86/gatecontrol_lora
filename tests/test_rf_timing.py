"""Pin tests for the RF timing constants.

The :mod:`racelink.services.rf_timing` module is the single source
of truth for every timeout / retry decision the RF code makes. A
casual edit that pushes a value into an unreasonable range
(retries=10, timeout=60s, etc.) silently changes operator UX
across many call sites at once. These tests don't assert *exact*
values — they assert the values stay within sane bounds, so a
deliberate tune lands without rewrite friction but a typo that
multiplies a value by 10 fails fast.
"""

from __future__ import annotations

import unittest

from racelink.services import rf_timing


class UnicastTimingTests(unittest.TestCase):
    def test_per_attempt_timeout_is_sub_second_to_few_seconds(self):
        # LoRa healthy-link RTT ~300–600 ms; per-attempt timeout
        # should give 2–4× margin without crossing into "operator
        # thinks something is broken" territory.
        self.assertGreater(rf_timing.UNICAST_ATTEMPT_TIMEOUT_S, 0.5)
        self.assertLess(rf_timing.UNICAST_ATTEMPT_TIMEOUT_S, 5.0)

    def test_max_attempts_is_bounded(self):
        # 3 attempts is the design target; 1 = no retries (degrades
        # to old behaviour); 10+ would be an obvious mistake.
        self.assertGreaterEqual(rf_timing.UNICAST_MAX_ATTEMPTS, 1)
        self.assertLessEqual(rf_timing.UNICAST_MAX_ATTEMPTS, 5)

    def test_retry_delay_is_short(self):
        # The delay only exists so the gateway radio finishes its
        # RX→TX transition before the next TX queues. Sub-second.
        self.assertGreaterEqual(rf_timing.UNICAST_RETRY_DELAY_S, 0.0)
        self.assertLess(rf_timing.UNICAST_RETRY_DELAY_S, 1.0)

    def test_worst_case_is_under_old_eight_second_budget(self):
        """The whole point of the rf-timing batch.

        Old code: ``8.0 s`` single attempt with no retries.
        New code: per_attempt × attempts + delay × (attempts-1)
        must be *shorter* than 8 s, so even genuinely-offline
        devices get faster fail-fast than before.
        """
        worst_case = (
            rf_timing.UNICAST_ATTEMPT_TIMEOUT_S * rf_timing.UNICAST_MAX_ATTEMPTS
            + rf_timing.UNICAST_RETRY_DELAY_S * max(0, rf_timing.UNICAST_MAX_ATTEMPTS - 1)
        )
        self.assertLess(
            worst_case, 8.0,
            f"worst case {worst_case:.2f}s ≥ old 8 s single-attempt budget — "
            "the rf-timing batch's premise no longer holds.",
        )


class CollectTimingTests(unittest.TestCase):
    def test_idle_timeout_short(self):
        self.assertGreater(rf_timing.COLLECT_IDLE_TIMEOUT_S, 0.0)
        self.assertLess(rf_timing.COLLECT_IDLE_TIMEOUT_S, 2.0)

    def test_max_ceiling_reasonable(self):
        # Discover with no expected count uses this as the entire
        # budget; status / stream cap their dynamic timeout here.
        self.assertGreaterEqual(rf_timing.COLLECT_MAX_CEILING_S, 1.0)
        self.assertLessEqual(rf_timing.COLLECT_MAX_CEILING_S, 30.0)

    def test_per_device_scale_short(self):
        self.assertGreater(rf_timing.COLLECT_PER_DEVICE_S, 0.0)
        self.assertLess(rf_timing.COLLECT_PER_DEVICE_S, 1.0)

    def test_base_reasonable(self):
        self.assertGreaterEqual(rf_timing.COLLECT_BASE_S, 0.5)
        self.assertLessEqual(rf_timing.COLLECT_BASE_S, 5.0)


class StreamTimingTests(unittest.TestCase):
    def test_attempt_timeout_reasonable(self):
        # Stream payloads are heavier than unicast acks; allow
        # higher per-attempt timeout but still under 30 s.
        self.assertGreaterEqual(rf_timing.STREAM_ATTEMPT_TIMEOUT_S, 1.0)
        self.assertLessEqual(rf_timing.STREAM_ATTEMPT_TIMEOUT_S, 30.0)

    def test_max_attempts_bounded(self):
        self.assertGreaterEqual(rf_timing.STREAM_MAX_ATTEMPTS, 1)
        self.assertLessEqual(rf_timing.STREAM_MAX_ATTEMPTS, 5)

    def test_retry_delay_short(self):
        self.assertGreaterEqual(rf_timing.STREAM_RETRY_DELAY_S, 0.0)
        self.assertLess(rf_timing.STREAM_RETRY_DELAY_S, 1.0)


if __name__ == "__main__":
    unittest.main()
