"""The entry files: `state.write_atomic` serializes JSON and delegates atomic replacement to `fathers_common.atomic`."""
from __future__ import annotations

import json
import os
import unittest

from support import Case

from junkfather import state


class WriteAtomicTests(Case):
    def test_a_write_that_fails_midway_keeps_the_old_content_and_leaves_no_temp_file(self):
        path = self.tmp / "entries" / "j1.json"
        state.write_atomic(path, {"id": "j1", "state": "running"})
        self.assertEqual({"id": "j1", "state": "running"}, json.loads(path.read_text()))
        before = path.read_text()
        with self.assertRaises(TypeError):  # json.dumps fails on the unserializable "z" before replacing path
            state.write_atomic(path, {"a": "x" * 50000, "z": object()})
        self.assertEqual(before, path.read_text())
        self.assertEqual(["j1.json"], sorted(os.listdir(path.parent)))


if __name__ == "__main__":
    unittest.main()
