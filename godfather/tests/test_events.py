"""The event reader (fathers#83): a Tail follows one tool's events.jsonl by inode and offset through rotation, torn
and hostile lines and its start window; the Feed merges every source's events by time, source and file order."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

from helpers import ROOT

from godfather import events

HOST = "mbp"


def line(n: int, source: str = "slotfather", host: str = HOST, kind: str = "lease.granted",
         second: int | None = None, **extra: Any) -> str:
    event = {"v": 1, "time": f"2026-11-12T02:{(second if second is not None else n) // 60 % 60:02d}:"
                             f"{(second if second is not None else n) % 60:02d}.000Z",
             "host": host, "source": source, "kind": kind, "subject": {"type": "request", "id": f"r{n}"},
             "actor": {"type": "tool"}, "details": {"n": n}}
    event.update(extra)
    return json.dumps(event) + "\n"


class TailTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "events.jsonl"
        self.tail = events.Tail("slotfather", self.path, HOST)
        self.addCleanup(self.tail.close)

    def append(self, text: str | bytes) -> None:
        with open(self.path, "ab") as handle:
            handle.write(text.encode() if isinstance(text, str) else text)

    def numbers(self) -> list[int]:
        return [event.details["n"] for event in self.tail.read()]

    def test_a_missing_log_gives_nothing_until_it_appears(self) -> None:
        self.assertEqual([], self.numbers())
        self.append(line(1))
        self.assertEqual([1], self.numbers())
        self.assertEqual([], self.numbers(), "read once")

    def test_appended_lines_are_read_once_in_file_order(self) -> None:
        self.append(line(1) + line(2))
        self.assertEqual([1, 2], self.numbers())
        self.append(line(3))
        self.append(line(4))
        self.assertEqual([3, 4], self.numbers())
        again = events.Tail("slotfather", self.path, HOST)
        self.addCleanup(again.close)
        orders = [event.order for event in again.read()]
        self.assertEqual(sorted(orders), orders)

    def test_a_torn_last_line_waits_until_it_is_whole(self) -> None:
        whole = line(2)
        self.append(line(1) + whole[:20])
        self.assertEqual([1], self.numbers())
        self.assertEqual([], self.numbers(), "still torn")
        self.append(whole[20:])
        self.assertEqual([2], self.numbers())
        self.assertEqual({"invalid": 0, "version": 0, "host": 0, "source": 0}, self.tail.skipped)

    def test_a_rotation_loses_no_line_and_repeats_none(self) -> None:
        self.append(line(1) + line(2))
        self.assertEqual([1, 2], self.numbers())
        self.append(line(3))  # written before the rename, not read yet
        os.replace(self.path, self.path.with_name("events.jsonl.1"))
        self.append(line(4) + line(5))
        self.assertEqual([3, 4, 5], self.numbers())
        self.append(line(6))
        self.assertEqual([6], self.numbers())

    def test_a_rotation_with_no_new_log_yet_waits_for_it(self) -> None:
        self.append(line(1))
        self.numbers()
        self.append(line(2))
        os.replace(self.path, self.path.with_name("events.jsonl.1"))
        self.assertEqual([2], self.numbers(), "the rest of the old file")
        self.append(line(3))
        self.assertEqual([3], self.numbers())

    def test_a_log_truncated_in_place_is_read_from_its_start(self) -> None:
        self.append(line(1) + line(2))
        self.numbers()
        self.path.write_text(line(3))
        self.assertEqual([3], self.numbers())

    def test_invalid_unknown_version_and_other_host_lines_are_skipped_and_counted(self) -> None:
        self.append("not json\n" + line(1) + line(2, v=2) + line(3, host="vm110") + line(5, source="lanefather")
                    + "[1, 2]\n"
                    + json.dumps({"v": 1}) + "\n" + line(4))
        self.assertEqual([1, 4], self.numbers())
        self.assertEqual({"invalid": 3, "version": 1, "host": 1, "source": 1}, self.tail.skipped)

    def test_a_host_name_that_changes_with_the_network_is_followed(self) -> None:
        """fathers#84 review: the tools name the host on every write; a Tail made for this machine looks it up on
        every read, so a new network name does not empty EVENTS for the rest of the session."""
        from unittest import mock

        tail = events.Tail("slotfather", self.path)
        self.addCleanup(tail.close)
        with mock.patch("socket.gethostname", return_value="Wintermute.local"):
            self.append(line(1, host="wintermute"))
            self.assertEqual([1], [event.details["n"] for event in tail.read()])
        with mock.patch("socket.gethostname", return_value="wintermute-2.lan"):
            self.append(line(2, host="wintermute") + line(3, host="wintermute-2"))
            self.assertEqual([3], [event.details["n"] for event in tail.read()])
        self.assertEqual(1, tail.skipped["host"])

    def test_one_read_takes_at_most_its_share_and_the_rest_next_time(self) -> None:
        from unittest import mock

        self.append(line(1))
        self.numbers()
        self.append("".join(line(n) for n in range(2, 12)))
        with mock.patch.object(events, "READ_MAX", len(line(2)) * 3):
            first = self.numbers()
            os.replace(self.path, self.path.with_name("events.jsonl.1"))
            self.append(line(12))
            later = [self.numbers() for _ in range(5)]
        self.assertEqual([2, 3, 4], first)
        self.assertEqual(list(range(2, 13)), first + [n for found in later for n in found], "none lost or repeated")

    def test_hostile_lines_never_reach_a_reader_nor_stop_it(self) -> None:
        escape = line(1, subject={"type": "request", "id": "r1", "label": "\x1b]52;c;cGF3bmVk\x07"})
        nested = '{"v": 1, "details": ' + "[" * 5000 + "]" * 5000 + "}\n"
        oversized = line(2, details={"x": "a" * events.LINE_MAX})
        self.append(escape + nested + oversized + line(3))
        found = self.tail.read()
        self.assertEqual([3], [event.details["n"] for event in found])
        self.assertEqual(3, self.tail.skipped["invalid"])
        self.assertNotIn("\x1b", "".join(event.line for event in found))

    def test_a_line_longer_than_the_limit_is_skipped_even_while_it_grows(self) -> None:
        self.append(line(1) + "x" * (events.LINE_MAX + 10))
        self.assertEqual([1], self.numbers())
        self.append("y" * 100)
        self.assertEqual([], self.numbers())
        self.append("z\n" + line(2))
        self.assertEqual([2], self.numbers())
        self.assertEqual(1, self.tail.skipped["invalid"])

    def test_the_start_window_is_the_newest_events_within_the_byte_limit(self) -> None:
        self.append("".join(line(n, second=n % 3600) for n in range(events.START_EVENTS + 50)))
        found = self.numbers()
        self.assertEqual(events.START_EVENTS, len(found))
        self.assertEqual(events.START_EVENTS + 49, found[-1])
        size = len(line(1, details={"x": "a" * 2000}))
        self.path.unlink()
        self.tail.close()
        count = events.START_BYTES // size + 20
        self.append("".join(line(n, details={"n": n, "x": "a" * 2000}) for n in range(count)))
        again = events.Tail("slotfather", self.path, HOST)
        self.addCleanup(again.close)
        found = again.read()
        self.assertLess(len(found), count)
        self.assertEqual(count - 1, found[-1].details["n"])
        self.assertLessEqual(sum(len(event.line) + 1 for event in found), events.START_BYTES)

    def test_the_start_window_takes_the_rotated_logs_end_while_the_log_is_short(self) -> None:
        self.path.with_name("events.jsonl.1").write_text(line(1) + line(2) + line(3)[:15])
        self.append(line(4))
        self.assertEqual([1, 2, 4], self.numbers(), "the rotated log's torn end is dropped")


class FeedTests(unittest.TestCase):
    def test_sources_merge_by_time_then_source_then_file_order(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "slot.jsonl").write_text(line(1, second=5) + line(2, second=5) + line(3, second=9))
            (root / "lane.jsonl").write_text(line(4, "lanefather", second=5) + line(5, "lanefather", second=7))
            feed = events.Feed(HOST)
            feed.follow("slotfather", str(root / "slot.jsonl"))
            feed.follow("lanefather", str(root / "lane.jsonl"))
            self.assertEqual([4, 1, 2, 5, 3], [event.details["n"] for event in feed.poll()])
            self.assertEqual([], feed.poll())
            with open(root / "slot.jsonl", "a") as handle:
                handle.write(line(6, second=6))
            self.assertEqual([6], [event.details["n"] for event in feed.poll()])
            self.assertEqual([4, 1, 2, 6, 5, 3], [event.details["n"] for event in feed.events()])
            feed.follow("lanefather", None)
            self.assertEqual([1, 2, 6, 3], [event.details["n"] for event in feed.events()])
            self.assertEqual({"slotfather": {"invalid": 0, "version": 0, "host": 0, "source": 0}}, feed.skipped())
            feed.close()


class ValidatorCopyTests(unittest.TestCase):
    def test_the_packaged_validator_is_the_shared_one_unchanged(self) -> None:
        shared = ROOT.parent / "common" / "eventcheck.py"
        if not shared.is_file():
            self.skipTest("outside the fathers repository")
        self.assertEqual(shared.read_bytes(), (ROOT / "src" / "godfather" / "eventcheck.py").read_bytes(),
                         "copy common/eventcheck.py to src/godfather/eventcheck.py")


if __name__ == "__main__":
    unittest.main()
