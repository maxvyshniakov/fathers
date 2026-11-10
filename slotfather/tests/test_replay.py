#!/usr/bin/env python3
"""The replay (#643): recorded requests through the queue orders. tests/fixtures/replay is the queue of
2026-11-02 22:10 to 2026-11-03 01:48 UTC (commands cut to their kind): the 6-slot measurements and the incident of
#643, where one 6-slot request held back twelve others with five slots idle. tests/fixtures/replay-2026-11-10 is
the queue of 2026-11-10 from 15:59 to 20:51 UTC (#5; commands cut to their kind, no directories or metadata;
queue.log from 2026-11-07 for the estimates): whole-machine reruns and a batch, yieldable measurement that were
the head while one-slot work waited beside idle slots."""
from __future__ import annotations
import sys as _path_sys
from pathlib import Path as _Path
_path_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "src"))
from slotfather import config as sf_config


import json
import pathlib
import shutil
import tempfile
import unittest
from typing import Any, cast

from test_slotfather import ROOT
from test_usage import load

replay = cast(Any, load(str(ROOT / "tools" / "replay.py"), "slotfather_replay_under_test"))  # a script, loaded

FIXTURE = ROOT / "tests" / "fixtures" / "replay"
NOW = sf_config.seconds("2026-11-03T01:48:36Z")  # when the live queue in the fixture was copied
INCIDENT = (sf_config.seconds("2026-11-03T00:48:50Z"), sf_config.seconds("2026-11-03T01:40:00Z"))
SHIPPED, CURRENT = "backfill (shipped)", "window (current)"
CLASSES = "classes + yields (#643 shipped)"
TODAY = ROOT / "tests" / "fixtures" / "replay-2026-11-10"
TODAY_SINCE, TODAY_NOW = sf_config.seconds("2026-11-10T15:59:00Z"), sf_config.seconds("2026-11-10T20:51:00Z")


class RecordedReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        policies = {name: replay.POLICIES[name] for name in (CURRENT, SHIPPED, "fifo (window 0)",
                                                              "backfill, estimates noisy", CLASSES,
                                                              "  estimates noisy")}
        cls.whole = replay.compare(FIXTURE, NOW, 6, policies)
        cls.incident = replay.compare(FIXTURE, NOW, 6, policies, since=INCIDENT[0], until=INCIDENT[1])

    def test_the_current_order_replays_the_recorded_waits(self):
        found = self.whole["validation"]
        self.assertEqual(57, found["requests"])
        self.assertGreaterEqual(found["within_60s"], 0.95)
        self.assertLess(found["mean_abs_error_s"], 30)
        self.assertAlmostEqual(found["recorded_h"], found["replayed_h"], delta=0.5)

    def test_backfill_waits_less_without_holding_large_requests_back_longer(self):
        current, shipped = (self.whole["policies"][name] for name in (CURRENT, SHIPPED))
        self.assertLess(shipped["total_h"], 0.9 * current["total_h"])
        self.assertLessEqual(shipped["max_s"], current["max_s"])
        self.assertLessEqual(shipped["large_max_s"], current["large_max_s"])
        self.assertLess(shipped["blocked_slot_h"], current["blocked_slot_h"])

    def test_whole_machine_batch_requests_hold_back_no_other_work_and_leave_no_slot_idle(self):
        # #5: the 6-slot measurements (batch) let waiting normal and interactive work go first; no more of them give
        # up, and nothing that fits waits beside an idle slot. How long they may wait is the policy's, in
        # AllSlotTraceTests.
        current, shipped = (self.whole["policies"][name] for name in (CURRENT, SHIPPED))
        self.assertEqual(9, shipped["whole"]["requests"])
        self.assertLessEqual(shipped["whole"]["never_ran"], current["whole"]["never_ran"])
        for name in (SHIPPED, CLASSES):
            self.assertLess(self.whole["policies"][name]["small_blocked_slot_h"], 0.05)

    def test_wrong_estimates_do_not_hold_large_requests_back_longer(self):
        current, noisy = (self.whole["policies"][name] for name in (CURRENT, "backfill, estimates noisy"))
        self.assertLessEqual(noisy["large_max_s"], current["large_max_s"])

    def test_classes_wait_less_and_serve_interactive_work_first_without_starving_batch_work(self):
        # Before #5 classes took a fifth off plain backfill's total here. Since #5 both let waiting normal and
        # interactive work pass the 6-slot measurements, and what differs is where each 35-minute measurement lands:
        # with classes ci-baseline-warm takes the machine at 00:24 and the pre-push checks that come then wait for it.
        # So classes are measured against the window order, as the shipped order is.
        current, classes = (self.whole["policies"][name] for name in (CURRENT, CLASSES))
        self.assertLess(classes["total_h"], 0.5 * current["total_h"])
        self.assertLess(classes["classes"]["interactive"]["p90_s"], current["classes"]["interactive"]["p90_s"])
        self.assertLessEqual(classes["large_max_s"], current["large_max_s"])
        self.assertLessEqual(classes["classes"]["batch"]["delay_max_s"], current["classes"]["batch"]["delay_max_s"])
        noisy, before = (self.whole["policies"][name] for name in ("  estimates noisy", "backfill, estimates noisy"))
        bound = replay.CLASSES["interactive"] + replay.CLASSES["batch"]  # the most a class moves a request
        self.assertLessEqual(noisy["classes"]["batch"]["delay_max_s"], before["classes"]["batch"]["delay_max_s"] + bound)
        incident = self.incident["policies"]
        self.assertLess(incident[CLASSES]["total_h"], 0.7 * incident[CURRENT]["total_h"])

    def test_the_incident_waits_far_less(self):
        current, shipped = (self.incident["policies"][name] for name in (CURRENT, SHIPPED))
        self.assertLess(shipped["total_h"], 0.7 * current["total_h"])
        self.assertLessEqual(shipped["blocked_slot_h"], current["blocked_slot_h"])


class YieldTraceTests(unittest.TestCase):
    """A Straylight gate (batch, yieldable) holds five of six slots and a lane check the sixth, both for an hour; a
    pre-push check (interactive) comes ten minutes in."""

    def requests(self):
        checks = "/w/verify.sh --checks " + ",".join(f"c{n}" for n in range(60)) + " --jobs 4"
        found = [{"label": "lane-1-gate", "cmd": checks, "count": 5, "arrival": 0.0, "run": 3600.0},
                 {"label": "lane-2", "cmd": "/w/verify.sh --checks a,b", "count": 1, "arrival": 0.0, "run": 3600.0},
                 {"label": "sid-kit-wt-1", "cmd": "node scripts/pre-push.mjs origin", "count": 1, "arrival": 600.0,
                  "run": 60.0}]
        for number, request in enumerate(found):
            request.update(id=number, patience=None, waited=None, source="history",
                           arrival=sf_config.seconds("2026-11-02T10:00:00Z") + request["arrival"])
        return found

    def test_the_gate_yields_to_the_pre_push_check_and_resumes(self):
        found = self.requests()
        self.assertEqual(["batch", "normal", "interactive"], [replay.classify(r, 6) for r in found])
        seed = [("lane-1-gate", found[0]["cmd"], 3600.0), ("lane-2", found[1]["cmd"], 3600.0)]
        both = replay.Replay(found, 6, replay.POLICIES[CLASSES], seed)
        gate, _, check = both.simulate()
        waited = check["granted"] - found[2]["arrival"]
        self.assertGreaterEqual(waited, 120, "it asks once it has waited yield_after")
        self.assertLessEqual(waited, 120 + 300 + replay.POLL, "the gate yields at the end of its step")
        self.assertEqual(1, gate["yields"])
        self.assertAlmostEqual(60, gate["yielded"], delta=replay.POLL + 1, msg="back once the check is done")
        self.assertAlmostEqual(found[0]["arrival"] + 3600 + gate["yielded"], gate["ended"], delta=1)
        alone = replay.Replay(found, 6, replay.POLICIES["  classes only"], seed).simulate()
        self.assertAlmostEqual(3600 - 600, alone[2]["granted"] - found[2]["arrival"], delta=replay.POLL,
                               msg="without yields it waits for the first holder to end")


class AllSlotTraceTests(unittest.TestCase):
    """A batch `-n all` measurement among checks (one a minute, five minutes each). Before #5 requests that ranked
    ahead of it started first and it then held back the rest (review P2-2: it started after both class offsets).
    Since #5 it lets interactive checks go first, and takes the machine once they stop; among batch checks it is the
    head as before, so it starts by its limit plus the runs of what passed it."""

    def requests(self, gaps: range | tuple[int, ...] = (), priority: str | None = None) -> list[dict[str, Any]]:
        start = sf_config.seconds("2026-11-02T10:00:00Z") or 0.0
        found: list[dict[str, Any]] = [{"label": "lane-1", "cmd": "make", "count": 1, "arrival": 0.0, "run": 600.0},
                 {"label": "lane-2-measure", "cmd": "measure.sh", "count": 6, "arrival": 10.0, "run": 600.0}]
        found += [{"label": "sid-kit-wt-1", "cmd": "node scripts/pre-push.mjs origin", "count": 1,
                   "arrival": 20.0 + 60 * n, "run": 300.0, "priority": priority} for n in range(240) if n not in gaps]
        for number, request in enumerate(found):
            request.update(id=number, patience=None, waited=None, source="history",
                           arrival=start + request["arrival"])
        self.assertEqual("batch", replay.classify(found[1], 6))
        return found

    def waited(self, found: list[dict[str, Any]], policy: str) -> float:
        seed = [("sid-kit-wt-1", found[2]["cmd"], 300.0), ("lane-1", "make", 600.0)]
        head = replay.Replay(found, 6, replay.POLICIES[policy], seed).simulate()[1]
        self.assertTrue(head["defers"])
        return head["granted"] - found[1]["arrival"]

    def test_an_all_slot_batch_request_waits_for_the_stream_of_checks_to_end(self):
        found = self.requests()
        last = found[-1]["arrival"] + 300 - found[1]["arrival"]  # when the last check ends
        for policy in (CLASSES, SHIPPED):
            with self.subTest(policy):
                self.assertAlmostEqual(last, self.waited(found, policy), delta=2 * replay.POLL)

    def test_it_starts_in_the_first_quiet_moment(self):
        found = self.requests(gaps=range(60, 75))  # no check comes from 3620 s to 4520 s
        quiet = found[2 + 59]["arrival"] + 300 - found[1]["arrival"]  # the last check before the gap ends
        self.assertAlmostEqual(quiet, self.waited(found, CLASSES), delta=2 * replay.POLL)

    def test_among_batch_checks_it_is_the_head_and_starts_by_its_limit(self):
        # Review of 649e879: batch one-slot work must not pass a batch `-n all` request forever.
        found = self.requests(priority="batch")
        for policy in (CLASSES, SHIPPED):
            with self.subTest(policy):
                self.assertLessEqual(self.waited(found, policy),
                                     float(sf_config.ORDER_DEFAULTS["limit"]) + 300 + 2 * replay.POLL)


class TodayReplayTests(unittest.TestCase):
    """#5: 2026-11-10, replayed through the installed order (classes and yields). Before #5 it left 7.1 slot-hours
    idle while a one-slot normal or interactive request waited and fitted (the order of c2d793c on this fixture);
    a waiter whose rerun would give up before the leases it waited for ran on was the head past its limit, and a
    batch, yieldable measurement of the whole machine was the head in front of normal lane checks."""

    @classmethod
    def setUpClass(cls):
        policies = {name: replay.POLICIES[name] for name in (CURRENT, CLASSES)}
        cls.today = replay.compare(TODAY, TODAY_NOW, 6, policies, since=TODAY_SINCE, until=TODAY_NOW)

    def test_no_slot_stays_idle_while_one_slot_work_waits(self):
        found = self.today["policies"][CLASSES]
        # A few slot-minutes remain: interactive heads (a 2-slot build, a `-n all` rerun waiting on leases of unknown
        # run) reserving their start, as a head should.
        self.assertLess(found["small_blocked_slot_h"], 0.1)
        self.assertLess(found["blocked_slot_h"], 0.1)
        self.assertLess(found["total_h"], 0.5 * self.today["policies"][CURRENT]["total_h"])

    def test_recorded_priorities_and_live_waits_are_replayed(self):
        found = replay.requests(TODAY, TODAY_NOW)
        live = {r["label"]: r for r in found if r["source"] == "live"}
        self.assertEqual(("batch", True, None), (live["data-grid-perf-61-confirm"]["priority"],
                                                 live["data-grid-perf-61-confirm"]["yieldable"],
                                                 live["data-grid-perf-61-confirm"]["give_up"]))
        self.assertEqual(("interactive", sf_config.seconds("2026-11-10T21:02:52Z")),
                         (live["lane-480-offline-expiry-alone"]["priority"],
                          live["lane-480-offline-expiry-alone"]["give_up"]))
        self.assertEqual({"interactive", "normal", "batch"}, {r["priority"] for r in found if r["source"] == "history"})
        self.assertEqual(3, self.today["policies"][CLASSES]["whole"]["requests"])


class TraceTests(unittest.TestCase):
    """Requests a slotfather without history left only in queue.log are recovered by their queue sequence."""

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="slotfather-replay-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def history(self, label, count, queued, granted, ended, pid, outcome="released"):
        at = lambda clock: None if clock is None else f"2026-11-02T{clock}Z"
        ran = None if granted is None else int(sf_config.seconds(at(ended)) - sf_config.seconds(at(granted)))
        return {"label": label, "cmd": f"make {label}", "count": count, "pid": pid, "queued": at(queued),
                "granted": at(granted), "ended": at(ended), "outcome": outcome, "ran": ran,
                "waited": int(sf_config.seconds(at(granted or ended)) - sf_config.seconds(at(queued)))}

    def test_recorded_and_unrecorded_requests_join_in_queue_order(self):
        items = [self.history("first", 1, "10:00:00", "10:00:00", "10:10:00", 11),
                 self.history("last", 1, "10:20:00", "10:30:00", "10:31:00", 14)]
        (self.dir / "history.jsonl").write_text("".join(json.dumps(i) + "\n" for i in items))
        (self.dir / "queue.log").write_text(
            "2026-11-02T10:00:00Z granted 1 to first (pid 11)\n"
            "2026-11-02T10:05:00Z granted 5 to old-gate (pid 12)\n"  # an old client: no history line
            "2026-11-02T10:10:00Z released 1 (0000000001-11.json)\n"
            "2026-11-02T10:25:00Z released 5 (0000000002-12.json)\n"
            "2026-11-02T10:26:00Z released 6 (0000000003-13.json)\n"  # an old client's waiter that gave up
            "2026-11-02T10:30:00Z granted 1 to last (pid 14)\n"
            "2026-11-02T10:31:00Z released 1 (0000000004-14.json)\n")
        found = replay.requests(self.dir, sf_config.seconds("2026-11-02T11:00:00Z"))
        self.assertEqual([("first", "history", None), ("old-gate", "log", None),
                          ("(left, unrecorded)", "log", 960.0), ("last", "history", None)],
                         [(r["label"], r["source"], r["patience"]) for r in found])
        gate = found[1]
        self.assertEqual((sf_config.seconds("2026-11-02T10:05:00Z"), 1200), (gate["arrival"], gate["run"]),
                         "granted before the next known arrival: taken to arrive when granted")
        self.assertEqual(sf_config.seconds("2026-11-02T10:10:00Z"), found[2]["arrival"], "half way between its neighbours")
        # The gate's slots free at 10:25, before the waiter gave up: in the replay it runs (ASSUMED_RUN), and past
        # its window it held `last` back.
        replayed = replay.Replay(found, 6, replay.POLICIES[CURRENT]).simulate()
        self.assertEqual([0, 0, 900, 900], [r["granted"] - q["arrival"] for r, q in zip(replayed, found)])

    def test_a_reaped_lease_holds_its_slots_but_is_no_run_of_its_kind(self):
        """A reaped held lease (queue.log only, or a history line) keeps its slots in the replay for as long as it
        did, as the queue saw it; the estimates, the replay's and the seed's, leave its time out as the queue's do."""
        (self.dir / "queue.log").write_text(
            "2026-11-02T08:00:00Z granted 1 to ci (pid 21)\n"
            "2026-11-02T08:05:00Z released 1 (0000000001-21.json)\n"
            "2026-11-02T08:10:00Z granted 1 to ci (pid 22)\n"
            "2026-11-02T08:56:40Z reaped held lease of 1 for ci (pid 22 exited)\n"
            "2026-11-02T09:30:00Z granted 1 to orphan (pid 23)\n"
            "2026-11-02T10:16:40Z reaped held lease of 1 for orphan (pid 23 exited)\n")
        items = [self.history("ci", 1, "10:00:00", "10:00:00", "10:05:00", 24),
                 self.history("ci", 1, "10:20:00", "10:20:00", "11:06:40", 25, outcome="reaped")]
        for item in items:
            item["cmd"] = None
        (self.dir / "history.jsonl").write_text("".join(json.dumps(i) + "\n" for i in items))
        logged = replay.log_requests(self.dir / "queue.log")
        self.assertEqual([("ci", 1), ("ci", None), ("orphan", None)], [(r["label"], r["seq"]) for r in logged])
        self.assertEqual([("ci", None, 300)], replay.seed_runs(self.dir, sf_config.seconds("2026-11-02T09:00:00Z")))
        found = replay.requests(self.dir, sf_config.seconds("2026-11-02T12:00:00Z"))
        self.assertEqual([("orphan", "log", "reaped", 2800), ("ci", "history", "released", 300),
                          ("ci", "history", "reaped", 2800)],
                         [(r["label"], r["source"], r["outcome"], r["run"]) for r in found])
        run = replay.Replay(found, 1, replay.POLICIES[SHIPPED])
        results = run.simulate()
        self.assertEqual(sf_config.seconds("2026-11-02T10:16:40Z"), results[1]["granted"], "the orphan held the slot")
        self.assertEqual([300.0], run.estimates.runs[("label", "ci")])
        self.assertIsNone(run.estimates.of("orphan", None))

    def test_a_family_that_ever_gave_up_waits_at_most_its_longest_give_up(self):
        items = [self.history("lane-1-alone", 6, "10:00:00", None, "10:30:00", 31, outcome="left"),
                 self.history("lane-2-tests", 1, "10:00:00", None, "10:10:00", 32, outcome="left"),
                 self.history("lane-3", 1, "10:00:00", "10:20:00", "10:40:00", 33),
                 self.history("lane-4", 1, "10:00:00", "10:50:00", "11:00:00", 34),  # waited longer: no wait
                 self.history("sid-kit", 1, "10:00:00", "10:00:00", "10:01:00", 35)]
        items.append({**self.history("post-merge-5", 1, "10:00:00", "10:00:00", "10:05:00", 36),
                      "until": "2026-11-02T10:10:00Z"})  # recorded since `until` is in the history
        (self.dir / "history.jsonl").write_text("".join(json.dumps(i) + "\n" for i in items))
        found = replay.family_patience(replay.requests(self.dir, sf_config.seconds("2026-11-02T12:00:00Z")))
        start = sf_config.seconds("2026-11-02T10:00:00Z")
        self.assertEqual({"lane-1-alone": 1800, "lane-2-tests": 600, "lane-3": 1800, "lane-4": None, "sid-kit": None,
                          "post-merge-5": 600},
                         {r["label"]: None if r.get("give_up") is None else r["give_up"] - start for r in found})

    def test_a_long_request_does_not_take_a_slot_the_head_needs_when_leases_end_out_of_order(self):
        # #643 review: x and z were expected to end within ten minutes and y in two hours, so by the expected order
        # one slot was spare for `long`; y ends early and z overruns, and the head would have waited for z.
        (self.dir / "queue.log").write_text("".join(
            f"2026-11-02T{start}Z granted {count} to {label} (pid {pid})\n"
            f"2026-11-02T{end}Z released {count} ({pid:010d}-{pid}.json)\n"
            for pid, (label, count, start, end) in enumerate((("x", 2, "06:00:00", "06:05:00"),
                                                             ("z", 2, "06:00:00", "06:10:00"),
                                                             ("y", 1, "06:00:00", "08:00:00"),
                                                             ("long", 1, "08:00:00", "10:00:00")), start=1)))
        items = [self.history("x", 2, "10:00:00", "10:00:00", "10:05:00", 11),
                 self.history("z", 2, "10:00:00", "10:00:00", "10:50:00", 12),
                 self.history("y", 1, "10:00:00", "10:00:00", "10:15:00", 13),
                 self.history("head", 4, "10:00:30", "10:50:00", "10:51:00", 14),
                 self.history("long", 1, "10:11:00", "10:51:00", "12:51:00", 15)]
        for item in items:
            item["cmd"] = None  # known by label, as the seed runs
        (self.dir / "history.jsonl").write_text("".join(json.dumps(i) + "\n" for i in items))
        found = replay.requests(self.dir, sf_config.seconds("2026-11-02T13:00:00Z"))
        replayed = {q["label"]: r for q, r in zip(found, replay.Replay(
            found, 6, replay.POLICIES[SHIPPED], replay.seed_runs(self.dir, found[0]["arrival"])).simulate())}
        self.assertEqual(sf_config.seconds("2026-11-02T10:15:00Z"), replayed["head"]["granted"], "it starts when y ends")
        self.assertGreaterEqual(replayed["long"]["granted"], replayed["head"]["granted"])


if __name__ == "__main__":
    unittest.main()
