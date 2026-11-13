#!/usr/bin/env python3
"""Queue files survive a crash and stay bounded (fathers#123): entry writes are fsynced before and after their
rename, the sequence file is written whole and a torn one never sends a request ahead of the queue, an empty
entry file is left alone and reported, and queue.log rotates at its size limit without losing a line."""
from __future__ import annotations
import sys as _path_sys
from pathlib import Path as _Path
_path_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "src"))

import datetime
import json
import os
import shutil
import stat
import tempfile
import unittest
from typing import Any
from unittest import mock

from slotfather import board as sf_board
from slotfather import leases as sf_leases
from slotfather import state as sf_state
from test_slotfather import SlotCase


class WriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = _Path(tempfile.mkdtemp(prefix="slotfather-durable-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def test_an_entry_is_fsynced_before_its_rename_and_its_directory_after(self) -> None:
        path = self.dir / "entry.json"
        synced: list[tuple[str, bool]] = []
        real = os.fsync

        def fsync(fd: int) -> None:
            directory = stat.S_ISDIR(os.fstat(fd).st_mode)
            synced.append(("dir" if directory else "file", path.exists()))
            real(fd)
        with mock.patch.object(sf_state.os, "fsync", side_effect=fsync):
            sf_state.write(path, {"pid": 1})
        self.assertEqual([("file", False), ("dir", True)], synced)
        self.assertEqual({"pid": 1}, json.loads(path.read_text()))
        self.assertEqual([path], list(self.dir.iterdir()), "no .pending file is left")

    def test_a_short_write_never_replaces_a_good_entry(self) -> None:
        path = self.dir / "entry.json"
        path.write_text('{"pid": 1}\n')
        real = os.write

        def short(fd: int, data: bytes) -> int:
            return real(fd, data[: len(data) // 2])  # a full disk takes half
        real_open = open

        class FullDisk:
            """A file on a disk that fills half way through the write."""
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                self.file: Any = real_open(*args, **kwargs)

            def __enter__(self) -> "FullDisk":
                return self

            def __exit__(self, *_: Any) -> None:
                self.file.close()

            def write(self, text: str) -> int:
                self.file.write(text[: len(text) // 2])
                self.file.flush()
                raise OSError(28, "No space left on device")

            def __getattr__(self, name: str) -> Any:
                return getattr(self.file, name)
        with mock.patch.object(sf_state.os, "write", side_effect=short), \
                mock.patch.object(sf_state, "open", FullDisk, create=True):
            with self.assertRaises(OSError):
                sf_state.write(path, {"pid": 2, "label": "x" * 100})
        self.assertEqual({"pid": 1}, json.loads(path.read_text()))
        self.assertEqual([path], list(self.dir.iterdir()), "the .pending file of a failed write is removed")

    def test_a_failed_sync_leaves_the_entry_and_no_pending_file(self) -> None:
        path = self.dir / "entry.json"
        path.write_text('{"pid": 1}\n')
        with mock.patch.object(sf_state.os, "fsync", side_effect=OSError(5, "I/O error")):
            with self.assertRaises(OSError):
                sf_state.write(path, {"pid": 2})
        self.assertEqual({"pid": 1}, json.loads(path.read_text()))
        self.assertEqual([path], list(self.dir.iterdir()))

    def test_a_sequence_whose_write_fails_keeps_its_value(self) -> None:
        (self.dir / "queue").mkdir()
        (self.dir / "sequence").write_text("41")
        with mock.patch.object(sf_state.os, "fsync", side_effect=OSError(5, "I/O error")):
            with self.assertRaises(OSError):
                sf_leases.next_sequence(self.dir)
        self.assertEqual("41", (self.dir / "sequence").read_text(), "the counter is replaced whole or not at all")
        self.assertEqual(42, sf_leases.next_sequence(self.dir))
        self.assertEqual("42\n", (self.dir / "sequence").read_text())

    def test_a_second_rotator_leaves_the_rotated_part_alone(self) -> None:
        """Two writers find queue.log over the limit; the first rotates it, a third starts the new one, then the
        second rotates: it must not replace the full queue.log.1 with the new, nearly empty file."""
        path = self.dir / "queue.log"
        path.write_text("x" * 3000 + "\n")
        late = os.open(path, os.O_RDWR | os.O_APPEND)  # the second writer's file, opened before the rotation
        self.addCleanup(os.close, late)
        sf_state.rotate_bounded(path, os.open(path, os.O_RDONLY), 2000)
        sf_state.append_bounded(path, "new line\n", 2000)
        sf_state.rotate_bounded(path, late, 2000)
        self.assertEqual(3001, (self.dir / "queue.log.1").stat().st_size, "the full rotated part was replaced")
        self.assertEqual("new line\n", path.read_text())

    def test_the_dashboard_reads_a_rotation_between_its_two_reads_once(self) -> None:
        current = self.dir / "queue.log"
        current.write_text("2026-11-13T10:00:00Z granted 2 to build (pid 4242)\n")
        os.link(current, self.dir / "queue.log.1")  # queue.log.1 is the file just read: rotated meanwhile
        self.assertEqual(["granted"], [event["verb"] for event in sf_board.queue_events(self.dir)])

    def test_queue_log_rotates_at_its_limit_without_losing_a_line(self) -> None:
        with mock.patch.object(sf_state, "QUEUE_LOG_MAX", 2000):
            for number in range(60):
                sf_state.log(self.dir, f"granted 1 to line-{number:03d} (pid {1000 + number})")
        rotated, current = self.dir / "queue.log.1", self.dir / "queue.log"
        self.assertTrue(rotated.exists())
        self.assertLessEqual(current.stat().st_size, 2000)
        lines = rotated.read_text().splitlines() + current.read_text().splitlines()
        numbers = [int(line.split("line-")[1][:3]) for line in lines]
        self.assertEqual(list(range(numbers[0], 60)), numbers, "a line was lost or reordered at the rotation")
        self.assertLess(numbers[0], 60 - len(current.read_text().splitlines()), "the rotated part holds lines")

    def test_the_dashboard_reads_a_grant_left_in_the_rotated_part(self) -> None:
        (self.dir / "queue.log.1").write_text("2026-11-13T10:00:00Z granted 2 to build (pid 4242)\n")
        (self.dir / "queue.log").write_text("2026-11-13T10:05:00Z released 2 (0000000007-4242.json)\n")
        events = sf_board.queue_events(self.dir)
        self.assertEqual(["granted", "released"], [event["verb"] for event in events])
        self.assertEqual([2, -2], [event["delta"] for event in events])
        self.assertEqual(datetime.timezone.utc, events[0]["when"].tzinfo)


class QueueFileTests(SlotCase):
    def lease_name(self, output: str) -> str:
        line = next(line for line in output.splitlines() if line.startswith("lease="))
        return _Path(line.split("=", 1)[1]).name

    def test_a_torn_sequence_never_sends_a_request_ahead_of_the_queue(self) -> None:
        pid, start = self.holder()
        (self.state / "queue").mkdir(parents=True)
        (self.state / "queue" / f"{41:010d}-{pid}.json").write_text(json.dumps(
            {"pid": pid, "start": start, "count": 1, "label": "earlier", "cmd": "fake", "since": "-", "state": "held"}))
        for torn in ("", "4", "garbage"):
            with self.subTest(torn=torn):
                (self.state / "sequence").write_text(torn)
                ran = self.tool("run", "-n", "1", "--label", "next", "--", "sh", "-c", 'echo "lease=$SLOTFATHER_LEASE"')
                self.assertEqual(0, ran.returncode, ran.stderr)
                number = int(self.lease_name(ran.stdout).split("-")[0])
                self.assertGreater(number, 41, ran.stdout)
                self.assertEqual(number, int((self.state / "sequence").read_text()))

    def test_an_empty_entry_file_is_left_alone_and_reported(self) -> None:
        (self.state / "queue").mkdir(parents=True)
        empty = self.state / "queue" / f"{5:010d}-{os.getpid()}.json"
        empty.write_text("")
        status = self.tool("status")
        self.assertEqual(0, status.returncode, status.stderr)
        self.assertIn(f"unreadable: queue/{empty.name}", status.stdout)
        ran = self.tool("run", "-n", "1", "--label", "after", "--", "true")
        self.assertEqual(0, ran.returncode, ran.stderr)
        self.assertTrue(empty.exists())
        self.assertEqual("", empty.read_text())


if __name__ == "__main__":
    unittest.main()
