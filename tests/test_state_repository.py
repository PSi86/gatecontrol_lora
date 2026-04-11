import unittest

from racelink.domain import RL_Device, RL_DeviceGroup
from racelink.state.repository import DeviceRepository, GroupRepository, StateRepository


class RepositoryTests(unittest.TestCase):
    def test_device_repository_upsert_and_lookup_by_addr_and_last3(self):
        repo = DeviceRepository()
        dev = RL_Device("AABBCCDDEEFF", 1, "Node A")

        repo.upsert(dev)

        self.assertIs(repo.get_by_addr("AABBCCDDEEFF"), dev)
        self.assertIs(repo.get_by_addr("DDEEFF"), dev)

        replacement = RL_Device("AABBCCDDEEFF", 1, "Node B")
        repo.upsert(replacement)

        self.assertEqual(repo.list(), [replacement])
        self.assertIs(repo.get_by_addr("DDEEFF"), replacement)

    def test_group_repository_append_remove_and_len(self):
        repo = GroupRepository()
        gid0 = repo.append(RL_DeviceGroup("Group 0"))
        gid1 = repo.append(RL_DeviceGroup("Group 1"))

        self.assertEqual(gid0, 0)
        self.assertEqual(gid1, 1)
        self.assertEqual(len(repo), 2)

        repo.remove(0)

        self.assertEqual(len(repo), 1)
        self.assertEqual(repo.get(0).name, "Group 1")

    def test_state_repository_uses_provided_lists(self):
        devices = [RL_Device("001122334455", 1, "Node")]
        groups = [RL_DeviceGroup("Group")]

        state = StateRepository(devices=devices, groups=groups, backup_devices=[], backup_groups=[])

        self.assertIs(state.devices.list(), devices)
        self.assertIs(state.groups.list(), groups)


if __name__ == "__main__":
    unittest.main()
