import unittest

from racelink.domain import RL_Device
from racelink.services.discovery_service import DiscoveryService
from racelink.services.status_service import StatusService
from racelink.transport import LP


class FakeTransport:
    def __init__(self):
        self.sent = []

    def send_get_devices(self, **kwargs):
        self.sent.append(("devices", kwargs))

    def send_get_status(self, **kwargs):
        self.sent.append(("status", kwargs))

    def drain_events(self, timeout_s=0.0):
        return []


class FakeGateway:
    def __init__(self, events, got_closed=True):
        self.events = events
        self.got_closed = got_closed
        self.installed = False

    def install_transport_hooks(self):
        self.installed = True

    def wait_rx_window(self, send_fn, collect_pred=None, fail_safe_s=8.0, *, stop_on_match=False):
        send_fn()
        collected = []
        for ev in self.events:
            if collect_pred and collect_pred(ev):
                collected.append(ev)
                if stop_on_match:
                    break
        return collected, self.got_closed

    def send_and_collect(
        self,
        send_fn,
        collect_pred,
        *,
        expected=None,
        idle_timeout_s=0.6,
        max_timeout_s=5.0,
    ):
        """Test shim -- prod collector uses idle/max timeouts + Condition."""
        send_fn()
        collected = []
        for ev in self.events:
            if collect_pred(ev):
                collected.append(ev)
                if expected is not None and len(collected) >= int(expected):
                    break
        return collected

    @staticmethod
    def compute_collect_max_timeout(expected, *, base_s=1.0, per_device_s=0.15, ceiling_s=5.0):
        n = max(0, int(expected))
        return min(ceiling_s, base_s + n * float(per_device_s))


class FakeController:
    def __init__(self, devices):
        self._devices = devices
        self.transport = FakeTransport()
        self.group_assignments = []

    def _to_hex_str(self, value):
        if isinstance(value, (bytes, bytearray)):
            return bytes(value).hex().upper()
        return str(value or "").upper()

    def getDeviceFromAddress(self, addr):
        want = str(addr or "").upper()
        for dev in self._devices:
            if dev.addr == want or dev.addr.endswith(want):
                return dev
        return None

    def setNodeGroupId(self, dev):
        self.group_assignments.append((dev.addr, dev.groupId))

    @property
    def device_repository(self):
        class Repo:
            def __init__(self, items):
                self._items = items

            def list(self):
                return self._items

        return Repo(self._devices)


class DiscoveryAndStatusTests(unittest.TestCase):
    def test_discovery_service_assigns_group_to_responders(self):
        dev = RL_Device("AABBCCDDEEFF", 1, "Node", groupId=0)
        controller = FakeController([dev])
        gateway = FakeGateway(
            [
                {
                    "opc": LP.OPC_DEVICES,
                    "reply": "IDENTIFY_REPLY",
                    "mac6": bytes.fromhex("AABBCCDDEEFF"),
                    "sender3": bytes.fromhex("DDEEFF"),
                }
            ]
        )
        service = DiscoveryService(controller, gateway)

        result = service.discover_devices(group_filter=0, add_to_group=4)

        self.assertTrue(gateway.installed)
        self.assertEqual(result["found"], 1)
        self.assertEqual(result["responders"], {"AABBCCDDEEFF"})
        self.assertEqual(dev.groupId, 4)
        self.assertEqual(controller.group_assignments, [("AABBCCDDEEFF", 4)])

    def test_discovery_service_in_groups_sweeps_each_id(self):
        """``discover_devices_in_groups`` fans out one OPC_DEVICES per
        group id and merges responders. Used by the WebUI's "Discover
        in: All groups" sweep — see broadcast-ruleset.md and the
        roadmap entry for the future single-packet replacement.
        """
        dev_a = RL_Device("AABBCCDDEEFF", 1, "A", groupId=2)
        dev_b = RL_Device("001122334455", 1, "B", groupId=3)
        controller = FakeController([dev_a, dev_b])
        # FakeGateway returns the same canned events for every send;
        # both group-2 and group-3 sweeps will record the same
        # IDENTIFY_REPLY twice. The assertion is on the SEND fan-out
        # count, not on responder uniqueness post-sweep.
        gateway = FakeGateway(
            [
                {
                    "opc": LP.OPC_DEVICES,
                    "reply": "IDENTIFY_REPLY",
                    "mac6": bytes.fromhex("AABBCCDDEEFF"),
                    "sender3": bytes.fromhex("DDEEFF"),
                }
            ]
        )
        service = DiscoveryService(controller, gateway)

        result = service.discover_devices_in_groups(group_ids=[2, 3])

        # Two sends, one per group filter.
        send_calls = [s for s in controller.transport.sent if s[0] == "devices"]
        self.assertEqual(len(send_calls), 2)
        emitted_filters = sorted(call[1].get("group_id") for call in send_calls)
        self.assertEqual(emitted_filters, [2, 3])
        # Responders merge into a set (no duplicates even though the
        # canned reply fired twice).
        self.assertEqual(result["responders"], {"AABBCCDDEEFF"})

    def test_discovery_service_in_groups_skips_invalid_ids(self):
        # Out-of-range / non-int ids are skipped silently rather than
        # crashing the sweep — the API may pass through malformed input
        # and the worker shouldn't blow up the task.
        controller = FakeController([])
        gateway = FakeGateway([])
        service = DiscoveryService(controller, gateway)

        result = service.discover_devices_in_groups(
            group_ids=[1, 255, -1, "bogus", 5],
        )

        send_calls = [s for s in controller.transport.sent if s[0] == "devices"]
        emitted = sorted(call[1].get("group_id") for call in send_calls)
        self.assertEqual(emitted, [1, 5])
        self.assertEqual(result["found"], 0)

    def test_status_service_marks_non_responders_offline_on_window_close(self):
        responding = RL_Device("AABBCCDDEEFF", 1, "Node A", groupId=2)
        silent = RL_Device("001122334455", 1, "Node B", groupId=2)
        controller = FakeController([responding, silent])
        gateway = FakeGateway(
            [
                {
                    "opc": LP.OPC_STATUS,
                    "reply": "STATUS_REPLY",
                    "mac6": bytes.fromhex("AABBCCDDEEFF"),
                    "sender3": bytes.fromhex("DDEEFF"),
                }
            ],
            got_closed=True,
        )
        service = StatusService(controller, gateway)

        result = service.get_status(group_filter=2)

        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["responders"], {"AABBCCDDEEFF"})
        self.assertFalse(silent.link_online)
        self.assertEqual(silent.link_error, "Missing reply (STATUS)")


if __name__ == "__main__":
    unittest.main()
