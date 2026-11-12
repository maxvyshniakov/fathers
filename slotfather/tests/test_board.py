#!/usr/bin/env python3
"""The dashboard contracts (slotfather#26): `capabilities --json` and `status --json --full`, whose `dashboard` block
is the dashboard's model (board.dashboard_model()) from the same look at the queue as the status beside it."""
from __future__ import annotations
import sys as _path_sys
from pathlib import Path as _Path
_path_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "src"))

import datetime
import importlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from typing import Any, cast
from unittest import mock

from test_slotfather import ROOT, SlotCase

# Facts that move between two looks a moment apart: process samples, the clock and what is computed from it.
MOVING_TOP = ("updated", "history", "advice", "averages", "memory", "start", "reserved")
MOVING_ITEM = ("cpu", "rss", "procs", "tree", "est_start")


def encoded(value: Any) -> Any:
    """The block's encoding, written out here on its own: datetimes as RFC 3339 UTC with milliseconds, tuples as
    arrays."""
    if isinstance(value, datetime.datetime):
        utc = value.astimezone(datetime.timezone.utc)
        return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"
    if isinstance(value, dict):
        return {key: encoded(item) for key, item in cast(dict[str, Any], value).items()}
    if isinstance(value, (list, tuple)):
        return [encoded(item) for item in cast(list[Any], value)]
    return value


def steady(block: dict[str, Any]) -> dict[str, Any]:
    found = {key: value for key, value in block.items() if key not in MOVING_TOP}
    for name in ("holders", "waiters"):
        found[name] = [{key: value for key, value in row.items() if key not in MOVING_ITEM} for row in block[name]]
    return found


class BoardTests(SlotCase):
    def setUp(self) -> None:
        super().setUp()
        self.env["SLOTFATHER_LOADAVG"] = "1"

    def full(self) -> dict[str, Any]:
        result = self.tool("status", "--json", "--full")
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads(result.stdout)

    def model(self) -> dict[str, Any]:
        with mock.patch.dict(os.environ, self.env, clear=True):
            return importlib.import_module("slotfather.board").dashboard_model()

    def fixture(self) -> tuple[Any, str]:
        """A running lease asked to yield (its marker written as a waiter writes it), a yieldable one, and a waiting
        entry with a marker of its own (#20: one pid's markers are read by entry, not by pid)."""
        holder = self.request("holder", 1, options=("--yieldable", "--priority", "batch", "--meta", "lane=l-1"))
        self.started("holder")
        other = self.request("other", 2)
        self.started("other")
        pid, start = self.holder()
        stem = f"{99:010d}-{pid}"
        since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 30))
        (self.state / "queue" / f"{stem}.json").write_text(json.dumps(
            {"pid": pid, "start": start, "count": 4, "label": "waiter", "cmd": "fake", "since": since,
             "state": "waiting", "priority": "interactive"}))
        held = next(path for path in (self.state / "queue").glob("*.json")
                    if json.loads(path.read_text())["label"] == "holder")
        asked = {"by": stem, "label": "waiter", "priority": "interactive", "count": 4,
                 "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        held.with_suffix(".yield").write_text(json.dumps(asked))
        (self.state / "queue" / f"{stem}.yield").write_text(json.dumps(dict(asked, label="stray")))
        self.addCleanup(lambda: [self.release(label) for label in ("holder", "other")])
        return (holder, other), stem

    def test_the_dashboard_block_is_the_in_tree_model(self) -> None:
        processes, _ = self.fixture()
        block = self.full()["dashboard"]
        model = self.model()
        self.assertEqual(1, block.pop("v"))
        self.assertEqual(0.0, block.pop("overtake_window"), "SLOTFATHER_OVERTAKE_WINDOW=0 in the fixture")
        memory = block["memory"]
        self.assertEqual((False, False, False), (memory.pop("low"), memory.pop("swap_low"), memory.pop("psi_low")))
        for row in model["holders"] + model["waiters"]:
            self.assertIn(row.pop("style"), ("text", "legacy"), "presentation stays out of the block")
        self.assertEqual(steady(encoded(model)), steady(block))
        self.assertEqual(["holder", "other"], [row["label"] for row in block["holders"]])
        self.assertEqual(["waiter"], [row["label"] for row in block["waiters"]])
        holder = block["holders"][0]
        self.assertEqual(("waiter", True, "batch", {"lane": "l-1"}),
                         (holder["asked"], holder["yieldable"], holder["priority"], holder["context"]))
        self.assertRegex(holder["asked_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        self.assertIsInstance(holder["key"], list)
        self.assertTrue(all(isinstance(row, list) and len(cast(list[Any], row)) == 4 for row in holder["tree"]))
        self.assertTrue(all(isinstance(step, list) and len(cast(list[Any], step)) == 3 for step in block["history"]))
        for label in ("holder", "other"):
            self.release(label)
        for process in processes:
            self.finish(process)

    def test_status_and_its_block_read_one_look(self) -> None:
        """#20: each entry's yield request and idle mark are the same in the status and in the block."""
        processes, stem = self.fixture()
        found = self.full()
        rows = {(row["pid"], row["key"][1]): row for row in found["dashboard"]["holders"] + found["dashboard"]["waiters"]}
        self.assertEqual(3, len(found["entries"]))
        for entry in found["entries"]:
            row = rows[(entry["pid"], entry["since"])]
            asked = cast(dict[str, Any], entry["yield_asked"] or {})
            self.assertEqual(asked.get("label") or "", row["asked"], entry["label"])
            self.assertEqual(entry["idle_since"] is None, row["idle_since"] is None)
            self.assertEqual(entry["state"] == "held", row["kind"] == "held")
        waiter = next(entry for entry in found["entries"] if entry["id"] == stem)
        self.assertEqual("stray", waiter["yield_asked"]["label"])
        for label in ("holder", "other"):
            self.release(label)
        for process in processes:
            self.finish(process)

    def test_full_status_is_the_status_older_readers_read_and_more(self) -> None:
        holder = self.request("holder", 1)
        self.started("holder")
        full = self.full()
        plain = json.loads(self.tool("status", "--json").stdout)
        self.assertEqual(sorted([*plain, "dashboard", "host", "time"]), sorted(full))
        self.assertEqual(2, full["v"])
        self.assertRegex(full["host"], r"^[a-z0-9-]+$")
        self.assertRegex(full["time"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        moving = ("idle_since", "waited", "est_start", "tree")
        self.assertEqual([{k: v for k, v in e.items() if k not in moving} for e in plain["entries"]],
                         [{k: v for k, v in e.items() if k not in moving} for e in full["entries"]])
        self.assertEqual(sorted(plain["entries"][0]), sorted(full["entries"][0]), "no entry field added")
        self.release("holder")
        self.finish(holder)

    def test_capabilities_name_the_snapshot_and_the_changelog(self) -> None:
        result = self.tool("capabilities", "--json")
        self.assertEqual(0, result.returncode, result.stderr)
        found = json.loads(result.stdout)
        self.assertEqual((1, "slotfather"), (found["v"], found["tool"]))
        version = importlib.import_module("slotfather.version").VERSION
        self.assertEqual(version, found["version"])
        dashboard = found["dashboard"]
        self.assertEqual((1, "slot"), (dashboard["v"], dashboard["tag"]))
        self.assertEqual({"argv": ["status", "--json", "--full"], "versions": [2], "poll": 1}, dashboard["snapshot"])
        self.assertEqual(str(ROOT / "CHANGELOG.md"), dashboard["changelog"])
        self.assertIn("reprioritize", dashboard["actions"])
        self.assertEqual(2, self.tool("capabilities").returncode, "only --json")


class MemoryFactsTests(unittest.TestCase):
    """The block's policy facts on Linux, where PSI and MemAvailable decide, not a pressure level."""

    def block(self, memory: dict[str, Any], **env: str) -> dict[str, Any]:
        board = importlib.import_module("slotfather.board")
        with mock.patch.dict(os.environ, env, clear=True):
            return cast(dict[str, Any], board.dashboard_json({"holders": [], "waiters": [], "memory": memory})["memory"])

    def test_psi_at_its_limit_is_low_on_linux(self) -> None:
        linux = {"swap_total": 2048.0, "swap_free": 1024.0, "level": None, "available": 8000.0, "total": 16000.0,
                 "psi_some": 4.0, "psi_full": 0.0, "cgroup_available": None, "cgroup": None}
        self.assertEqual((False, False, False), tuple(self.block(linux)[k] for k in ("low", "swap_low", "psi_low")))
        some = dict(linux, psi_some=10.0)
        self.assertEqual((True, True), (self.block(some)["low"], self.block(some)["psi_low"]),
                         "SLOTFATHER_MAX_PSI_SOME defaults to 10")
        self.assertTrue(self.block(dict(linux, psi_full=5.0))["psi_low"], "and _FULL to 5")
        self.assertFalse(self.block(some, SLOTFATHER_MAX_PSI_SOME="20")["psi_low"])
        self.assertTrue(self.block(dict(linux, swap_free=100.0))["swap_low"])

    def test_macos_has_no_psi(self) -> None:
        mac = {"swap_total": 10240.0, "swap_free": 5000.0, "level": 2.0, "total": 32768.0, "used": 20000.0}
        self.assertEqual((True, False), (self.block(mac)["low"], self.block(mac)["psi_low"]))


def board() -> Any:
    return importlib.reload(importlib.import_module("slotfather.board"))


class AdmissionTests(SlotCase):
    """The block's `admission` (ported from the in-tree dashboard's tests, #28): the slots a generic request would get
    now, after the queue's memory gates, which `available` and `advise` leave out."""

    def model(self) -> dict[str, Any]:
        with mock.patch.dict(os.environ, self.env, clear=True):
            return cast(dict[str, Any], board().dashboard_model())

    def test_admission_is_available_slots_not_worker_advice_and_honours_memory_gates(self) -> None:
        self.env["SLOTFATHER_LOADAVG"] = "0"
        self.assertEqual(4, self.model()["admission"])
        self.memory.write_text("128 1\n")
        self.assertEqual(4, self.model()["admission"], "an idle host may still start its first lease")
        holder = self.request("memory-gate-owner", 1)
        self.started("memory-gate-owner")
        state = self.model()
        self.assertEqual(3, state["free"])
        self.assertEqual(0, state["admission"], "low swap blocks additional work despite free slots")
        self.assertEqual("3", self.tool("available").stdout.strip(),
                         "available reports queue space before memory policy; admission includes that policy")
        self.assertEqual(3, state["advice"]["jobs"], "three free worker slots are advice, not admission")
        self.memory.write_text("8000 1\n")
        self.assertEqual(3, self.model()["admission"])
        self.assertEqual(3, json.loads(self.tool("status", "--json", "--full").stdout)["dashboard"]["admission"])
        self.release("memory-gate-owner")
        self.finish(holder)

    def test_the_memory_exemption_for_a_first_lease_is_consumed_by_a_waiter_ahead(self) -> None:
        from slotfather import config
        tool = board()
        settings = dict(config.ORDER_DEFAULTS)
        waiter = dict(count=1, label="waiting", cmd="work", since=config.now(), state="waiting")
        self.assertEqual(0, tool.dashboard_admission([waiter], [], 6, 0, "memory pressure", None, settings))
        self.assertEqual(6, tool.dashboard_admission([], [], 6, 0, "memory pressure", None, settings))
        self.assertEqual(5, tool.dashboard_admission([waiter], [], 6, 0, None, None, settings))
        waiter["priority"] = "batch"
        self.assertEqual(6, tool.dashboard_admission([waiter], [], 6, 0, "memory pressure", None, settings),
                         "a normal probe ranked first may use the idle-host exemption")


class EventTests(unittest.TestCase):
    """EVENTS' rows from queue.log and each request's history line (ported from the in-tree dashboard's tests): a
    waiter that gave up and a lease that ended stay apart also once queue.log is longer than the part read."""

    LOG = ("2026-11-11T02:00:00Z granted 1 to build (pid 101)\n"
           "2026-11-11T02:00:05Z granted 2 to verify (pid 102)\n"
           "2026-11-11T02:04:00Z released 1 (0000000001-101.json)\n"
           "2026-11-11T02:05:00Z released 1 (0000000003-103.json)\n"
           "2026-11-11T02:06:00Z reaped held lease of 2 for verify (pid 102 exited)\n"
           "2026-11-11T02:06:30Z granted 1 to live (pid 104)\n")
    LIVE = [{"pid": 104, "since": "2026-11-11T02:05:30Z", "granted": "2026-11-11T02:06:30Z", "state": "held"}]

    def history(self) -> list[dict[str, Any]]:
        def line(label: str, pid: int, queued: str, granted: str | None, ended: str, outcome: str,
                 waited: int, ran: int | None, status: int | None) -> dict[str, Any]:
            return {"v": 2, "label": label, "count": 1, "pid": pid, "queued": queued, "granted": granted,
                    "ended": ended, "outcome": outcome, "waited": waited, "ran": ran, "status": status}
        return [line("build", 101, "2026-11-11T01:57:00Z", "2026-11-11T02:00:00Z", "2026-11-11T02:04:00Z",
                     "released", 180, 240, 1),
                line("gate", 103, "2026-11-11T01:50:00Z", None, "2026-11-11T02:05:00Z", "left", 900, None, None),
                line("verify", 102, "2026-11-11T02:00:05Z", "2026-11-11T02:00:05Z", "2026-11-11T02:06:00Z",
                     "reaped", 0, 355, None)]

    def directory(self, log: str, history: bool = True) -> _Path:
        directory = _Path(tempfile.mkdtemp(prefix="slotfather-events-"))
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        (directory / "queue.log").write_text(log)
        if history:
            (directory / "history.jsonl").write_text("".join(json.dumps(item) + "\n" for item in self.history()))
        return directory

    def test_each_event_says_what_its_history_line_knows(self) -> None:
        tool = board()
        for padding in (0, 20000):  # 20000 lines: longer than the part of queue.log that is read
            with self.subTest(padding=padding):
                filler = "".join(f"2026-11-11T01:00:00Z sweep note {n:06d}\n" for n in range(padding))
                directory = self.directory(filler + self.LOG)
                events = tool.queue_events(directory)
                tool.describe_events(events, directory, self.LIVE)
                self.assertEqual([("granted", "build", "waited 3m00s", 1), ("granted", "verify", "no wait", 2),
                                  ("released", "build", "exit 1, ran 4m00s", -1),
                                  ("left", "gate", "gave up after 15m00s", 0),
                                  ("reaped", "verify", "owner exited, ran 5m55s", -2),
                                  ("granted", "live", "waited 1m00s", 1)],
                                 [(e["verb"], e["label"], e["detail"], e["delta"]) for e in events])

    def test_a_release_never_granted_adds_no_slots_to_the_history(self) -> None:
        """A request that left without a grant or a history line, once queue.log was longer than the part read, was
        taken for a lease that ended, and the history walked back from the slots held now with 2 more."""
        tool = board()
        filler = "".join(f"2026-11-11T01:00:00Z sweep note {n:06d}\n" for n in range(20000))
        directory = self.directory(filler + self.LOG + "2026-11-11T02:07:00Z released 2 (0000000009-109.json)\n")
        events = tool.queue_events(directory)
        tool.describe_events(events, directory, self.LIVE)
        stray = next(event for event in events if event["pid"] == 109)
        self.assertEqual(("left", 0, "never granted"), (stray["verb"], stray["delta"], stray["detail"]))
        build = next(event for event in events if event["pid"] == 101 and event["verb"] != "granted")
        self.assertEqual(("released", -1), (build["verb"], build["delta"]), "a history line still says it held")
        now = datetime.datetime(2026, 11, 11, 2, 8, tzinfo=datetime.timezone.utc)
        steps = tool.held_history(events, 1, now, window=600)
        self.assertLessEqual(max(level for _, _, level in steps), 3, "no more than build, verify and live held")
        self.assertEqual(1, steps[-1][2], "live holds 1 now")

    def test_held_slots_are_walked_back_from_now_and_a_waiter_giving_up_holds_nothing(self) -> None:
        tool = board()
        directory = self.directory(
            "2026-01-01T10:00:00Z granted 2 to build (pid 10)\n"
            "2026-01-01T10:10:00Z granted 1 to tests (pid 11)\n"
            "2026-01-01T10:20:00Z released 1 (0000000003-12.json)\n"
            "2026-01-01T10:30:00Z released 2 (0000000001-10.json)\n"
            "2026-01-01T10:40:00Z reaped held lease of 1 for tests (pid 11 exited)\n", history=False)
        events = tool.queue_events(directory)
        self.assertEqual(["granted", "granted", "left", "released", "reaped"], [e["verb"] for e in events])
        self.assertEqual("build", events[3]["label"], "a release is named by the grant to its pid")
        now = datetime.datetime(2026, 1, 1, 10, 50, tzinfo=datetime.timezone.utc)
        steps = tool.held_history(events, 0, now, window=3600)
        self.assertEqual([0, 2, 3, 1, 0], [level for _, _, level in steps])


class MachineFactTests(unittest.TestCase):
    def test_macos_memory_counts_active_wired_and_compressor_physical_pages(self) -> None:
        sample = ("Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages active: 100.\n"
                  "Pages inactive: 1000.\nPages wired down: 50.\nPages occupied by compressor: 25.\n")
        self.assertEqual(2.734375, board().macos_used_memory(sample))
        self.assertIsNone(board().macos_used_memory("no vm statistics"))

    def test_a_failed_process_sample_is_not_returned_as_zero_cpu(self) -> None:
        tool = board()
        failure = subprocess.CompletedProcess(["ps"], 1, stdout="", stderr="unavailable")
        with mock.patch.object(tool.subprocess, "run", return_value=failure):
            with self.assertRaises(OSError):
                tool.process_usage()


if __name__ == "__main__":
    unittest.main()
