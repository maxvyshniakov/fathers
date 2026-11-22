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


def counting(function: Any, calls: list[object]) -> Any:
    """`function`, recording each call's arguments in `calls`."""
    def counted(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        return function(*args, **kwargs)
    return counted


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
        # The text status reads the same look (fathers#36): the yield request of the holder, none of the waiter's
        text = self.status()
        self.assertRegex(text, r"held: 1 slot\(s\) holder .* yieldable asked-to-yield-for=waiter")
        self.assertRegex(text, r"waiting: 4 slot\(s\) waiter (?!.*asked-to-yield)")
        self.assertIn(f"{found['held']} of {found['slots']} slots held", text)
        for label in ("holder", "other"):
            self.release(label)
        for process in processes:
            self.finish(process)

    def test_every_reader_takes_the_queue_lock_once_and_reads_ps_at_most_once(self) -> None:
        """fathers#36: each reader of the queue reads it from one look under the lock."""
        processes, stem = self.fixture()
        with mock.patch.dict(os.environ, self.env, clear=True):
            query = importlib.import_module("slotfather.query")
            state = importlib.import_module("slotfather.state")
            process = importlib.import_module("slotfather.process")
            cli = importlib.import_module("slotfather.cli")
            sweep = importlib.import_module("slotfather.sweep")
            path = self.state / "queue" / f"{stem}.json"
            readers = {
                "status": lambda: query.status_lines(),
                "status --json": lambda: query.status_json(),
                "status --json --pid": lambda: query.status_json(pid=os.getpid()),
                "long wait": lambda: query.long_wait_report(self.state, path),
                "available": lambda: query.available(),
                "advise": lambda: query.advise(),
                "slots": lambda: cli.slots_command([]),
                "sweep protection": lambda: sweep.leases(self.state),
            }
            real_lock, real_table = state.locked, process.processes
            for name, reader in readers.items():
                locks: list[object] = []
                tables: list[object] = []
                with self.subTest(reader=name), \
                        mock.patch.object(state, "locked", counting(real_lock, locks)), \
                        mock.patch.object(process, "processes", counting(real_table, tables)), \
                        mock.patch("sys.stdout"), mock.patch("sys.stderr"):
                    reader()
                    self.assertEqual(1, len(locks), f"{name} took the queue lock {len(locks)} times")
                    self.assertLessEqual(len(tables), 1, f"{name} read the process table {len(tables)} times")
        for label in ("holder", "other"):
            self.release(label)
        for process_ in processes:
            self.finish(process_)

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
        self.assertEqual(0, self.tool("capabilities").returncode, "text summary without --json (#124)")


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
        from slotfather import config, schedule
        settings = dict(config.ORDER_DEFAULTS)
        moment = time.time()
        waiter = dict(count=1, label="waiting", cmd="work", since=config.now(), state="waiting")
        short = schedule.Host(memory=("short", "memory pressure"))
        self.assertEqual(0, schedule.Admission([waiter], [], 6, moment, settings=settings, host=short).dashboard_room().admission)
        self.assertEqual(6, schedule.Admission([], [], 6, moment, settings=settings, host=short).dashboard_room().admission)
        self.assertEqual(5, schedule.Admission([waiter], [], 6, moment, settings=settings).dashboard_room().admission)
        batch = dict(waiter, priority="batch")
        self.assertEqual(6, schedule.Admission([batch], [], 6, moment, settings=settings, host=short).dashboard_room().admission,
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

    def test_events_jsonl_tracks_yields_resumes_and_release_while_waiting(self) -> None:
        """fathers#18: events.jsonl drives EVENTS and held_history(), including yields, resumes and releases while
        waiting to resume (delta 0)."""
        tool = board()
        directory = _Path(tempfile.mkdtemp(prefix="slotfather-events-jsonl-"))
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)

        def entry(at: str, kind: str, req_id: str, label: str, pid: int, details: dict[str, Any]) -> str:
            return json.dumps({"v": 1, "time": at, "host": "h", "source": "slotfather", "kind": kind,
                               "subject": {"type": "request", "id": req_id, "label": label, "pid": pid},
                               "actor": {"type": "process", "pid": pid}, "details": details}) + "\n"

        # 10:00..11:00 window (now = 11:00):
        # 10:10 a granted 2 -> 2
        # 10:20 a yields 2 to b -> 0
        # 10:25 b granted 3 -> 3
        # 10:30 b yields 3 for lock -> 0
        # 10:35 a resumed 2 -> 2
        # 10:40 b released while waiting to resume -> 2 (delta 0)
        # 10:42 c left -> 2 (delta 0)
        # 10:45 a released -> 0
        # 10:50 d granted 1 -> 1
        # 10:55 d reaped while held -> 0
        lines = [
            entry("2026-01-01T10:10:00.000Z", "lease.granted", "0001-10", "alpha", 10, {"count": 2, "waited": 0}),
            entry("2026-01-01T10:20:00.000Z", "lease.yielded", "0001-10", "alpha", 10,
                  {"count": 2, "to": {"id": "0002-11", "label": "beta"}, "yields": 1}),
            entry("2026-01-01T10:25:00.000Z", "lease.granted", "0002-11", "beta", 11, {"count": 3, "waited": 5}),
            entry("2026-01-01T10:30:00.000Z", "lease.yielded", "0002-11", "beta", 11,
                  {"count": 3, "lock": "/tmp/build.lock", "yields": 1}),
            entry("2026-01-01T10:35:00.000Z", "lease.granted", "0001-10", "alpha", 10,
                  {"count": 2, "waited": 900, "resumed": True}),
            entry("2026-01-01T10:40:00.000Z", "lease.released", "0002-11", "beta", 11,
                  {"count": 3, "exit": 0, "ran": 900}),
            entry("2026-01-01T10:42:00.000Z", "request.left", "0003-12", "gamma", 12,
                  {"count": 1, "gave_up_after": 60}),
            entry("2026-01-01T10:45:00.000Z", "lease.released", "0001-10", "alpha", 10,
                  {"count": 2, "exit": 0, "ran": 2100}),
            entry("2026-01-01T10:50:00.000Z", "lease.granted", "0004-13", "delta", 13, {"count": 1, "waited": 0}),
            entry("2026-01-01T10:55:00.000Z", "lease.reaped", "0004-13", "delta", 13,
                  {"count": 1, "ran": 300, "reason": "owner exited"}),
        ]
        (directory / "events.jsonl").write_text("".join(lines))
        # A stale/contradictory queue.log beside events.jsonl is ignored once events.jsonl exists.
        (directory / "queue.log").write_text("2026-01-01T10:05:00Z granted 4 to ignored (pid 99)\n")

        events = tool.queue_events(directory)
        tool.describe_events(events, directory, [])
        self.assertEqual([
            ("granted", "alpha", 2, "no wait"),
            ("yielded", "alpha", -2, "to beta"),
            ("granted", "beta", 3, "waited 5s"),
            ("yielded", "beta", -3, "for lock /tmp/build.lock"),
            ("granted", "alpha", 2, "resumed, waited 15m00s"),
            ("released", "beta", 0, "exit 0, ran 15m00s"),
            ("left", "gamma", 0, "gave up after 1m00s"),
            ("released", "alpha", -2, "exit 0, ran 35m00s"),
            ("granted", "delta", 1, "no wait"),
            ("reaped", "delta", -1, "owner exited, ran 5m00s"),
        ], [(e["verb"], e["label"], e["delta"], e["detail"]) for e in events])

        def t(minute: int) -> datetime.datetime:
            return datetime.datetime(2026, 1, 1, 10, minute, tzinfo=datetime.timezone.utc)

        now = datetime.datetime(2026, 1, 1, 11, 0, tzinfo=datetime.timezone.utc)
        steps = tool.held_history(events, 0, now, window=3600)
        self.assertEqual([
            (t(0), t(10), 0),
            (t(10), t(20), 2),
            (t(20), t(25), 0),
            (t(25), t(30), 3),
            (t(30), t(35), 0),
            (t(35), t(45), 2),
            (t(45), t(50), 0),
            (t(50), t(55), 1),
            (t(55), now, 0),
        ], steps)

    def test_queue_log_fallback_tracks_yields_regrants_and_release_while_waiting(self) -> None:
        """fathers#18: when events.jsonl is absent, queue.log tracks yields, re-grants and releases while waiting to
        resume without corrupting held_history()."""
        tool = board()
        directory = self.directory(
            "2026-01-01T10:10:00Z granted 2 to alpha (pid 10)\n"
            "2026-01-01T10:20:00Z yielded 2 (0000000001-10.json) to beta\n"
            "2026-01-01T10:25:00Z granted 3 to beta (pid 11)\n"
            "2026-01-01T10:30:00Z yielded 3 (0000000002-11.json) while waiting for lock /tmp/build.lock\n"
            "2026-01-01T10:35:00Z granted 2 to alpha (pid 10)\n"
            "2026-01-01T10:40:00Z released 3 (0000000002-11.json)\n"
            "2026-01-01T10:45:00Z released 2 (0000000001-10.json)\n",
            history=False,
        )
        events = tool.queue_events(directory)
        tool.describe_events(events, directory, [])
        self.assertEqual([
            ("granted", "alpha", 2),
            ("yielded", "alpha", -2),
            ("granted", "beta", 3),
            ("yielded", "beta", -3),
            ("granted", "alpha", 2),
            ("released", "beta", 0),
            ("released", "alpha", -2),
        ], [(e["verb"], e["label"], e["delta"]) for e in events])

        def t(minute: int) -> datetime.datetime:
            return datetime.datetime(2026, 1, 1, 10, minute, tzinfo=datetime.timezone.utc)

        now = datetime.datetime(2026, 1, 1, 11, 0, tzinfo=datetime.timezone.utc)
        steps = tool.held_history(events, 0, now, window=3600)
        self.assertEqual([
            (t(0), t(10), 0),
            (t(10), t(20), 2),
            (t(20), t(25), 0),
            (t(25), t(30), 3),
            (t(30), t(35), 0),
            (t(35), t(45), 2),
            (t(45), now, 0),
        ], steps)


class RecentCpuTests(SlotCase):
    """fathers#20: on Linux, held leases' dashboard `cpu` reads the holder's recent rate from `.idle` so an idle
    lease is not simultaneously hot from `ps`'s lifetime average; older clients and macOS fall back to `ps`."""

    def test_linux_idle_lease_uses_recent_cpu_and_older_holder_or_macos_falls_back_to_ps(self) -> None:
        from slotfather import idle as sf_idle

        (self.state / "queue").mkdir(parents=True, exist_ok=True)
        quiet_pid, quiet_start = self.holder()
        older_pid, older_start = self.holder()
        now = time.time()
        since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 600))
        quiet_path = self.state / "queue" / f"{1:010d}-{quiet_pid}.json"
        older_path = self.state / "queue" / f"{2:010d}-{older_pid}.json"
        quiet_path.write_text(json.dumps(
            {"pid": quiet_pid, "start": quiet_start, "count": 1, "label": "quiet", "cmd": "sleep 60",
             "since": since, "granted": since, "state": "held"}))
        older_path.write_text(json.dumps(
            {"pid": older_pid, "start": older_start, "count": 1, "label": "older", "cmd": "sleep 60",
             "since": since, "granted": since, "state": "held"}))

        # Feed injected samples: a heavy burst followed by a quiet interval long enough to be idle.
        tracker = sf_idle.Tracker()
        key = (quiet_pid, quiet_start)
        tracker.observe(now - 420, {key: 0.0}, 0.1)
        tracker.observe(now - 410, {key: 40.0}, 0.1)  # 4.0 cores burst
        idle_at = tracker.observe(now - 400, {key: 40.1}, 0.1)  # 0.01 cores = 1.0%
        idle_at = tracker.observe(now, {key: 44.1}, 0.1)  # 0.01 cores over 400s = 1.0%
        sf_idle.record(quiet_path, since=idle_at, rate=tracker.rate, moment=now)

        # Fake ps reporting high lifetime %cpu (400% = 4 cores, well above a 1-slot hot share) for both holders.
        fake_usage: tuple[dict[int, tuple[float, int, str]], dict[int, list[int]]] = (
            {quiet_pid: (400.0, 1024, "python3"), older_pid: (400.0, 1024, "python3")}, {})
        tool = board()
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(tool, "process_usage", return_value=fake_usage), \
                mock.patch.object(tool._platform, "linux", return_value=True):
            linux_block = tool.dashboard_json(tool.dashboard_model())
        linux_rows = {row["label"]: row for row in linux_block["holders"]}
        self.assertIsNotNone(linux_rows["quiet"]["idle_since"], "quiet lease is idle")
        self.assertAlmostEqual(1.0, linux_rows["quiet"]["cpu"], places=1,
                               msg="recent rate from .idle is near 0, below the hot threshold")
        self.assertIsNone(linux_rows["older"]["idle_since"], "older client writes no .idle record")
        self.assertEqual(400.0, linux_rows["older"]["cpu"], "older holder without a record falls back to ps")

        # On macOS, dashboard_model falls back to ps %cpu even when a fresh .idle record exists.
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(tool, "process_usage", return_value=fake_usage), \
                mock.patch.object(tool._platform, "linux", return_value=False):
            mac_block = tool.dashboard_json(tool.dashboard_model())
        mac_rows = {row["label"]: row for row in mac_block["holders"]}
        self.assertIsNotNone(mac_rows["quiet"]["idle_since"])
        self.assertEqual(400.0, mac_rows["quiet"]["cpu"])


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
