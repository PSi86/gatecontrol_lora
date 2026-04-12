import unittest

from racelink.domain import RL_Device
from racelink.services.gateway_service import GatewayService
from racelink.transport import EV_RX_WINDOW_CLOSED, LP


class FakeTransport:
    def __init__(self):
        self.listeners = []
        self.tx_listeners = []
        self.sent_config = []

    def add_listener(self, cb):
        self.listeners.append(cb)

    def remove_listener(self, cb):
        if cb in self.listeners:
            self.listeners.remove(cb)

    def add_tx_listener(self, cb):
        self.tx_listeners.append(cb)

    def send_config(self, **kwargs):
        self.sent_config.append(kwargs)

    def emit(self, ev):
        for cb in list(self.listeners):
            cb(ev)

    def drain_events(self, timeout_s=0.0):
        return []


class FakeController:
    def __init__(self):
        self.dev = RL_Device("AABBCCDDEEFF", 1, "Node")
        self.transport = FakeTransport()
        self._pending_expect = None
        self._pending_config = {}
        self._transport_hooks_installed = False
        self.applied = []

    def _to_hex_str(self, value):
        if isinstance(value, (bytes, bytearray)):
            return bytes(value).hex().upper()
        return str(value or "").upper()

    def getDeviceFromAddress(self, addr):
        addr = str(addr or "").upper()
        if addr in {self.dev.addr, self.dev.addr[-6:]}:
            return self.dev
        return None

    def _apply_config_update(self, dev, option, data0):
        self.applied.append((dev, option, data0))

    def _stream_ctrl(self, start, stop, packets_left):
        return (0x80 if start else 0) | (0x40 if stop else 0) | (packets_left & 0x3F)

    @property
    def device_repository(self):
        class Repo:
            def __init__(self, item):
                self.item = item

            def list(self):
                return [self.item]

        return Repo(self.dev)


class GatewayServiceTests(unittest.TestCase):
    def test_send_and_wait_for_ack_marks_device_online(self):
        controller = FakeController()
        service = GatewayService(controller)

        def send_fn():
            controller.transport.emit(
                {
                    "opc": LP.OPC_ACK,
                    "ack_of": LP.OPC_CONFIG,
                    "ack_status": 0,
                    "sender3": bytes.fromhex("DDEEFF"),
                }
            )
            controller.transport.emit({"type": EV_RX_WINDOW_CLOSED})

        events, got_closed = service.send_and_wait_for_reply(bytes.fromhex("DDEEFF"), LP.OPC_CONFIG, send_fn)

        self.assertTrue(got_closed)
        self.assertEqual(len(events), 1)
        self.assertTrue(controller.dev.link_online)

    def test_handle_ack_applies_pending_config(self):
        controller = FakeController()
        service = GatewayService(controller)
        controller._pending_config["DDEEFF"] = {"option": 0x04, "data0": 1}

        service.handle_ack_event(
            {
                "sender3": bytes.fromhex("DDEEFF"),
                "ack_of": LP.OPC_CONFIG,
                "ack_status": 0,
                "ack_seq": 7,
                "host_rssi": -40,
                "host_snr": 6,
            }
        )

        self.assertTrue(controller.dev.ack_ok())
        self.assertEqual(controller.applied, [(controller.dev, 0x04, 1)])

    def test_pending_window_closed_marks_device_offline(self):
        controller = FakeController()
        service = GatewayService(controller)
        controller._pending_expect = {
            "dev": controller.dev,
            "rule": type("Rule", (), {"name": "STATUS"})(),
            "opcode7": LP.OPC_STATUS,
            "sender_last3": "DDEEFF",
        }

        service.pending_window_closed({"type": EV_RX_WINDOW_CLOSED})

        self.assertFalse(controller.dev.link_online)
        self.assertEqual(controller.dev.link_error, "Missing reply (STATUS)")
        self.assertIsNone(controller._pending_expect)


if __name__ == "__main__":
    unittest.main()
