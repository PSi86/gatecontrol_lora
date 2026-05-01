"""STATE_REQUEST / STATE_REPORT round-trip (Batch B, 2026-04-28).

The host's ``gateway_service.query_state()`` writes a 1-byte
``GW_CMD_STATE_REQUEST`` frame and waits for the matching
``EV_STATE_REPORT``. This test pins:

* The send is a 3-byte USB frame: ``[0x00, 0x01, 0x7F]``.
* The reply seeds the result dict with state name + byte + metadata.
* A missing reply within ``timeout_s`` returns a fallback dict with
  ``ok=False`` (degraded but not raising).
* The transport's state mirror updates as a side-effect of the reply
  arriving via the listener pipeline.
"""

from __future__ import annotations

import sys
import threading
import time
import types
import unittest


serial_stub = types.ModuleType("serial")


class _FakeSerial:
    def __init__(self, *args, **kwargs):
        self.baudrate = None
        self.timeout = None
        self.port = None
        self.is_open = False
        self.written = bytearray()

    def write(self, data):
        self.written.extend(data)

    def read(self, _n):
        return b""

    def close(self):
        self.is_open = False


serial_stub.Serial = _FakeSerial
serial_stub.SerialException = type("SerialException", (Exception,), {})
serial_tools_stub = types.ModuleType("serial.tools")
serial_list_ports_stub = types.ModuleType("serial.tools.list_ports")
serial_list_ports_stub.comports = lambda: []
serial_tools_stub.list_ports = serial_list_ports_stub
serial_stub.tools = serial_tools_stub

sys.modules.setdefault("serial", serial_stub)
sys.modules.setdefault("serial.tools", serial_tools_stub)
sys.modules.setdefault("serial.tools.list_ports", serial_list_ports_stub)

from racelink.services.gateway_service import GatewayService
from racelink.transport.gateway_events import (
    EV_STATE_REPORT,
    GATEWAY_STATE_IDLE,
    GATEWAY_STATE_RX_WINDOW,
    GW_CMD_STATE_REQUEST,
)
from racelink.transport.gateway_serial import GatewaySerialTransport


def _new_transport():
    t = GatewaySerialTransport(port="COM_TEST")
    t.ser = _FakeSerial()
    t.ser.is_open = True
    return t


class _MinimalController:
    """Bare-minimum controller stub to construct GatewayService.

    GatewayService stores the controller and reaches into a few attributes
    only on paths the query-state test never exercises (state lock, host
    API, etc.). Setting the few attributes it touches up-front keeps the
    test focused.
    """

    def __init__(self, transport):
        self.transport = transport
        self.state_repository = None
        self.ready = True
        self._last_error_notify_ts = 0.0
        self._host_api = None
        self._transport_hooks_installed = False


class StateQueryTests(unittest.TestCase):

    def test_send_state_request_writes_one_byte_command(self):
        transport = _new_transport()
        ok = transport.send_state_request()
        self.assertTrue(ok)
        # Frame layout: [SOF=0x00][LEN=1][CMD=0x7F]
        self.assertEqual(bytes(transport.ser.written), bytes([0x00, 0x01, GW_CMD_STATE_REQUEST]))

    def test_query_state_round_trip_returns_state(self):
        transport = _new_transport()
        controller = _MinimalController(transport)
        service = GatewayService(controller)

        def feed_reply():
            # The query_state callback adds itself as a listener before
            # writing — give it a moment to register, then simulate the
            # gateway's STATE_REPORT carrying RX_WINDOW + 1500 ms metadata.
            time.sleep(0.05)
            transport._handle_frame(
                EV_STATE_REPORT,
                bytes([GATEWAY_STATE_RX_WINDOW, 0xDC, 0x05]),  # 1500 LE
            )

        threading.Thread(target=feed_reply, daemon=True).start()
        result = service.query_state(timeout_s=0.5)

        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "RX_WINDOW")
        self.assertEqual(result["state_byte"], GATEWAY_STATE_RX_WINDOW)
        self.assertEqual(result["state_metadata_ms"], 1500)
        # Side-effect: the transport's state mirror followed the reply.
        self.assertEqual(transport.gateway_state_byte, GATEWAY_STATE_RX_WINDOW)
        self.assertEqual(transport.gateway_state_metadata_ms, 1500)

    def test_query_state_timeout_returns_fallback_snapshot(self):
        transport = _new_transport()
        # Pre-seed the mirror so the fallback path has something useful
        # to return — emulates a host that previously saw a state event.
        transport._update_gateway_state(GATEWAY_STATE_IDLE, 0)
        controller = _MinimalController(transport)
        service = GatewayService(controller)

        result = service.query_state(timeout_s=0.05)

        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "IDLE")
        self.assertEqual(result["state_byte"], GATEWAY_STATE_IDLE)


if __name__ == "__main__":
    unittest.main()
