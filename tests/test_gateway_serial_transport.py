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

from racelink.transport.gateway_serial import GatewaySerialTransport
from racelink.transport.gateway_events import (
    EV_RX_WINDOW_CLOSED,
    EV_RX_WINDOW_OPEN,
)


class GatewaySerialTransportTests(unittest.TestCase):
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
            return True

        transport._send_m2n = fake_send

        transport.send_stream(recv3=b"\xAA\xBB\xCC", payload=b"\x01\x02")

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["recv3"], b"\xAA\xBB\xCC")
        self.assertEqual(calls[0]["body"], b"\x01\x02")

    def test_rx_window_state_is_idempotent_for_duplicate_open(self):
        """Gateway firmware may emit OPEN(Timed) without a prior CLOSE for the
        preceding Continuous RX; tracking must not blow up or log an error."""
        transport = GatewaySerialTransport(port="COM1")

        self.assertEqual(transport.rx_window_state, 0)
        # Initial Continuous RX after boot.
        transport._update_rx_window_state(EV_RX_WINDOW_OPEN)
        self.assertEqual(transport.rx_window_state, 1)
        # Host TX triggers Continuous -> Idle -> Timed RX. No CLOSE in between.
        transport._update_rx_window_state(EV_RX_WINDOW_OPEN)
        self.assertEqual(transport.rx_window_state, 1)
        # Timed window ends.
        transport._update_rx_window_state(EV_RX_WINDOW_CLOSED)
        self.assertEqual(transport.rx_window_state, 0)
        # Post-Timed default kicks back to Continuous.
        transport._update_rx_window_state(EV_RX_WINDOW_OPEN)
        self.assertEqual(transport.rx_window_state, 1)
        # Double CLOSE should also not underflow.
        transport._update_rx_window_state(EV_RX_WINDOW_CLOSED)
        transport._update_rx_window_state(EV_RX_WINDOW_CLOSED)
        self.assertEqual(transport.rx_window_state, 0)


if __name__ == "__main__":
    unittest.main()
