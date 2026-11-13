"""The entry lifecycle (entry.py): each transition where it is allowed and where it is refused, the races between
a reap and a sweep, a supervisor reviving a `gone` entry, a resource entry through begin_reap/end_reap, and no
state string outside entry.py."""
from __future__ import annotations

import os
import re
from typing import Any, Callable, List, Optional
import unittest
from unittest import mock

from support import ROOT, Case, as_entry, got, plain, proc, table

from junkfather import entry, resources, sweep

A = "Thu Nov 12 10:00:00 2026"


class TransitionTests(Case):
    def make(self, state: str, **fields: Any) -> str:
        record = entry.new(self.store, ["sleep", "600"], 3600.0, "owner", None, {"kind": "shell"}, "attached", "/")
        record["state"] = state
        record.update(as_entry(fields))
        self.store.save(record)
        return record["id"]

    def check(self, transition: Callable[..., Optional[Any]], allowed: List[str], *args: Any, **options: Any
              ) -> None:
        """`transition` moves an entry in each `allowed` state, and refuses every other state without a write."""
        for state in entry.STATES:
            ident = self.make(state)
            before = self.store.path(ident).read_bytes()
            moved = transition(self.store, ident, *args, **options)
            if state in allowed:
                self.assertIsNotNone(moved, f"{transition.__name__} from {state}")
            else:
                self.assertIsNone(moved, f"{transition.__name__} from {state}")
                self.assertEqual(before, self.store.path(ident).read_bytes(), "a refused transition writes nothing")

    def test_each_transition_from_its_allowed_states_only(self):
        leader = proc(4100, cmd="sleep 600")
        self.check(entry.started, [entry.STARTING], leader, {"pid": 1, "start": None})
        self.check(entry.failed, [entry.STARTING], "no")
        self.check(entry.failed, [entry.STARTING, entry.FAILED], "no", start_error=True)
        self.check(entry.known, [entry.RUNNING], [leader])
        self.check(entry.gone, [entry.RUNNING])
        self.check(entry.revived, [entry.GONE])
        self.check(entry.extended, [entry.RUNNING, entry.STARTING], 60.0)
        self.check(entry.handed, [entry.RUNNING, entry.STARTING], {"kind": "claude", "session": "s2"})
        self.check(entry.noted_error, [entry.RUNNING], {"reason": "r"})
        self.check(entry.release_stuck, [entry.REAPING])

    def test_what_each_transition_writes(self):
        leader = proc(4100, cmd="sleep 600")
        ident = self.make(entry.STARTING)
        started = got(entry.started(self.store, ident, leader, {"pid": 7, "start": A}))
        self.assertEqual((entry.RUNNING, 4100, 4100, A, "sleep 600", [[4100, A]], [[4100, A, "sleep 600"]]),
                         (started["state"], started["pid"], started["pgid"], started["start"], started["seen"],
                          started["known"], started["known_seen"]))
        gone = got(entry.gone(self.store, ident, now=5.0))
        self.assertEqual((entry.GONE, 5.0, []), (gone["state"], gone["ended"], gone["known"]))
        revived = got(entry.revived(self.store, ident))
        self.assertEqual((entry.RUNNING, None), (revived["state"], revived["ended"]))
        extended = got(entry.extended(self.store, ident, 60.0, now=100.0))
        self.assertEqual((60.0, 160.0), (extended["ttl"], extended["expires"]))
        target = {"kind": "claude", "session": "s2", "lane": None}
        moved = entry.handed(self.store, ident, target, now=7.0)
        assert moved is not None
        handed, before = plain(moved[0]), moved[1]
        self.assertEqual(("owner", "s2", {"kind": "shell"}, {"kind": "shell"}),
                         (handed["keep"], handed["owner"]["session"], handed["handed"][-1]["from"], before))
        self.assertEqual({"kind": "claude", "session": "s2", "lane": None}, target, "the caller's target is untouched")
        failed = got(entry.failed(self.store, self.make(entry.STARTING), "why", now=9.0))
        self.assertEqual((entry.FAILED, "why", 9.0), (failed["state"], failed["error"], failed["ended"]))

    def test_the_entry_type_names_every_key_the_lifecycle_writes(self):
        known = set(entry.Entry.__annotations__)
        record = entry.new(self.store, ["sleep", "600"], 3600.0, "owner", None, {"kind": "shell"}, "attached", "/")
        self.assertLessEqual(set(entry.Entry.__required_keys__), set(record), "new writes every required key")
        entry.save_starting(self.store, record)
        ident = record["id"]
        entry.started(self.store, ident, proc(4100, cmd="sleep 600"), {"pid": 7, "start": A})
        entry.handed(self.store, ident, {"kind": "claude", "session": "s2"})
        entry.noted_error(self.store, ident, {"reason": "r"})
        me = entry.begin_reap(self.store, ident, "test", table(proc(os.getpid())))
        self.assertIsNotNone(me)
        reaping = got(self.store.load(ident))
        entry.end_reap(self.store, ident, os.getpid(), True, {"reason": "test"}, {})
        failed = got(entry.failed(self.store, self.make(entry.STARTING), "why"))
        resource = entry.new(self.store, [], 60.0, "owner", None, {}, "resource", "/")
        entry.resource_owned(self.store, resource, {"kind": "command", "id": "x"}, lambda e: False)
        gone = got(entry.gone(self.store, resource["id"]))
        for written in (reaping, got(self.store.load(ident)), failed, gone):
            self.assertLessEqual(set(written), known, f"keys the Entry type does not name: {set(written) - known}")

    def test_a_resource_entry_keeps_its_fields_when_gone(self):
        record = entry.new(self.store, [], 60.0, "owner", None, {}, "resource", "/")
        owned, holder = entry.resource_owned(self.store, record, {"kind": "command", "id": "x"}, lambda e: True)
        self.assertIsNone(holder)
        assert owned is not None
        before = set(owned)
        gone = got(entry.gone(self.store, owned["id"], now=3.0))
        self.assertEqual(before | {"ended"}, set(gone))
        self.assertEqual(entry.GONE, gone["state"])

    def test_a_held_resource_is_refused_to_a_second_entry(self):
        first = entry.new(self.store, [], 60.0, "owner", None, {}, "resource", "/")
        entry.resource_owned(self.store, first, {"kind": "command", "id": "x"}, lambda e: True)
        second = entry.new(self.store, [], 60.0, "owner", None, {}, "resource", "/")
        refused, holder = entry.resource_owned(self.store, second, {"kind": "command", "id": "x"},
                                               lambda e: (e.get("resource") or {}).get("id") == "x")
        self.assertEqual((None, first["id"]), (refused, holder))
        self.assertEqual([first["id"]], [e["id"] for e in self.store.entries()])


class ReapTransitionTests(Case):
    def running(self, mode: str = "detached") -> str:
        record = entry.new(self.store, ["sleep", "600"], 3600.0, "owner", None, {}, mode, "/")
        record.update(state=entry.RUNNING, pid=4100)
        self.store.save(record)
        return record["id"]

    def test_one_reaper_holds_an_entry_and_only_it_ends_the_reap(self):
        ident = self.running()
        me = entry.begin_reap(self.store, ident, "test", table(proc(os.getpid())))
        self.assertEqual(os.getpid(), me)
        self.assertIsNone(entry.begin_reap(self.store, ident, "again", table(proc(os.getpid()))))
        self.assertIsNone(entry.end_reap(self.store, ident, os.getpid() + 1, True, {}, {}), "not its reaper")
        self.assertEqual(entry.REAPING, self.state_of(ident))
        ended = got(entry.end_reap(self.store, ident, os.getpid(), True, {"reason": "test"}, {}))
        self.assertEqual((entry.REAPED, {"reason": "test"}), (ended["state"], ended["end"]))
        self.assertNotIn("reaping", ended)

    def test_a_failed_reap_returns_the_entry_to_running_with_its_error(self):
        ident = self.running()
        me = entry.begin_reap(self.store, ident, "test", table(proc(os.getpid())))
        assert me is not None
        back = got(entry.end_reap(self.store, ident, me, False, {}, {"reason": "test", "left": [4100]}))
        self.assertEqual((entry.RUNNING, [4100]), (back["state"], back["last_error"]["left"]))

    def test_a_resource_entry_goes_through_the_same_reap(self):
        ident = self.running(mode="resource")
        me = entry.begin_reap(self.store, ident, "test", table(proc(os.getpid())))
        assert me is not None
        self.assertEqual(entry.REAPED, (entry.end_reap(self.store, ident, me, True, {"steps": []}, {}) or {})["state"])

    def test_a_sweep_that_comes_second_does_not_undo_a_reap(self):
        """A reap begins while a sweep holds an older read: the sweep's gone and known writes are refused."""
        ident = self.running()
        me = entry.begin_reap(self.store, ident, "test", table(proc(os.getpid())))
        self.assertIsNone(entry.gone(self.store, ident))
        self.assertIsNone(entry.known(self.store, ident, [proc(4100)]))
        self.assertIsNone(entry.release_stuck(self.store, "jnothing"))
        self.assertEqual(entry.REAPING, self.state_of(ident))
        assert me is not None
        entry.end_reap(self.store, ident, me, True, {}, {})
        self.assertIsNone(entry.revived(self.store, ident), "a reaped entry is never revived")

    def test_a_reap_that_comes_second_skips_an_entry_the_sweep_marked_gone(self):
        ident = self.running()
        self.assertIsNotNone(entry.gone(self.store, ident))
        self.assertIsNone(entry.begin_reap(self.store, ident, "late", table(proc(os.getpid()))))
        self.assertEqual(entry.GONE, self.state_of(ident))

    def test_the_sweep_releases_a_stuck_reap_and_marks_the_entry_gone(self):
        ident = self.running()
        entry.begin_reap(self.store, ident, "test", table(proc(os.getpid())))
        self.store.update(ident, lambda e: e.get("reaping", {}).update(at=0.0, by=4242, by_start=A))
        here = table(proc(os.getpid(), start=A, cmd="python3"))
        with mock.patch.object(resources, "unowned", return_value=([], [])):
            report = sweep.sweep(self.store, table=here, cwds={}, sessions=[], now=sweep.REAPING_STUCK + 10)
        self.assertEqual([ident], [g["id"] for g in report["gone"]])
        self.assertEqual(entry.GONE, self.state_of(ident))


class NoStateLiteralTests(unittest.TestCase):
    """States are written and compared through entry.py's names only. A word that is also a reap result ("gone",
    "failed") counts only next to `state`; a report's key or an entry field of the same name is not a state, and a
    word of output says so with `# text, not a state`."""

    WORDS = re.compile(r"""(?<![\[\w])(?<!get\()["'](starting|running|reaping|exited|reaped)["'](?!\s*[:\]])""")
    STATE = re.compile(r"""state\W{1,8}["'](starting|running|reaping|exited|reaped|gone|failed)["']""")

    def test_no_state_string_outside_entry_py(self):
        found: List[str] = []
        for path in sorted((ROOT / "junkfather").glob("*.py")):
            if path.name == "entry.py":
                continue
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if (self.WORDS.search(line) or self.STATE.search(line)) and "# text, not a state" not in line:
                    found.append(f"{path.name}:{number}: {line.strip()}")
        self.assertEqual([], found)

    def test_the_check_finds_a_state_literal(self):
        for line in ('if e.get("state") == "running":', 'e.update(state="gone")', "x in ('running', 'starting')"):
            self.assertTrue(self.WORDS.search(line) or self.STATE.search(line), line)
        for line in ('report["running"] += 1', '{"running": 0}', 'e.get("reaping")', '"result": "gone"'):
            self.assertFalse(self.WORDS.search(line) or self.STATE.search(line), line)


if __name__ == "__main__":
    unittest.main()
