import unittest
import sys
import types


serial_stub = types.ModuleType("serial")


class _FakeSerial:
    def __init__(self, *args, **kwargs):
        self.baudrate = None
        self.timeout = None
        self.port = None
        self.is_open = False


serial_stub.Serial = _FakeSerial
serial_stub.SerialException = Exception
serial_tools_stub = types.ModuleType("serial.tools")
serial_list_ports_stub = types.ModuleType("serial.tools.list_ports")
serial_list_ports_stub.comports = lambda: []
serial_tools_stub.list_ports = serial_list_ports_stub
serial_stub.tools = serial_tools_stub

sys.modules.setdefault("serial", serial_stub)
sys.modules.setdefault("serial.tools", serial_tools_stub)
sys.modules.setdefault("serial.tools.list_ports", serial_list_ports_stub)

from racelink.transport.gateway_serial import GatewaySerialTransport, SendOutcome
from racelink.transport.gateway_events import (
    GATEWAY_STATE_IDLE,
    GATEWAY_STATE_RX_WINDOW,
    GATEWAY_STATE_UNKNOWN,
)


class GatewaySerialTransportSendTests(unittest.TestCase):
    """Smoke-tests for the synchronous send path (Batch B).

    The transport's wire-level `_send_m2n` blocks until the gateway's
    matching outcome event lands; tests stub it to keep the high-level
    helpers (send_stream etc.) exercised without a real USB.
    """

    def test_send_stream_sends_raw_payload_without_host_ctrl(self):
        transport = GatewaySerialTransport(port="COM1")
        calls = []

        def fake_send(type_full, recv3, body=b""):
            calls.append(
                {
                    "type_full": type_full,
                    "recv3": recv3,
                    "body": body,
                }
            )
            return SendOutcome.success()

        transport._send_m2n = fake_send

        outcome = transport.send_stream(recv3=b"\xAA\xBB\xCC", payload=b"\x01\x02")

        self.assertTrue(bool(outcome))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["recv3"], b"\xAA\xBB\xCC")
        self.assertEqual(calls[0]["body"], b"\x01\x02")


class GatewayStateMirrorTests(unittest.TestCase):
    """The transport's state mirror is the host's source of pill truth.

    Pre-Batch-B this lived behind the EV_RX_WINDOW_OPEN/CLOSED pair (idempotent
    OPEN/CLOSE counter). Batch B consolidated both events into EV_STATE_CHANGED
    with a state-byte body; the test set follows the same shape — verifies
    the mirror updates from each event, with metadata preserved for
    RX_WINDOW.
    """

    def test_initial_state_is_unknown_until_first_event(self):
        transport = GatewaySerialTransport(port="COM1")
        self.assertEqual(transport.gateway_state_byte, GATEWAY_STATE_UNKNOWN)
        self.assertEqual(transport.gateway_state_name, "UNKNOWN")
        self.assertEqual(transport.gateway_state_metadata_ms, 0)

    def test_state_changed_idle_updates_mirror(self):
        transport = GatewaySerialTransport(port="COM1")
        transport._update_gateway_state(GATEWAY_STATE_IDLE, 0)
        self.assertEqual(transport.gateway_state_byte, GATEWAY_STATE_IDLE)
        self.assertEqual(transport.gateway_state_name, "IDLE")
        self.assertEqual(transport.gateway_state_metadata_ms, 0)

    def test_state_changed_rx_window_carries_metadata(self):
        transport = GatewaySerialTransport(port="COM1")
        transport._update_gateway_state(GATEWAY_STATE_RX_WINDOW, 1500)
        self.assertEqual(transport.gateway_state_byte, GATEWAY_STATE_RX_WINDOW)
        self.assertEqual(transport.gateway_state_name, "RX_WINDOW")
        self.assertEqual(transport.gateway_state_metadata_ms, 1500)

    def test_parse_state_event_body_handles_short_bodies(self):
        # 1-byte body (state-only, no metadata)
        sb, ms = GatewaySerialTransport._parse_state_event_body(bytes([0x00]))
        self.assertEqual(sb, GATEWAY_STATE_IDLE)
        self.assertEqual(ms, 0)
        # 3-byte body (state + LE16 metadata)
        sb, ms = GatewaySerialTransport._parse_state_event_body(bytes([0x02, 0xE8, 0x03]))  # 1000 ms
        self.assertEqual(sb, GATEWAY_STATE_RX_WINDOW)
        self.assertEqual(ms, 1000)
        # empty body falls back to UNKNOWN sentinel
        sb, ms = GatewaySerialTransport._parse_state_event_body(b"")
        self.assertEqual(sb, GATEWAY_STATE_UNKNOWN)
        self.assertEqual(ms, 0)


if __name__ == "__main__":
    unittest.main()
