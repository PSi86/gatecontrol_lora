"""Tests for :func:`classify_gateway_error` and the structured error payload."""

from __future__ import annotations

import unittest

from controller import (
    GW_ERR_HOST_ERROR,
    GW_ERR_LINK_LOST,
    GW_ERR_NOT_FOUND,
    GW_ERR_PORT_BUSY,
    RaceLink_Host,
    classify_gateway_error,
)


class _Ui:
    def message_notify(self, _msg: str) -> None:
        pass

    def broadcast_ui(self, _panel: str) -> None:
        pass


class _Db:
    def option(self, _key, default=None):
        return default

    def option_set(self, _key, _value):
        pass


class _RhApi:
    def __init__(self):
        self.db = _Db()
        self.ui = _Ui()

    def __(self, text):  # noqa: D401 -- satisfies HostApi.__ translator
        return text


def _fresh_host():
    return RaceLink_Host(_RhApi(), "name", "label")


class ClassifyGatewayErrorTests(unittest.TestCase):
    def test_port_busy_messages(self):
        for msg in (
            "Port busy: /dev/ttyUSB0 exclusive lock failed",
            "Could not exclusively lock port /dev/ttyUSB0",
            "[Errno 11] Resource temporarily unavailable: '/dev/ttyUSB0'",
        ):
            self.assertEqual(classify_gateway_error(msg), GW_ERR_PORT_BUSY, msg)

    def test_not_found_messages(self):
        self.assertEqual(
            classify_gateway_error("No RaceLink Gateway module discovered or configured"),
            GW_ERR_NOT_FOUND,
        )

    def test_link_lost_messages(self):
        self.assertEqual(
            classify_gateway_error("Serial disconnect detected"), GW_ERR_LINK_LOST,
        )

    def test_unknown_falls_back_to_host_error(self):
        self.assertEqual(classify_gateway_error("some unexpected failure"), GW_ERR_HOST_ERROR)

    def test_empty_reason_uses_fallback(self):
        self.assertEqual(classify_gateway_error(""), GW_ERR_HOST_ERROR)


class LastGatewayErrorShapeTests(unittest.TestCase):
    def test_payload_includes_code_and_next_retry_for_port_busy(self):
        host = _fresh_host()
        host._record_gateway_error(
            reason="exclusive lock failed", origin="auto",
        )

        status = host.gateway_status()
        last = status["last_error"]
        self.assertIsNotNone(last)
        self.assertEqual(last["code"], GW_ERR_PORT_BUSY)
        self.assertIsNotNone(last["next_retry_in_s"])
        self.assertGreater(last["next_retry_in_s"], 0)
        # Clean up the scheduled timer so the test does not leave threads behind.
        host._cancel_gateway_retry()

    def test_payload_has_no_auto_retry_for_not_found(self):
        host = _fresh_host()
        host._record_gateway_error(
            reason="No RaceLink Gateway module discovered or configured",
            origin="manual",
        )

        status = host.gateway_status()
        last = status["last_error"]
        self.assertIsNotNone(last)
        self.assertEqual(last["code"], GW_ERR_NOT_FOUND)
        self.assertIsNone(last["next_retry_in_s"])

    def test_retry_gateway_cancels_pending_auto_retry(self):
        host = _fresh_host()
        host._record_gateway_error(
            reason="exclusive lock failed", origin="auto",
        )
        self.assertIsNotNone(host._gateway_retry_timer)
        # retry_gateway() forces an immediate try and must kill the timer.
        # discoverPort is called inside and will itself record a new error,
        # which will schedule a fresh timer -- that is expected behaviour.
        host.retry_gateway()
        host._cancel_gateway_retry()

    def test_not_found_upgrades_to_link_lost_during_recovery(self):
        """USB unplug: after EV_ERROR we treat subsequent NOT_FOUND as
        LINK_LOST so the auto-retry keeps polling until the dongle returns."""
        host = _fresh_host()
        # Simulate ``schedule_reconnect`` setting the flag.
        host._link_recovery_pending = True

        host._record_gateway_error(
            reason="No RaceLink Gateway module discovered or configured",
            origin="auto",
        )

        status = host.gateway_status()
        last = status["last_error"]
        self.assertEqual(last["code"], GW_ERR_LINK_LOST)
        # And auto-retry must be armed (since LINK_LOST is auto-eligible).
        self.assertIsNotNone(last["next_retry_in_s"])
        host._cancel_gateway_retry()

    def test_retry_gateway_clears_link_recovery_flag(self):
        host = _fresh_host()
        host._link_recovery_pending = True
        host.retry_gateway()
        # After a manual retry the flag is cleared, so a subsequent NOT_FOUND
        # is shown as NOT_FOUND (user asked to re-evaluate the situation).
        self.assertFalse(host._link_recovery_pending)
        host._cancel_gateway_retry()


if __name__ == "__main__":
    unittest.main()
