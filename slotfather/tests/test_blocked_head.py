"""A head blocked by leases with no end estimate (fathers#160): requests that fit the idle slots start by their p90,
a head whose reserved start passed `reservation_timeout` ago gives way naming the leases, and `status` says so."""
from __future__ import annotations
import sys as _path_sys
from pathlib import Path as _Path
_path_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "src"))

import contextlib
import io
import json
import shutil
import tempfile
import time
from typing import Any, Callable, Optional
import unittest

from test_entry_pins import T0, Queue, normal

from slotfather import config as sf_config
from slotfather import leases as sf_leases
from slotfather import query as sf_query
from slotfather import queue as sf_queue
from slotfather import schedule as sf_schedule

M = 1_800_000_000.0


def stamp(offset: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(M + offset))


def waiter(label: str, count: int, queued: float, **extra: Any) -> dict[str, Any]:
    return {"label": label, "count": count, "cmd": label, "since": stamp(queued), "state": "waiting", **extra}


def holder(label: str, count: int, granted: float) -> dict[str, Any]:
    return {"label": label, "count": count, "cmd": label, "since": stamp(granted), "granted": stamp(granted),
            "state": "held", "pid": abs(hash(label)) % 90000 + 1000}


def runs(**estimates: float) -> Callable[[dict[str, Any]], Optional[float]]:
    def estimate(entry: dict[str, Any]) -> float | None:
        return estimates.get(entry["label"])
    return estimate


class PlanTests(unittest.TestCase):
    """The rule on fixed queues: 8 slots, three 1-slot lane checks with no estimate running for two hours, an
    all-slot measurement the head since its reserved start passed."""

    def setUp(self) -> None:
        self.settings: dict[str, Any] = dict(sf_config.ORDER_DEFAULTS)
        self.held = [holder(f"lane-{n}", 1, -7200) for n in "abc"]

    def plan(self, queue: list[dict[str, Any]], estimates: dict[str, float], p90: dict[str, float] | None = None,
             **settings: Any) -> sf_schedule.Plan:
        return sf_schedule.plan(queue, self.held, 8, M, None, runs(**estimates), settings={**self.settings, **settings},
                                window=600.0, p90=runs(**p90) if p90 is not None else None)

    def test_a_head_past_its_promise_does_not_keep_idle_slots_from_short_requests(self) -> None:
        measure = waiter("measure", 8, -5400, reserved=M - 600)  # its promise passed 10 minutes ago
        install = waiter("install", 1, -60)
        found = self.plan([measure, install], {"install": 60})
        self.assertEqual((["install"], "measure"), ([e["label"] for e in found.grants], found.head and found.head["label"]))
        self.assertEqual(["lane-a", "lane-b", "lane-c"], sorted(e["label"] for e in found.blocked))
        self.assertEqual(M - 600, found.reserved, "the promise stays as it was")

    def test_what_passes_is_bound_by_its_p90_and_by_the_heads_projected_start(self) -> None:
        measure = waiter("measure", 8, -5400, reserved=M - 600)
        # the lanes, two hours in with no estimate, are expected to run on for the overrun: 3600 s more
        found = self.plan([measure, waiter("install", 1, -60)], {"install": 60}, p90={"install": 3700})
        self.assertEqual([], found.grants, "its p90 ends after the head's projected start")
        found = self.plan([measure, waiter("install", 1, -60)], {"install": 60}, p90={"install": 3500})
        self.assertEqual(["install"], [e["label"] for e in found.grants])
        found = self.plan([measure, waiter("unknown", 1, -60)], {}, fallback=3700.0)
        self.assertEqual([], found.grants, "a run nobody knows counts the fallback")

    def test_a_promise_not_yet_due_or_leases_with_estimates_keep_the_rule_before(self) -> None:
        fresh = waiter("measure", 8, -5400, reserved=M + 600)
        self.assertEqual([], self.plan([fresh, waiter("install", 1, -60)], {"install": 300}).grants,
                         "3 x 300 s ends after the start it was promised: it waits as before")
        known = {f"lane-{n}": 600.0 for n in "abc"}
        passed = waiter("measure", 8, -5400, reserved=M - 600)
        found = self.plan([passed, waiter("install", 1, -60)], {**known, "install": 60})
        self.assertEqual(([], []), (found.grants, found.blocked), "leases past a known estimate are no unknown")

    def test_a_head_past_its_promise_by_the_timeout_gives_way_naming_the_leases(self) -> None:
        late = waiter("measure", 8, -9000, reserved=M - 1900)
        unknown = waiter("unknown", 1, -60)
        install = waiter("install", 1, -30)
        found = self.plan([late, unknown, install], {"install": 4000})
        self.assertIsNone(found.head)
        self.assertEqual(["install"], [e["label"] for e in found.grants],
                         "it holds the idle slots back no more; a request with no history is no backfill against it")
        self.assertEqual([("measure", ["lane-a", "lane-b", "lane-c"])],
                         [(e["label"], sorted(b["label"] for b in blockers)) for e, blockers in found.dropped])
        self.assertIn("measure gave way: its reserved start passed 31m40s ago while leases with no end estimate "
                      "hold its slots (lane-a, lane-b, lane-c)", found.reason or "")
        self.assertEqual([], sf_schedule.reservations([late, unknown, install], found), "it keeps its promise and place")
        kept = self.plan([late, unknown], {}, reservation_timeout=0.0)
        self.assertEqual(("measure", []), (kept.head and kept.head["label"], kept.dropped), "0: never")
        self.assertIsNone(self.plan([late], {}, reservation_timeout=2000.0).dropped or None, "not yet")

    def test_a_head_that_gave_way_is_not_starved_by_new_requests_with_no_history(self) -> None:
        """fathers#160 review: once it gave way, a stream of requests nobody knows the run of starts nothing past it,
        and leases granted after it gave way do not let it give way again."""
        late = waiter("measure", 8, -9000, reserved=M - 1900, head_dropped=M - 600)
        stream = [waiter(f"new-{n}", 1, -50 + n) for n in range(5)]
        found = self.plan([late, *stream], {})
        self.assertEqual(([], ["measure"]), ([e["label"] for e in found.grants], [e["label"] for e, _ in found.dropped]),
                         "the lanes that blocked it still run: it gives way, and none of the stream starts")
        self.held = [holder("lane-a", 1, -7200), holder("since", 1, -300)]  # `since` was granted after it gave way
        still = self.plan([late, *stream], {})
        self.assertEqual(["lane-a"], [b["label"] for b in still.dropped[0][1]],
                         "only what blocked it before it gave way counts")
        self.held = [holder("since", 1, -300), holder("later", 1, -60)]
        back = self.plan([late, *stream], {})
        self.assertEqual(("measure", [], []), (back.head and back.head["label"], back.dropped,
                                               [e["label"] for e in back.grants]),
                         "only leases granted after it gave way: it is the head again and holds the slots back")


class GrantTests(unittest.TestCase):
    """grant() on a queue of 4 slots (the entry pins' harness: fixed clock, fake holders)."""

    def setUp(self) -> None:
        root = _Path(tempfile.mkdtemp(prefix="slotfather-blocked-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        self.queue = Queue(root)
        history = [{"label": "install", "cmd": "make install", "ran": 60, "granted": "2025-11-12T09:00:00Z",
                    "outcome": "released"}] * 3
        (self.queue.directory / "history.jsonl").write_text("".join(json.dumps(h) + "\n" for h in history))
        for n, label in enumerate(("lane-a", "lane-b")):
            self.queue.entry(90101 + n, label, 1, "held", since=-7200)

    def grant(self) -> str | None:
        with contextlib.redirect_stderr(io.StringIO()):
            return sf_leases.grant(self.queue.directory, sf_queue.process_table(self.queue.directory))

    def states(self) -> dict[str, dict[str, Any]]:
        return {e["label"]: e for e in sf_query.status_json(self.queue.directory)["entries"]}

    def test_short_requests_take_the_idle_slots_and_status_names_the_blocking_leases(self) -> None:
        with self.queue.running():
            self.queue.entry(90201, "measure", 4, since=-5400, reserved=T0 - 600)
            self.queue.entry(90202, "install", 1, since=-60)
            self.queue.entry(90203, "unknown", 1, since=-50)
            lines = sf_query.status_lines(self.queue.directory)
            self.assertIn("head measure blocked by leases lane-a (pid 90101), lane-b (pid 90102) with no end "
                          "estimate; 1 slot idle", lines)
            head = self.states()["measure"]
            self.assertEqual(([{"label": "lane-a", "pid": 90101}, {"label": "lane-b", "pid": 90102}], 1),
                             (head["blocked_by"], head["idle_slots"]))
            self.assertNotIn("blocked_by", self.states()["install"])
            self.grant()
            self.assertEqual(("held", "waiting", "waiting"),
                             tuple(self.states()[label]["state"] for label in ("install", "measure", "unknown")))

    def test_a_head_past_its_promise_by_the_timeout_gives_way_once_and_names_the_leases(self) -> None:
        with self.queue.running():
            measure = self.queue.entry(90201, "measure", 4, since=-9000, reserved=T0 - 1900)
            self.queue.entry(90203, "unknown", 1, since=-50)
            self.queue.entry(90204, "install", 1, since=-40)
            reason = self.grant() or ""
            self.assertIn("measure gave way", reason)
            self.assertIn("(lane-a, lane-b)", reason)
            self.assertEqual(("held", "waiting"), (self.states()["install"]["state"], self.states()["unknown"]["state"]),
                             "a request with history takes an idle slot; one with none waits behind it")
            entry = json.loads(measure.read_text())
            self.assertEqual((T0, T0 - 1900), (entry["head_dropped"], entry["reserved"]))
            self.assertEqual(["lane-a", "lane-b"], [e["label"] for e in self.states()["measure"]["gave_way"]])
            self.assertIn("gave-way-to=lane-a,lane-b", "\n".join(sf_query.status_lines(self.queue.directory)))
            self.queue.at(30)
            self.grant()
            events = [json.loads(line) for line in (self.queue.directory / "events.jsonl").read_text().splitlines()]
            dropped = [e for e in events if e["kind"] == "request.head_dropped"]
            self.assertEqual(1, len(dropped), "said once")
            self.assertEqual((1900, ["lane-a", "lane-b"]), (dropped[0]["details"]["late"],
                             [b["label"] for b in dropped[0]["details"]["blocked_by"]]))
            log = normal((self.queue.directory / "queue.log").read_text())
            self.assertIn("measure (pid 90201) gave way as the head: its reserved start passed 1900 s ago while "
                          "leases with no end estimate hold its slots: lane-a (pid 90101), lane-b (pid 90102)", log)
            # once the leases end its slots are free: it starts, the mark gone
            for pid in (90101, 90102, 90204):
                del self.queue.table[pid]
            self.queue.at(60)
            self.grant()
            entry = json.loads(measure.read_text())
            self.assertEqual("held", entry["state"])
            self.assertNotIn("head_dropped", entry)


if __name__ == "__main__":
    unittest.main()
