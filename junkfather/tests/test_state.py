"""The entry files: a write is durable and atomic, and a write that fails leaves the old file and no temp file."""
from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from support import Case

from junkfather import state


class WriteAtomicTests(Case):
    def test_a_write_that_fails_midway_keeps_the_old_content_and_leaves_no_temp_file(self):
        path = self.tmp / "entries" / "j1.json"
        state.write_atomic(path, {"id": "j1", "state": "running"})
        before = path.read_text()
        with self.assertRaises(TypeError):  # json.dump has written "a" before it reaches the unserializable "z"
            state.write_atomic(path, {"a": "x" * 50000, "z": object()})
        self.assertEqual(before, path.read_text())
        self.assertEqual(["j1.json"], sorted(os.listdir(path.parent)))

    def test_a_write_reaches_the_disk_before_it_replaces_the_file(self):
        path = self.tmp / "entries" / "j1.json"
        calls = []
        real_fsync, real_replace = os.fsync, os.replace
        with mock.patch.object(state.os, "fsync", side_effect=lambda fd: (calls.append("fsync"), real_fsync(fd))), \
                mock.patch.object(state.os, "replace",
                                  side_effect=lambda a, b: (calls.append("replace"), real_replace(a, b))):
            state.write_atomic(path, {"id": "j1"})
        self.assertEqual("fsync", calls[0])
        self.assertIn("replace", calls)
        self.assertEqual({"id": "j1"}, json.loads(path.read_text()))


if __name__ == "__main__":
    unittest.main()
