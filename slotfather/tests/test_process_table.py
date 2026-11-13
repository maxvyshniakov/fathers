#!/usr/bin/env python3
"""A process table that cannot be trusted reaps nothing (fathers#121): a ps that fails, times out, answers nothing
or leaves out its caller raises, every look that would reap skips its pass and says so in queue.log and the event
log, and the next good table reaps as before."""
from __future__ import annotations
import sys as _path_sys
from pathlib import Path as _Path
_path_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "src"))

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from typing import Any, Callable
from unittest import mock

from slotfather import board as sf_board
from slotfather import config as sf_config
from slotfather import events as sf_events
from slotfather import leases as sf_leases
from slotfather import platform as sf_platform
from slotfather import query as sf_query
from slotfather import sweep as sf_sweep
from test_slotfather import SlotCase

REAL_RUN: Callable[..., Any] = subprocess.run


def answer(code: int, stdout: str) -> Callable[..., Any]:
    def run(argv: Any, *args: Any, **kwargs: Any) -> Any:
        if argv and argv[0] == "ps":
            return subprocess.CompletedProcess(argv, code, stdout=stdout, stderr="ps: broken")
        return REAL_RUN(argv, *args, **kwargs)
    return run


def timeout(argv: Any, *args: Any, **kwargs: Any) -> Any:
    if argv and argv[0] == "ps":
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout") or 0)
    return REAL_RUN(argv, *args, **kwargs)


def dead_pid() -> int:
    child = subprocess.Popen(["true"])
    child.wait()
    return child.pid


FAILURES: dict[str, Callable[..., Any]] = {
    "a non-zero ps": answer(1, ""),
    "a ps that times out": timeout,
    "an empty table": answer(0, ""),
    "a table without the caller": answer(0, "    1     0 Ss   Fri Nov 13 10:00:00 2026\n"),
}


class ProcessTableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = _Path(tempfile.mkdtemp(prefix="slotfather-ps-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        patcher = mock.patch.dict(os.environ, {"SLOTFATHER_DIR": str(self.dir), "SLOTFATHER_LEGACY_DIRS": "",
                                               "SLOTFATHER_CORES": "4"})
        patcher.start()
        self.addCleanup(patcher.stop)
        (self.dir / "queue").mkdir(parents=True)
        me = os.getpid()
        start = sf_platform.processes()[me][1]
        self.live = self.dir / "queue" / f"{1:010d}-{me}.json"
        self.live.write_text(json.dumps({"pid": me, "start": start, "count": 1, "label": "live", "cmd": "x",
                                         "since": "2026-11-13T00:00:00Z", "state": "held"}))
        ghost = dead_pid()
        self.dead = self.dir / "queue" / f"{2:010d}-{ghost}.json"
        self.dead.write_text(json.dumps({"pid": ghost, "start": "Fri Nov 13 00:00:00 2026", "count": 1,
                                         "label": "ghost", "cmd": "x", "since": "2026-11-13T00:00:00Z",
                                         "state": "held"}))

    def kinds(self) -> list[str]:
        path = self.dir / "events.jsonl"
        return [json.loads(line)["kind"] for line in path.read_text().splitlines()] if path.exists() else []

    def test_processes_raises_for_each_failure(self) -> None:
        for name, run in FAILURES.items():
            with self.subTest(name), mock.patch.object(sf_platform.subprocess, "run", side_effect=run):
                with self.assertRaises(sf_config.ProcessTableError):
                    sf_platform.processes()

    def test_processes_has_a_timeout(self) -> None:
        seen: list[Any] = []

        def run(argv: Any, *args: Any, **kwargs: Any) -> Any:
            seen.append(kwargs.get("timeout"))
            return REAL_RUN(argv, *args, **kwargs)
        with mock.patch.object(sf_platform.subprocess, "run", side_effect=run):
            sf_platform.processes()
        self.assertEqual([sf_config.PS_TIMEOUT], seen)

    def test_no_look_reaps_on_a_bad_table_and_the_next_good_table_reaps(self) -> None:
        looks: dict[str, Callable[[], Any]] = {
            "snapshot": lambda: sf_query.snapshot(self.dir),
            "look": lambda: sf_query.look(self.dir),
            "status": lambda: sf_query.status_lines(self.dir),
            "status --json": lambda: sf_query.status_json(self.dir),
            "sweep": lambda: sf_sweep.leases(self.dir, mock.Mock()),
            "grant": lambda: sf_leases.grant(self.dir, sf_leases.process_table(self.dir)),
        }
        skipped = 0
        for name, run in FAILURES.items():
            for look_name, look in looks.items():
                with self.subTest(failure=name, look=look_name), \
                        mock.patch.object(sf_platform.subprocess, "run", side_effect=run), \
                        mock.patch("slotfather.macos_process.basic_snapshot", side_effect=OSError("no libproc")):
                    with self.assertRaises(sf_config.SlotError):
                        look()
                    skipped += 1
                    self.assertTrue(self.live.exists() and self.dead.exists(), "a bad table reaped a lease")
        log = (self.dir / "queue.log").read_text().splitlines()
        self.assertEqual(skipped, sum(" skipped reaping: " in line for line in log), log)
        self.assertEqual(skipped, self.kinds().count("reap.skipped"))
        self.assertNotIn("lease.reaped", self.kinds())
        sf_query.snapshot(self.dir)
        self.assertTrue(self.live.exists())
        self.assertFalse(self.dead.exists(), "the next good table reaps the dead holder")
        self.assertIn("lease.reaped", self.kinds())

    def test_the_skip_event_validates(self) -> None:
        _path_sys.path.insert(0, str(_Path(__file__).resolve().parents[2] / "common"))
        import eventcheck
        with mock.patch.object(sf_platform.subprocess, "run", side_effect=answer(0, "")):
            with self.assertRaises(sf_config.SlotError):
                sf_query.snapshot(self.dir)
        self.assertEqual([], eventcheck.validate_file(str(self.dir / "events.jsonl"), strict=True))
        self.assertIn("reap.skipped", sf_events.KINDS)


class BoardSampleTests(unittest.TestCase):
    def test_process_usage_has_a_timeout_and_refuses_a_table_without_its_caller(self) -> None:
        for name, run in {"a ps that times out": timeout,
                          "a table without the caller": answer(0, "    1     0  0.0  1000 launchd\n"),
                          "an empty table": answer(0, "")}.items():
            with self.subTest(name), mock.patch.object(sf_board.subprocess, "run", side_effect=run):
                with self.assertRaises(OSError):
                    sf_board.process_usage()
        own, _ = sf_board.process_usage()
        self.assertIn(os.getpid(), own)


FAKE_PS = """#!/bin/sh
if [ -e "$PS_BROKEN" ]; then echo "ps: broken" >&2; exit 1; fi
exec /bin/ps "$@"
"""


class WaiterTests(SlotCase):
    def test_a_waiter_and_its_holder_outlive_a_broken_ps(self) -> None:
        """While ps fails the holder keeps its lease and the waiter keeps waiting; once ps works again the queue
        moves on as before."""
        bins = self.tmp / "bin"
        bins.mkdir()
        (bins / "ps").write_text(FAKE_PS)
        (bins / "ps").chmod(0o755)
        broken = self.tmp / "ps-broken"
        self.env.update(PATH=f"{bins}{os.pathsep}{self.env.get('PATH', '')}", PS_BROKEN=str(broken))
        holder = self.request("holder", 4)
        self.started("holder")
        waiter = self.request("waiter", 4)
        self.wait_for(lambda: "waiting: 4 slot(s) waiter" in self.status(), "the waiter to queue")
        broken.touch()
        self.wait_for(lambda: "skipped reaping" in (self.state / "queue.log").read_text(), "a skipped look")
        time.sleep(0.5)  # several more looks of the waiter
        self.assertIsNone(holder.poll())
        self.assertIsNone(waiter.poll(), waiter.stdout.read() if waiter.poll() is not None and waiter.stdout else "")
        self.assertEqual(2, len(list((self.state / "queue").glob("*.json"))), "a broken ps reaped a lease")
        broken.unlink()
        self.release("holder")
        self.started("waiter")
        self.release("waiter")
        for process in (holder, waiter):
            self.finish(process)
        self.assertNotIn("reaped", (self.state / "queue.log").read_text())


if __name__ == "__main__":
    unittest.main()
