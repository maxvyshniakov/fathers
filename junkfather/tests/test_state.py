"""The entry files: a write is durable and atomic, and a write that fails leaves the old file and no temp file."""
from __future__ import annotations

import json
import os
from typing import Any, Callable, List
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
        calls: List[str] = []
        real_fsync, real_replace = os.fsync, os.replace

        def fsync(fd: int) -> None:
            calls.append("fsync")
            real_fsync(fd)

        def replace(a: Any, b: Any) -> None:
            calls.append("replace")
            real_replace(a, b)
        with mock.patch.object(state.os, "fsync", side_effect=fsync), \
                mock.patch.object(state.os, "replace", side_effect=replace):
            state.write_atomic(path, {"id": "j1"})
        self.assertEqual("fsync", calls[0])
        self.assertIn("replace", calls)
        self.assertEqual({"id": "j1"}, json.loads(path.read_text()))

    def test_a_directory_that_cannot_be_synced_fails_the_write_unless_its_filesystem_has_no_such_sync(self):
        import errno
        path = self.tmp / "entries" / "j1.json"
        real_fsync = os.fsync

        def fsync_failing(code: int) -> Callable[[int], None]:
            def fsync(fd: int) -> None:
                if os.path.isdir(f"/dev/fd/{fd}"):
                    raise OSError(code, os.strerror(code))
                real_fsync(fd)
            return fsync
        with mock.patch.object(state.os, "fsync", side_effect=fsync_failing(errno.EIO)), self.assertRaises(OSError):
            state.write_atomic(path, {"id": "j1"})
        with mock.patch.object(state.os, "fsync", side_effect=fsync_failing(errno.EINVAL)):
            state.write_atomic(path, {"id": "j2"})
        self.assertEqual({"id": "j2"}, json.loads(path.read_text()))


if __name__ == "__main__":
    unittest.main()
