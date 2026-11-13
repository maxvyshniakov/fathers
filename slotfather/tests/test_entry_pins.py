#!/usr/bin/env python3
"""Golden pins of every queue entry transition (fathers#33): a new request, a grant (normal, under memory warning,
after ageing), a yield and its resume, an older client's grant of a yielded lease, a priority change, a release
that ran, one that never did and one of a yielded waiter, a reap, and an ask by hand. Each scenario drives the live
functions in this process on a queue of its own, with a fixed clock, capacity and process table, and compares the
entry files byte for byte, the yield requests, queue.log, events.jsonl, history.jsonl, `status` and `status --json`
with tests/fixtures/entry-pins.json. The replay fixtures pin the order's outcomes, not the bytes these write.

SLOTFATHER_PIN_WRITE=1 rewrites the fixture; only do that for a change that is meant to alter what is written."""
from __future__ import annotations
import sys as _path_sys
from pathlib import Path as _Path
_path_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "src"))

import contextlib
import io
import json
import os
import re
import shutil
import tempfile
import time
import unittest
from typing import Any, Callable, Iterator
from unittest import mock

from slotfather import config as sf_config
from slotfather import events as sf_events
from slotfather import leases as sf_leases
from slotfather import platform as sf_platform
from slotfather import query as sf_query
from slotfather import state as sf_state

FIXTURE = _Path(__file__).resolve().parent / "fixtures" / "entry-pins.json"
T0 = 1762948800.0  # 2025-11-12T12:00:00Z
START = "Wed Nov 12 11:00:00 2025"
SELF = os.getpid()
BASIS = {"slots": 4, "saved": True, "mode": "fixed", "gib_per_slot": None, "memory_gib": None}


def stamp(moment: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(moment))


class Queue:
    """One scenario's queue: a fixed clock, the fake holders' process table, entries written as a client writes
    them, and the files it snapshots."""

    def __init__(self, root: _Path) -> None:
        self.directory = root / "state"
        (self.directory / "queue").mkdir(parents=True)
        self.memory = root / "memory"
        self.memory.write_text("8000 1\n")
        self.clock = T0
        self.table: dict[int, tuple[int, str]] = {SELF: (1, START)}
        self.sequence = 0
        self.ids = 0
        self.snapshots: list[dict[str, Any]] = []

    def at(self, offset: float) -> None:
        self.clock = T0 + offset

    def entry(self, pid: int, label: str, count: int, state: str = "waiting", since: float = -600.0,
              alive: bool = True, **extra: Any) -> _Path:
        self.sequence += 1
        if alive:
            self.table[pid] = (1, START)
        path = self.directory / "queue" / f"{self.sequence:010d}-{pid}.json"
        found: dict[str, Any] = {"pid": pid, "start": START, "count": count, "label": label, "cmd": f"make {label}",
                                 "cwd": "/work", "since": stamp(T0 + since), "state": state, "priority": "normal",
                                 "lease_id": f"lease-{label}"}
        if state == "held":
            found["granted"] = stamp(T0 + since + 60)
        found.update(extra)
        sf_state.write(path, found)
        (self.directory / "sequence").write_text(str(self.sequence))
        return path

    def uuid(self) -> Any:
        self.ids += 1
        return mock.Mock(hex=f"{self.ids:032x}")

    @contextlib.contextmanager
    def running(self) -> Iterator[None]:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("SLOTFATHER", "STRAYLIGHT_SLOT"))}
        env.update(SLOTFATHER_DIR=str(self.directory), SLOTFATHER_LEGACY_DIRS="", SLOTFATHER_OVERTAKE_WINDOW="0",
                   SLOTFATHER_CORES="12", SLOTFATHER_MEMORY_FILE=str(self.memory), SLOTFATHER_GIT_FACTS="0",
                   SLOTFATHER_POLL="0.01", TZ="UTC", GODFATHER_ACTOR="")
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, env, clear=True))
            time.tzset()
            stack.callback(time.tzset)
            stack.enter_context(mock.patch("time.time", lambda: self.clock))
            stack.enter_context(mock.patch.object(sf_config, "now", lambda: stamp(self.clock)))
            stack.enter_context(mock.patch.object(sf_config, "capacity_basis", lambda: dict(BASIS)))
            stack.enter_context(mock.patch.object(sf_platform, "processes", lambda: dict(self.table)))
            stack.enter_context(mock.patch.object(sf_query, "status_processes", lambda *_: dict(self.table)))
            stack.enter_context(mock.patch.object(sf_platform, "start_epoch", lambda pid: None))
            stack.enter_context(mock.patch.object(sf_platform, "process_uid", lambda pid: os.geteuid()))
            stack.enter_context(mock.patch.object(sf_platform, "process_group", lambda pid: None))
            stack.enter_context(mock.patch.object(sf_leases.uuid, "uuid4", self.uuid))
            stack.enter_context(mock.patch.object(sf_events, "moment", lambda: stamp(self.clock)))
            stack.enter_context(mock.patch.object(sf_events, "host", lambda: "host"))
            yield

    def snapshot(self, step: str) -> None:
        queue = {}
        for path in sorted((self.directory / "queue").iterdir()):
            queue[normal(path.name)] = normal(path.read_text())
        found: dict[str, Any] = {"step": step, "queue": queue}
        for name in ("queue.log", "history.jsonl", "events.jsonl"):
            path = self.directory / name
            found[name] = normal(path.read_text()).splitlines() if path.exists() else []
        found["status"] = [normal(line) for line in sf_query.status_lines(self.directory)]
        listing = sf_query.status_json(self.directory)
        found["status --json"] = plain(listing)
        self.snapshots.append(found)


ROOTS: list[str] = []  # the scenario's queue directory, as written and resolved


def normal(text: str) -> str:
    """This process's pid as SELF and the scenario's directory as QUEUE: what differs from one run to the next."""
    for root in ROOTS:
        text = text.replace(root, "QUEUE")
    return re.sub(rf"\b{SELF}\b", "SELF", text)


def plain(value: Any) -> Any:
    """normal() through JSON data: this process's pid as SELF in numbers and text."""
    if isinstance(value, dict):
        return {normal(str(k)): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, str):
        return normal(value)
    return "SELF" if value == SELF and type(value) is int else value


def scenarios() -> dict[str, Callable[[Queue], None]]:
    def request_and_release(queue: Queue) -> None:
        cwd = os.getcwd()
        os.chdir(queue.directory)
        try:
            lease = sf_leases.acquire(1, "plain", "make plain", out=io.StringIO())
            queue.snapshot("a plain request granted at once")
            queue.at(90)
            lease.status = 0
            lease.release()
            queue.snapshot("released after it ran")
            queue.at(100)
            lease = sf_leases.acquire("all", "every", "make every", out=io.StringIO(), wait=60,
                                      meta={"lane": "l1", "cache": "warm"}, yieldable=True, priority="batch",
                                      ticket="#12", naming={"given": "g", "derived": "d", "project": "p"})
            queue.snapshot("a request with every field")
            queue.at(130)
            lease.status = 3
            lease.release()
            queue.snapshot("released with a status")
        finally:
            os.chdir(cwd)

    def grants(queue: Queue) -> None:
        queue.entry(90001, "running", 1, "held", since=-7200)
        queue.entry(90002, "aged", 1, since=-7200, priority="batch")
        queue.entry(90003, "fresh", 1, since=-60)
        with contextlib.redirect_stderr(io.StringIO()):
            sf_leases.grant(queue.directory, sf_leases.process_table(queue.directory))
        queue.snapshot("an aged batch waiter and a fresh one granted")
        queue.memory.write_text("8000 2\n")
        queue.at(10)
        queue.entry(90004, "pressed", 1, since=-5)
        queue.entry(90005, "second", 1, since=-4)
        sf_leases.grant(queue.directory, sf_leases.process_table(queue.directory))
        queue.snapshot("under warning: one granted pressed, the next held back")
        queue.memory.write_text("8000 1\n")
        queue.at(20)
        sf_leases.grant(queue.directory, sf_leases.process_table(queue.directory))
        queue.snapshot("memory normal again")

    def yield_and_resume(queue: Queue) -> None:
        gate = queue.entry(90011, "gate", 4, "held", since=-7200, priority="batch", yieldable=True)
        quick = queue.entry(90012, "quick", 4, since=-600, priority="interactive")
        sf_leases.grant(queue.directory, sf_leases.process_table(queue.directory))
        queue.snapshot("the interactive waiter asks the gate to yield")
        queue.at(5)
        out = io.StringIO()
        with mock.patch.object(sf_leases, "held_again", lambda path: queue.snapshot("the gate yielded")):
            self_yielded = sf_leases.yield_lease(gate, out=out)
        assert self_yielded, out.getvalue()
        queue.at(65)
        sf_leases.Lease(4, quick).release()
        queue.snapshot("quick released: the gate resumes")
        sf_leases.held_again(gate)
        queue.at(125)
        lease = sf_leases.Lease(4, gate)
        lease.status = 0
        lease.release()
        queue.snapshot("the gate released after its yield")

    def older_client_grant(queue: Queue) -> None:
        gate = queue.entry(90021, "gate", 2, "held", since=-7200, priority="batch", yieldable=True, yields=1,
                           yielded_at=T0 - 300, first_granted=stamp(T0 - 7000), rank=T0 - 1000.5,
                           granted=stamp(T0 - 30))
        sf_leases.held_again(gate)
        queue.snapshot("an older client granted the yielded gate; its holder settles the resume")

    def yielded_waiter_leaves(queue: Queue) -> None:
        gate = queue.entry(90031, "gate", 4, "waiting", since=-7200, priority="batch", yieldable=True, yields=1,
                           yielded_at=T0 - 300, first_granted=stamp(T0 - 7000), rank=T0 - 1000.5,
                           granted=stamp(T0 - 7000))
        queue.entry(90032, "holder", 4, "held", since=-900)
        lone = queue.entry(90033, "lone", 1, since=-100)
        queue.snapshot("a yielded lease and a waiter behind a full machine")
        sf_leases.Lease(4, gate).release()
        queue.snapshot("the yielded lease gave up: released, it ran")
        sf_leases.Lease(1, lone).release()
        queue.snapshot("the waiter gave up: left")

    def reprioritize(queue: Queue) -> None:
        queue.entry(90041, "holder", 4, "held", since=-900)
        queue.entry(90042, "late", 1, since=-100, aged="normal")
        queue.entry(90043, "batchy", 1, since=-50, priority="batch")
        sf_leases.reprioritize("90042", "interactive", queue.directory)
        queue.snapshot("late moved to interactive")
        queue.at(30)
        sf_leases.reprioritize("late", "batch", queue.directory)
        sf_leases.reprioritize("batchy", "batch", queue.directory)
        queue.snapshot("late moved to batch, batchy unchanged")

    def reap(queue: Queue) -> None:
        queue.entry(90051, "crashed", 2, "held", since=-900, alive=False)
        queue.entry(90052, "gone", 1, since=-100, alive=False)
        queue.entry(90053, "waits", 1, since=-50)
        sf_leases.grant(queue.directory, sf_leases.process_table(queue.directory))
        queue.snapshot("a crashed holder and a gone waiter reaped; the waiter granted")

    def ask_by_hand(queue: Queue) -> None:
        queue.entry(90061, "gate", 4, "held", since=-7200, priority="normal", yieldable=True)
        queue.entry(90062, "urgent", 2, since=-30, priority="interactive")
        with mock.patch.dict(os.environ, {"SLOTFATHER_YIELD_AFTER": "3600"}):
            sf_leases.ask_yield(90061, START, queue.directory)
        queue.snapshot("asked by hand for the interactive waiter")

    return {name: function for name, function in locals().items() if callable(function)}


def run(name: str, scenario: Callable[[Queue], None]) -> list[dict[str, Any]]:
    root = _Path(tempfile.mkdtemp(prefix="slotfather-pins-"))
    try:
        queue = Queue(root)
        ROOTS[:] = sorted({str(queue.directory), os.path.realpath(queue.directory)}, key=len, reverse=True)
        with queue.running():
            scenario(queue)
        return queue.snapshots
    finally:
        shutil.rmtree(root, ignore_errors=True)


class EntryPinTests(unittest.TestCase):
    maxDiff = None

    def test_every_transition_writes_what_it_wrote_before(self) -> None:
        found = {name: run(name, scenario) for name, scenario in scenarios().items()}
        if os.environ.get("SLOTFATHER_PIN_WRITE") == "1":
            FIXTURE.write_text(json.dumps(found, indent=1, sort_keys=True) + "\n")
        pinned = json.loads(FIXTURE.read_text())
        self.assertEqual(sorted(pinned), sorted(found))
        for name in pinned:
            with self.subTest(scenario=name):
                self.assertEqual(pinned[name], json.loads(json.dumps(found[name])))


if __name__ == "__main__":
    unittest.main()
