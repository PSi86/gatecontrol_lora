"""Tests for the debounced StandaloneOptionStore (plan P3-4)."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest

from racelink.integrations.standalone.config import StandaloneConfig, StandaloneOptionStore


class StandaloneOptionStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.config_path = os.path.join(self._tmpdir.name, "standalone_config.json")
        self.config = StandaloneConfig(path=self.config_path)

    def _read_disk(self) -> dict:
        if not os.path.exists(self.config_path):
            return {}
        with open(self.config_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def test_option_set_without_debounce_is_synchronous(self):
        store = StandaloneOptionStore(self.config, debounce_seconds=0)
        store.option_set("key", "value")
        self.assertEqual(self._read_disk().get("options", {}).get("key"), "value")

    def test_option_set_with_debounce_batches_writes(self):
        store = StandaloneOptionStore(self.config, debounce_seconds=0.05)
        for i in range(10):
            store.option_set(f"key{i}", i)
        # Immediately after the last set, nothing has been written yet.
        self.assertEqual(self._read_disk(), {})

        # Wait for the debounce to elapse + small margin.
        time.sleep(0.15)
        options = self._read_disk().get("options", {})
        # All 10 writes should have coalesced into a single disk file.
        self.assertEqual(options.get("key9"), 9)

    def test_flush_forces_immediate_save(self):
        store = StandaloneOptionStore(self.config, debounce_seconds=5.0)
        store.option_set("foo", "bar")
        self.assertEqual(self._read_disk(), {})
        store.flush()
        self.assertEqual(self._read_disk().get("options", {}).get("foo"), "bar")

    def test_atomic_save_recovers_from_tempfile_gc(self):
        store = StandaloneOptionStore(self.config, debounce_seconds=0)
        store.option_set("a", 1)
        # No leftover .tmp files in the dir.
        residue = [n for n in os.listdir(self._tmpdir.name) if n.startswith(".standalone_config-")]
        self.assertEqual(residue, [])


if __name__ == "__main__":
    unittest.main()
