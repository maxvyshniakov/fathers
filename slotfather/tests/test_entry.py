#!/usr/bin/env python3
"""The queue entry module (fathers#33): its phase, its accessors and its transitions, and a property test that random
grant, yield, resume, reprioritize and release sequences leave the same entries through the live functions as
through the transitions the replay applies (tools/replay.py), and leave an entry this client does not know as it
was (fathers#19)."""
from __future__ import annotations
import sys as _path_sys
from pathlib import Path as _Path
_path_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "src"))

import io
import json
import random
import shutil
import tempfile
import unittest
from typing import Any, cast
from unittest import mock

from slotfather import config as sf_config
from slotfather import entry as sf_entry
from slotfather import leases as sf_leases
from slotfather import schedule as sf_schedule
from slotfather import state as sf_state
from test_entry_pins import T0, Queue, stamp, unknown
from test_slotfather import ROOT
from test_usage import load

replay = cast(Any, load(str(ROOT / "tools" / "replay.py"), "slotfather_replay_for_entry"))
install = cast(Any, load(str(ROOT / "install.py"), "slotfather_install_for_entry"))


def waiter(**extra: Any) -> dict[str, Any]:
    return {"pid": 7, "start": "Mon Jan  1 00:00:00 2026", "count": 1, "since": "2026-01-01T00:00:00Z",
            "state": "waiting", **extra}


class PhaseTests(unittest.TestCase):
    def test_a_phase_from_the_state_and_what_lies_beside_it(self) -> None:
        held = waiter(state="held")
        self.assertEqual(("waiting", False), tuple(sf_entry.phase(waiter())))
        self.assertEqual(("yielded", False), tuple(sf_entry.phase(waiter(yielded_at=T0))))
        self.assertEqual(("held", False), tuple(sf_entry.phase(held)))
        self.assertEqual(("asked", True), tuple(sf_entry.phase(held, asked=True, idle_since=T0)))
        self.assertEqual(("held", False), tuple(sf_entry.phase(dict(held, yielded_at=T0))),
                         "an older client granted a yielded lease: it holds")

    def test_split_keeps_each_side_in_queue_order(self) -> None:
        entries = [waiter(label="a"), waiter(label="b", state="held"), waiter(label="c")]
        held, waiting = sf_entry.split(entries)
        self.assertEqual((["b"], ["a", "c"]), ([e["label"] for e in held], [e["label"] for e in waiting]))

    def test_the_outcome_of_a_request_that_leaves(self) -> None:
        self.assertEqual("released", sf_entry.outcome(waiter(state="held")))
        self.assertEqual("released", sf_entry.outcome(waiter(yields=1)), "a yielded lease ran")
        self.assertEqual("left", sf_entry.outcome(waiter()))
        self.assertEqual("reaped", sf_entry.outcome(waiter(state="held"), reaped=True))


class TransitionTests(unittest.TestCase):
    def test_a_yield_and_its_resume_keep_the_first_grant(self) -> None:
        entry = waiter(state="held", granted=stamp(T0), reserved=T0 + 5, pressed=True)
        sf_entry.to_yielded(entry, T0 + 100.0004, T0 - 1)
        self.assertEqual({"state": "waiting", "yields": 1, "yielded_at": T0 + 100.0, "first_granted": stamp(T0),
                          "rank": T0 - 1}, {k: entry[k] for k in ("state", "yields", "yielded_at", "first_granted",
                                                                  "rank")})
        self.assertNotIn("reserved", entry)
        self.assertNotIn("pressed", entry)
        sf_entry.to_held(entry, stamp(T0 + 160), T0 + 160)
        self.assertEqual({"state": "held", "granted": stamp(T0), "resumed": stamp(T0 + 160), "yielded": 60.0,
                          "yields": 1}, {k: entry[k] for k in ("state", "granted", "resumed", "yielded", "yields")})
        for gone in ("yielded_at", "first_granted", "rank"):
            self.assertNotIn(gone, entry)

    def test_a_grant_marks_memory_pressure_and_ageing(self) -> None:
        entry = waiter(priority="batch")
        sf_entry.to_held(entry, stamp(T0), T0, pressed=True, served="normal")
        self.assertEqual((True, "normal"), (entry["pressed"], entry["aged"]))
        same = waiter(priority="batch", pressed=True)
        sf_entry.to_held(same, stamp(T0), T0, pressed=False, served="batch")
        self.assertNotIn("pressed", same)
        self.assertNotIn("aged", same)

    def test_trials_are_copies(self) -> None:
        entry = waiter(state="held", granted=stamp(T0), yielded=3.0)
        trial = sf_entry.trial_yielded(entry, T0)
        self.assertEqual(("waiting", T0, "held"), (trial["state"], trial["rank"], entry["state"]))
        started = sf_entry.trial_held(waiter(yielded=3.0), stamp(T0))
        self.assertEqual(("held", stamp(T0)), (started["state"], started["granted"]))
        self.assertNotIn("yielded", started)

    def test_a_priority_change_keeps_the_class_asked_first(self) -> None:
        entry = waiter(priority="batch", aged="normal")
        sf_entry.to_reprioritized(entry, "interactive", T0 + 0.0004)
        sf_entry.to_reprioritized(entry, "normal", T0 + 10)
        self.assertEqual(("normal", "batch", T0 + 10), (entry["priority"], entry["reprioritized"],
                                                        entry["reprioritized_at"]))
        self.assertNotIn("aged", entry)

    def test_a_new_request_writes_the_fields_in_the_order_clients_always_have(self) -> None:
        entry = sf_entry.new_request(1, "s", 2, "l", "c", "/w", "t", "normal", "id", started=5, ticket="3",
                                     whole=True, meta={"a": "b"}, naming={"given": "g", "project": "p"},
                                     session={"name": "n"}, git={"head": "h"}, yieldable=True, until="u",
                                     yield_after=0.0)
        self.assertEqual(["pid", "start", "count", "label", "cmd", "cwd", "since", "state", "priority", "lease_id",
                          "started", "ticket", "all", "meta", "label_given", "project", "session", "git",
                          "yieldable", "until", "yield_after"], list(entry))
        bare = sf_entry.new_request(1, "s", 2, "l", "c", "/w", "t", "normal", "id")
        self.assertEqual(["pid", "start", "count", "label", "cmd", "cwd", "since", "state", "priority", "lease_id"],
                         list(bare))


class InstallerCopyTests(unittest.TestCase):
    """install.py migrates queues other versions wrote, so it stays self-contained: its copies of the ticket rule
    and of the states it accepts must agree with the package's."""

    def test_the_installer_normalises_tickets_as_the_package_does(self) -> None:
        for value in (None, 42, "42", "#42", " #7 ", "#x", "None", "", "  ", "a" * 200, "a" * 201, "t\x1b",
                      True, 1.5, ["1"]):
            with self.subTest(value=value):
                try:
                    expected: object = sf_entry.checked_ticket(value)
                except sf_config.SlotError:
                    expected = ValueError
                try:
                    found: object = install.history_ticket(value)
                except ValueError:
                    found = ValueError
                self.assertEqual(expected, found)

    def test_the_installer_accepts_the_states_the_package_knows(self) -> None:
        directory = _Path(tempfile.mkdtemp(prefix="slotfather-entry-"))
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        (directory / "queue").mkdir()
        path = directory / "queue" / "0000000001-7.json"
        for state in ("held", "waiting", "paused"):
            with self.subTest(state=state):
                path.write_text(json.dumps(waiter(state=state)))
                known = sf_entry.valid_identity(waiter(state=state))
                try:
                    install.require_idle(directory, {})
                    accepted = True
                except install.InstallError:
                    accepted = False
                self.assertEqual(known, accepted)


AGES_OFF = {"aging_batch": 0.0, "aging_normal": 0.0}  # a grant's class is its own: no `aged` to work out


class SequenceTests(unittest.TestCase):
    """Random sequences of grants, yields (and the resumes that follow), priority changes and releases on a live
    queue, mirrored with the transitions the replay applies: replay.grant() and entry.to_yielded() for the same
    decisions. The entries must be equal, field for field and in order, but for `reserved`, which only the live
    queue's plan keeps."""

    def live(self, queue: Queue) -> dict[str, dict[str, Any]]:
        """The entries this client knows; an unknown one (fathers#19) is checked on its own."""
        found = {path.name: json.loads(path.read_text()) for path in sorted((queue.directory / "queue").glob("*.json"))}
        return {name: entry for name, entry in found.items() if sf_entry.unknown(entry) is None}

    def granted(self, queue: Queue, mirror: dict[str, dict[str, Any]], done: list[str]) -> None:
        for name, entry in self.live(queue).items():
            if sf_entry.is_held(entry) and not sf_entry.is_held(mirror[name]):
                if "yielded_at" in mirror[name]:
                    done.append("resume")
                replay.grant(mirror[name], queue.clock)

    def same(self, queue: Queue, mirror: dict[str, dict[str, Any]], step: str) -> None:
        live = self.live(queue)
        self.assertEqual(sorted(mirror), sorted(live), step)
        for name, entry in live.items():
            self.assertEqual([(k, v) for k, v in entry.items() if k != "reserved"],
                             [(k, v) for k, v in mirror[name].items() if k != "reserved"], f"{step}: {name}")

    def play(self, seed: int) -> list[str]:
        rng = random.Random(seed)
        root = _Path(tempfile.mkdtemp(prefix="slotfather-entry-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        queue = Queue(root)
        mirror: dict[str, dict[str, Any]] = {}
        done: list[str] = []
        pids = iter(range(91000, 92000))
        with queue.running(), mock.patch.object(sf_leases, "held_again", unknown):
            with sf_state.locked(queue.directory):
                sf_state.save_order(dict(sf_config.ORDER_DEFAULTS, **AGES_OFF), queue.directory)
            # Half the sequences carry a newer client's entry (a state this one does not know), alive or not: it
            # must stay as it is through every step (fathers#19).
            strange = queue.entry(next(pids), "strange", rng.randint(1, 4), "paused", since=-60,
                                  alive=rng.random() < 0.5) if seed % 2 else None
            kept = strange.read_bytes() if strange else b""
            for step in range(30):
                queue.clock += rng.choice([1.0, 5.25, 30.5, 120.0, 600.125])
                live = self.live(queue)
                held = [n for n, e in live.items() if sf_entry.is_held(e)]
                waiting = [n for n in live if n not in held]
                op = rng.choice(["arrive", "arrive", "grant", "yield", "yield", "reprioritize", "release"])
                if op == "arrive" or not live:
                    pid = next(pids)
                    path = queue.entry(pid, f"r{pid}", rng.randint(1, 4), since=queue.clock - T0,
                                       priority=rng.choice(sf_config.PRIORITIES), yieldable=rng.random() < 0.6)
                    mirror[path.name] = json.loads(path.read_text())
                    done.append("arrive")
                elif op == "grant":
                    sf_leases.grant(queue.directory, sf_leases.process_table(queue.directory))
                    self.granted(queue, mirror, done)
                    done.append("grant")
                elif op == "yield" and held and waiting:
                    name, asker = rng.choice(held), rng.choice(waiting)
                    path = queue.directory / "queue" / name
                    settings = sf_schedule.order_settings(queue.directory, environment=False)
                    rank = sf_schedule.rank_time(live[asker], settings, queue.clock) + sf_config.YIELD_BEHIND
                    sf_state.write(sf_state.marker(path), {"by": asker[:-5], "label": live[asker]["label"],
                                                          "priority": "interactive", "count": 1, "at": "now"})
                    if sf_leases.yield_lease(path, out=io.StringIO()):
                        sf_entry.to_yielded(mirror[name], queue.clock, rank)
                        self.granted(queue, mirror, done)
                        done.append("yield")
                elif op == "reprioritize":
                    name = rng.choice(list(live))
                    priority = rng.choice(sf_config.PRIORITIES)
                    sf_leases.reprioritize(str(live[name]["pid"]), priority, queue.directory)
                    if sf_entry.priority_of(mirror[name]) != priority:
                        sf_entry.to_reprioritized(mirror[name], priority, queue.clock)
                    self.granted(queue, mirror, done)
                    done.append("reprioritize")
                elif op == "release":
                    name = rng.choice(list(live))
                    lease = sf_leases.Lease(int(live[name]["count"]), queue.directory / "queue" / name)
                    lease.status = 0
                    lease.release()
                    line = json.loads((queue.directory / "history.jsonl").read_text().splitlines()[-1])
                    self.assertEqual(sf_entry.outcome(mirror.pop(name)), line["outcome"], f"step {step}")
                    self.granted(queue, mirror, done)
                    done.append("release")
                self.same(queue, mirror, f"seed {seed} step {step} {op}")
                if strange:
                    self.assertEqual(kept, strange.read_bytes(), f"seed {seed} step {step} {op}: left alone")
        return done

    def test_live_transitions_leave_what_the_replay_applies(self) -> None:
        done: list[str] = []
        for seed in range(40):
            done += self.play(seed)
        for op in ("arrive", "grant", "yield", "resume", "reprioritize", "release"):
            self.assertIn(op, done, "every transition is reached")
        self.assertGreaterEqual(done.count("yield"), 5)


if __name__ == "__main__":
    unittest.main()
