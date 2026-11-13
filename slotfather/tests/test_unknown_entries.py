#!/usr/bin/env python3
"""Entries this client does not know (fathers#19): a state other than waiting or held, a count without a state, or
an entry without a field the scheduler needs (pid, start, count, since). Such an entry may be a newer client's live
lease, so it is left alone: never granted, reaped, ranked or deleted, its slots counted neither held nor free, and
shown as `unknown` by `status`, `status --json` and the dashboard block. No client raises on it."""
from __future__ import annotations
import sys as _path_sys
from pathlib import Path as _Path
_path_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "src"))

import json
import shutil
import tempfile
import unittest
from typing import Any

from slotfather import board as sf_board
from slotfather import entry as sf_entry
from slotfather import leases as sf_leases
from slotfather import query as sf_query
from slotfather import state as sf_state
from test_entry_pins import START, Queue

UNKNOWN: dict[str, dict[str, Any]] = {
    "a state this client does not know": {"state": "paused"},
    "a count without a state": {"state": None},
    "no pid": {"pid": None},
    "no start": {"start": None},
    "no since": {"since": None},
    "no count": {"count": None},
}


class UnknownEntryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = _Path(tempfile.mkdtemp(prefix="slotfather-unknown-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def queue(self, change: dict[str, Any], alive: bool = True) -> tuple[Queue, _Path, bytes]:
        """A queue of 4 slots: a held 2-slot lease, the unknown entry (4 slots, which would block everything were
        they counted or it ranked first), and a 2-slot waiter behind it."""
        queue = Queue(self.root / str(len(list(self.root.iterdir()))))
        queue.entry(90101, "running", 2, "held", since=-900)
        strange = queue.entry(90102, "strange", 4, "waiting", since=-800, alive=alive)
        raw: dict[str, Any] = json.loads(strange.read_text())
        for key, value in change.items():
            if value is None:
                raw.pop(key, None)
            else:
                raw[key] = value
        strange.write_text(json.dumps(raw) + "\n")
        queue.entry(90103, "behind", 2, since=-700)
        return queue, strange, strange.read_bytes()

    def test_it_is_never_granted_reaped_or_deleted(self) -> None:
        for why, change in UNKNOWN.items():
            for alive in (True, False):
                with self.subTest(why, alive=alive):
                    queue, strange, before = self.queue(change, alive)
                    with queue.running():
                        reason = sf_leases.grant(queue.directory, sf_leases.process_table(queue.directory))
                        live = sf_leases.reap(queue.directory, sf_leases.process_table(queue.directory))
                    self.assertIsNone(reason)
                    self.assertEqual(before, strange.read_bytes(), "left as it was")
                    self.assertEqual(["running", "behind"], [e["label"] for _, e in live])
                    behind = json.loads(next(queue.directory.glob("queue/*-90103.json")).read_text())
                    self.assertEqual("held", behind["state"], "planned as if it were absent")
                    log = (queue.directory / "queue.log").read_text()
                    self.assertNotIn("strange", log)
                    self.assertFalse((queue.directory / "history.jsonl").exists(), "nothing left the queue")

    def test_status_shows_it_as_unknown(self) -> None:
        for why, change in UNKNOWN.items():
            with self.subTest(why):
                queue, strange, before = self.queue(change)
                with queue.running():
                    lines = sf_query.status_lines(queue.directory)
                    listing = sf_query.status_json(queue.directory)
                    full = sf_board.full_status(queue.directory)
                self.assertEqual(before, strange.read_bytes())
                warning = next(line for line in lines if line.startswith("unknown:"))
                self.assertIn(f"queue/{strange.name}", warning)
                self.assertIn("leaves it alone", warning)
                self.assertEqual("2 of 4 slots held", lines[-1], "only the known lease's slots")
                found = {item["id"]: item for item in listing["entries"]}
                self.assertEqual("unknown", found[strange.stem]["phase"])
                self.assertIsNone(found[strange.stem]["position"])
                self.assertEqual({"held", "waiting"}, {item["phase"] for name, item in found.items() if name != strange.stem})
                self.assertEqual((2, 2), (listing["held"], listing["free"]))
                block = full["dashboard"]
                self.assertEqual([strange.stem], [item["id"] for item in block["unknown"]])
                self.assertIn("leaves it alone", block["unknown"][0]["warning"])
                self.assertNotIn("strange", [row["label"] for row in block["holders"] + block["waiters"]])

    def test_the_classifier(self) -> None:
        known = {"pid": 1, "start": START, "count": 1, "since": "2026-01-01T00:00:00Z", "state": "waiting"}
        self.assertIsNone(sf_entry.unknown(known))
        self.assertIsNone(sf_entry.unknown(dict(known, state="held")))
        self.assertIsNone(sf_entry.unknown(dict(known, start="")), "an empty start matches any holder, as before")
        for why, change in UNKNOWN.items():
            with self.subTest(why):
                entry: dict[str, Any] = dict(known)
                for key, value in change.items():
                    if value is None:
                        entry.pop(key, None)
                    else:
                        entry[key] = value
                self.assertIsNotNone(sf_entry.unknown(entry))
                self.assertEqual("unknown", sf_entry.phase(entry).name)
                self.assertFalse(sf_entry.is_held(entry) or sf_entry.is_waiting(entry))
                self.assertEqual(([], []), sf_entry.split([entry]))
        self.assertIsNotNone(sf_entry.unknown(dict(known, pid=True)), "a pid that is not a number")
        self.assertIsNotNone(sf_entry.unknown(dict(known, count="2")))
        self.assertEqual("unknown", sf_entry.phase(dict(known, state="held", pid="x")).name)

    def test_state_lists_it_and_entries_keep_their_contract(self) -> None:
        queue, strange, _ = self.queue({"count": None})
        names = [path.name for path, _ in sf_state.unknown(queue.directory)]
        self.assertEqual([strange.name], names)
        self.assertNotIn(strange.name, [path.name for path, _ in sf_state.entries(queue.directory)],
                         "entries() still needs an int count, as older readers expect")


if __name__ == "__main__":
    unittest.main()
