"""Tokenfather's event log and rollup files on the shared `fathers_common` writers (fathers#117, fathers#208)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "common"))
import eventcheck  # pyright: ignore[reportMissingImports]

from fathers_common import atomic as common_atomic
from fathers_common import events as common_events
from tokenfather import events
from tokenfather.store import RollupStore


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="tokenfather-shared-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log = self.tmp / "events.jsonl"


class EventTests(Case):
    def test_an_event_is_schema_v1_from_tokenfather_and_validates_strictly(self) -> None:
        found = events.event("usage.collected", {"type": "rollup", "id": "2026-11-22", "skip": None},
                             {"message": "collected\n3\u2028requests", "requests": 3, "ratio": float("nan")})
        self.assertEqual((1, "tokenfather", "usage.collected"), (found["v"], found["source"], found["kind"]))
        self.assertEqual({"type": "tool", "name": "tokenfather", "pid": os.getpid()}, found["actor"])
        self.assertEqual({"type": "rollup", "id": "2026-11-22"}, found["subject"])
        self.assertEqual("collected 3 requests", found["details"]["message"])
        self.assertIsNone(found["details"]["ratio"])
        self.assertNotIn("level", found)
        self.assertRegex(found["time"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        self.assertTrue(events.append(found, self.log))
        self.assertEqual(0o600, self.log.stat().st_mode & 0o777)
        problems: list[str] = eventcheck.validate_file(str(self.log), strict=True)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        self.assertEqual([], problems)

    def test_the_shared_writer_does_the_appending_capping_and_rotating(self) -> None:
        found = events.event("usage.daily", {"type": "rollup", "id": "day"}, {"message": "m", "blob": "x" * 40000})
        line = events.encode(found)
        self.assertLessEqual(len(line), common_events.MAX_LINE)
        self.assertEqual(common_events.encode_event(found), line)
        with mock.patch.object(common_events, "append_event", wraps=common_events.append_event) as shared:
            self.assertTrue(events.append(found, self.log))
        shared.assert_called_once_with(self.log, found, rotate_at=events.ROTATE_AT, lock_wait=events.LOCK_WAIT)
        self.log.write_bytes(b"x" * (common_events.ROTATE_AT - 10) + b"\n")
        self.assertTrue(events.append(found, self.log))
        self.assertTrue(self.log.with_name("events.jsonl.1").is_file())
        rows: list[dict[str, Any]] = [json.loads(row) for row in self.log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(["usage.daily"], [row["kind"] for row in rows])

    def test_the_log_path_follows_tokenfather_events(self) -> None:
        with mock.patch.dict(os.environ, {"TOKENFATHER_EVENTS": str(self.log)}):
            self.assertEqual(self.log, events.path())
            self.assertTrue(events.append(events.event("usage.daily", {"type": "rollup", "id": "d"}, {})))
        self.assertTrue(self.log.is_file())


class StoreWriteTests(Case):
    def test_cursors_are_written_atomically_private_and_durably_through_the_shared_writer(self) -> None:
        store = RollupStore(self.tmp / "state", events_log=self.log)
        with mock.patch("tokenfather.store.atomic_write", wraps=common_atomic.atomic_write) as shared, \
                mock.patch.object(common_atomic, "sync_directory", wraps=common_atomic.sync_directory) as synced:
            store.save_cursors({"/a.jsonl": {"inode": 1, "offset": 2}})
        shared.assert_called_once()
        synced.assert_called_once_with(store.cursors_path.parent)
        self.assertEqual({"/a.jsonl": {"inode": 1, "offset": 2}}, store.load_cursors())
        self.assertEqual(0o600, store.cursors_path.stat().st_mode & 0o777)
        self.assertEqual(["cursors.json"], [p.name for p in store.state_dir.iterdir()], "no temporary file is left")


if __name__ == "__main__":
    unittest.main()
