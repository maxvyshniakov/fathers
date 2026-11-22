"""The entry files: `state.write_atomic` serializes JSON and delegates atomic replacement to `fathers_common.atomic`."""
from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from support import Case

from spawnfather import state


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

    def test_state_dir_prefers_spawnfather_state_falls_back_to_junkfather_state_and_uses_xdg(self) -> None:
        home = self.tmp / "home"
        with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=True):
            self.assertEqual(home / ".local" / "state" / "spawnfather", state.state_dir())
        with mock.patch.dict(os.environ, {"HOME": str(home), "XDG_STATE_HOME": str(self.tmp / "xdg")}, clear=True):
            self.assertEqual(self.tmp / "xdg" / "spawnfather", state.state_dir())
        with mock.patch.dict(os.environ, {"JUNKFATHER_STATE": str(self.tmp / "legacy")}, clear=True):
            self.assertEqual(self.tmp / "legacy", state.state_dir())
        with mock.patch.dict(os.environ, {"JUNKFATHER_STATE": str(self.tmp / "legacy"),
                                          "SPAWNFATHER_STATE": str(self.tmp / "new")}, clear=True):
            self.assertEqual(self.tmp / "new", state.state_dir())


if __name__ == "__main__":
    unittest.main()
