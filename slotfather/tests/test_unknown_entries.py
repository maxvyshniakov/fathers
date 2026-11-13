#!/usr/bin/env python3
"""Entries this client does not know (fathers#19): a state other than waiting or held, a count without a state, or
an entry without a field the scheduler needs (pid, start, count, since). Such an entry may be a newer client's live
lease, so it is left alone: never granted, reaped, ranked or deleted, its slots counted neither held nor free, and
shown as `unknown` by `status`, `status --json` and the dashboard block. No client raises on it."""
from __future__ import annotations
import sys as _path_sys
from pathlib import Path as _Path
_path_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "src"))

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from typing import Any, cast
from unittest import mock

from slotfather import board as sf_board
from slotfather import config as sf_config
from slotfather import entry as sf_entry
from slotfather import leases as sf_leases
from slotfather import platform as sf_platform
from slotfather import procs as sf_procs
from slotfather import query as sf_query
from slotfather import state as sf_state
from test_entry_pins import START, Queue
from test_slotfather import ROOT
from test_usage import load

install = cast(Any, load(str(ROOT / "install.py"), "slotfather_install_for_unknown"))

UNKNOWN: dict[str, dict[str, Any]] = {
    "a state this client does not know": {"state": "paused"},
    "a count without a state": {"state": None},
    "no pid": {"pid": None},
    "no start": {"start": None},
    "no since": {"since": None},
    "no count": {"count": None},
}
# The ones whose holder can be checked and that have a count: alive, they reserve their slots (fathers#19 review)
RESERVES = ("a state this client does not know", "a count without a state", "no since")


class UnknownEntryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = _Path(tempfile.mkdtemp(prefix="slotfather-unknown-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def queue(self, change: dict[str, Any], alive: bool = True) -> tuple[Queue, _Path, bytes]:
        """A queue of 4 slots: a held 2-slot lease, the unknown entry (2 slots, which are taken while its holder
        runs), and a 2-slot waiter behind it, which starts only if those slots are free."""
        queue = Queue(self.root / str(len(list(self.root.iterdir()))))
        queue.entry(90101, "running", 2, "held", since=-900)
        strange = queue.entry(90102, "strange", 2, "waiting", since=-800, alive=alive)
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
                    kept = alive and why in RESERVES
                    self.assertEqual(kept, reason is not None, reason)
                    self.assertEqual(before, strange.read_bytes(), "left as it was")
                    self.assertEqual(["running", "behind"], [e["label"] for _, e in live])
                    behind = json.loads(next(queue.directory.glob("queue/*-90103.json")).read_text())
                    self.assertEqual("waiting" if kept else "held", behind["state"],
                                     "its slots reserved while its holder runs, else planned as if it were absent")
                    log = queue.directory / "queue.log"
                    self.assertNotIn("strange", log.read_text() if log.exists() else "")
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
                holds = 2 if why in RESERVES else 0
                self.assertEqual(f"{2 + holds} of 4 slots held", lines[-1], "a live unknown entry's slots are held")
                self.assertIn("stay reserved" if holds else "remove", warning)
                found = {item["id"]: item for item in listing["entries"]}
                self.assertEqual("unknown", found[strange.stem]["phase"])
                self.assertIsNone(found[strange.stem]["position"])
                self.assertEqual({"held", "waiting"}, {item["phase"] for name, item in found.items() if name != strange.stem})
                self.assertEqual((2 + holds, 2 - holds), (listing["held"], listing["free"]))
                self.assertEqual(holds, found[strange.stem]["holds"])
                block = full["dashboard"]
                self.assertEqual([strange.stem], [item["id"] for item in block["unknown"]])
                self.assertIn("leaves it alone", block["unknown"][0]["warning"])
                self.assertEqual((holds, 2 + holds, 2 - holds),
                                 (block["unknown"][0]["holds"], block["used"], block["free"]))
                self.assertNotIn("strange", [row["label"] for row in block["holders"] + block["waiters"]])

    def test_a_live_unknown_entry_keeps_its_slots(self) -> None:
        # fathers#19 review: a newer client's live lease of 2 of 4 slots; a 3-slot request waits for them
        queue = Queue(self.root / "reserved")
        strange = queue.entry(90201, "newer", 2, "paused", since=-900)
        three = queue.entry(90202, "three", 3, since=-100)
        with queue.running():
            reason = sf_leases.grant(queue.directory, sf_leases.process_table(queue.directory))
            lines = sf_query.status_lines(queue.directory)
            listing = sf_query.status_json(queue.directory)
            block = sf_board.full_status(queue.directory)["dashboard"]
            free = sf_query.available(queue.directory)
        self.assertEqual("waiting", json.loads(three.read_text())["state"], reason)
        self.assertIn("2 of 4 slots free", reason or "")
        self.assertEqual("2 of 4 slots held", lines[-1])
        self.assertIn(f"queue/{strange.name}", next(line for line in lines if line.startswith("unknown:")))
        self.assertEqual((2, 2), (listing["held"], listing["free"]))
        self.assertEqual((2, 2), (block["used"], block["free"]))
        self.assertEqual(0, free, "the 3-slot waiter comes first in the 2 free slots")
        del queue.table[90201]  # its holder exits: the slots are free again, the file stays
        with queue.running():
            sf_leases.grant(queue.directory, sf_leases.process_table(queue.directory))
        self.assertEqual("held", json.loads(three.read_text())["state"])
        self.assertTrue(strange.exists())

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
        for bad in (dict(known, count=0), dict(known, state="held", count=-1), dict(known, pid=0)):
            with self.subTest(bad=bad):  # one rule (fathers#19 review): the scheduler and procs agree
                self.assertIsNotNone(sf_entry.unknown(bad))
                self.assertFalse(sf_entry.valid_identity(bad))
                self.assertEqual(([], []), sf_entry.split([bad]))
        self.assertTrue(sf_entry.valid_identity(known))
        self.assertFalse(sf_entry.valid_identity(dict(known, start="")), "procs needs a start to compare")
        self.assertEqual(0.0, sf_entry.held_for(dict(known, granted="2026-01-01T00:00:00Z", yielded="x"),
                                                sf_config.seconds("2026-01-01T00:00:00Z") or 0.0))
        self.assertEqual(5.0, sf_entry.yielded_of({"yielded": 5}))
        self.assertEqual((2, 0, 0), (sf_entry.reserved(dict(known, count=2)), sf_entry.reserved(dict(known, pid=None)),
                                     sf_entry.reserved(dict(known, count=None))))

    def test_state_lists_it_and_entries_keep_their_contract(self) -> None:
        queue, strange, _ = self.queue({"count": None})
        names = [path.name for path, _ in sf_state.unknown(queue.directory)]
        self.assertEqual([strange.name], names)
        self.assertNotIn(strange.name, [path.name for path, _ in sf_state.entries(queue.directory)],
                         "entries() still needs an int count, as older readers expect")



class DeadUnknownEntryTests(unittest.TestCase):
    """fathers#19 review: an unknown entry whose holder is provably gone blocks nothing. `procs --json` and the
    installer read it as absent, with a warning naming the file; one whose holder runs still refuses both. Nothing
    removes the file."""

    def setUp(self) -> None:
        self.directory = _Path(tempfile.mkdtemp(prefix="slotfather-unknown-"))
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        (self.directory / "queue").mkdir()

    def write(self, pid: int, start: str) -> _Path:
        path = self.directory / "queue" / f"0000000001-{pid}.json"
        path.write_text(json.dumps({"pid": pid, "start": start, "count": 2, "label": "newer", "cmd": "x",
                                    "since": "2026-01-01T00:00:00Z", "state": "paused"}) + "\n")
        return path

    def own(self) -> tuple[int, str]:
        start = subprocess.run(["ps", "-o", "lstart=", "-p", str(os.getpid())], capture_output=True, text=True,
                               env=sf_platform.c_locale()).stdout.strip()
        return os.getpid(), start

    def test_procs_reads_a_dead_one_as_absent_and_refuses_a_live_one(self) -> None:
        child = subprocess.Popen(["true"])
        child.wait()
        path = self.write(child.pid, START)
        with mock.patch.dict(os.environ, {"SLOTFATHER_LEGACY_DIRS": ""}):
            found = sf_procs.query(pid=os.getpid(), directory=self.directory)
            self.assertEqual(1, len(found["warnings"]))
            self.assertIn(str(path), found["warnings"][0])
            self.assertTrue(path.exists())
            path.unlink()
            self.write(*self.own())
            with self.assertRaises(sf_procs.ProcsError):
                sf_procs.query(pid=os.getpid(), directory=self.directory)

    def test_the_installer_reads_a_dead_one_as_absent_and_refuses_a_live_one(self) -> None:
        path = self.write(4242, START)
        for check in (install.require_idle, install.queue_updates):
            with self.subTest(check=check.__name__):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    check(self.directory, {})
                self.assertIn(str(path), err.getvalue())
                with self.assertRaises(install.InstallError):
                    check(self.directory, {4242: START})
        self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
