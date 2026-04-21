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


if __name__ == "__main__":
    unittest.main()
