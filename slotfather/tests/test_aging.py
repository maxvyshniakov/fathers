#!/usr/bin/env python3
"""Ageing, the head's reservation, reprioritize and the long-wait signal (2026-11-11: a Safari profile run of the
whole machine asked as batch waited 6 h 14 min while normal and interactive work kept passing it, then twice more
after it was queued again at a higher class). plan() on fixed queues at an injected moment, the queue played forward
by the replay's event simulation with estimates from a seeded history, and the CLI through real processes."""
from __future__ import annotations
import sys as _path_sys
from pathlib import Path as _Path
_path_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "src"))
from slotfather import config as sf_config
from slotfather import schedule as sf_schedule

import json
import os
import unittest
from typing import Any, cast
from unittest import mock

from test_order import M, PlanCase, holder, runs, waiter
from test_slotfather import ROOT, SlotCase
from test_usage import load

replay = cast(Any, load(str(ROOT / "tools" / "replay.py"), "slotfather_replay_for_aging"))
SHIPPED = "ageing + reservations (shipped)"
BEFORE = "classes + yields (#643 shipped)"  # the order installed on 2026-11-11
DAY = ROOT / "tests" / "fixtures" / "replay-2026-11-11"
DAY_NOW = sf_config.seconds("2026-11-11T23:20:56Z")  # when the live queue in the fixture was copied


def classed(entry: dict[str, Any], cls: str, **extra: Any) -> dict[str, Any]:
    return {**entry, "priority": cls, **extra}


class AgingTests(PlanCase):
    """A waiter moves up one class per step it waits through, from when it queued; its arrival stays."""

    def served(self, cls: str, waited: float) -> str:
        return sf_schedule.effective_priority(classed(waiter("w", 1, -waited), cls), self.settings, M)

    def test_batch_ages_to_normal_then_interactive_at_each_threshold(self):
        self.assertEqual(["batch", "normal", "normal", "interactive"],
                         [self.served("batch", w) for w in (1799, 1800, 1800 + 3599, 1800 + 3600)])
        self.assertEqual(["normal", "interactive"], [self.served("normal", w) for w in (3599, 3600)])
        self.assertEqual("interactive", self.served("interactive", 99999))

    def test_a_step_of_zero_stops_ageing_there(self):
        self.settings["aging_normal"] = 0.0
        self.assertEqual("normal", self.served("batch", 99999))
        self.settings["aging_batch"] = 0.0
        self.assertEqual("batch", self.served("batch", 99999))
        self.assertEqual("normal", self.served("normal", 99999))

    def test_the_thresholds_are_settings(self):
        self.settings.update(aging_batch=600.0, aging_normal=600.0)
        self.assertEqual(["batch", "normal", "interactive"], [self.served("batch", w) for w in (599, 600, 1200)])

    def test_an_aged_request_keeps_its_arrival_and_only_moves_up(self):
        entry = classed(waiter("w", 1, 0), "batch")
        ranks = [sf_schedule.rank_time(entry, self.settings, M + t) for t in range(0, 8000, 60)]
        self.assertEqual(sorted(ranks, reverse=True), ranks, "its rank never moves later")
        self.assertEqual([M + 1800, M, M - 1800], sorted(set(ranks), reverse=True),
                         "batch, then normal at its arrival, then interactive: its arrival less the offset")

    def test_a_held_lease_and_the_window_order_keep_their_class(self):
        self.assertEqual("batch", sf_schedule.effective_priority(classed(holder("h", 1, -9999), "batch"),
                                                                 self.settings, M))
        queue = [classed(waiter("old", 1, -1000), "batch"), waiter("new", 1, -10)]  # in arrival order
        self.assertEqual(["old"], self.started(queue, [holder("one", 5, -60)], "window"), "arrival order alone")
        self.assertEqual(["new"], self.started(queue, [holder("one", 5, -60)]), "batch, not aged yet")

    def test_a_lease_back_after_a_yield_ages_from_when_it_yielded(self):
        back = classed(waiter("gate", 1, -9000), "batch", yielded_at=M - 100, yields=1)
        self.assertEqual("batch", sf_schedule.effective_priority(back, self.settings, M))

    def test_an_aged_batch_request_no_longer_defers_and_becomes_the_head(self):
        held = [holder("one", 1, -600)]
        measure = classed(waiter("measure", 6, -1799), "batch", all=True)
        check = waiter("check", 1, -10)
        found = sf_schedule.plan([measure, check], held, 6, M, None, runs(one=3600, check=7200), settings=self.settings)
        self.assertEqual(["check"], [e["label"] for e in found.grants], "batch: it lets the check go first")
        measure = classed(waiter("measure", 6, -1800), "batch", all=True)
        found = sf_schedule.plan([measure, check], held, 6, M, None, runs(one=3600, check=7200), settings=self.settings)
        self.assertEqual(([], "measure"), ([e["label"] for e in found.grants], found.head and found.head["label"]))

    def test_the_environment_can_only_age_less(self):
        with mock.patch.dict(os.environ, {"SLOTFATHER_AGING_BATCH": "600", "SLOTFATHER_AGING_NORMAL": "7200"}):
            found = sf_schedule.order_settings()
        self.assertEqual((1800.0, 7200.0), (found["aging_batch"], found["aging_normal"]))
        with mock.patch.dict(os.environ, {"SLOTFATHER_AGING_BATCH": "0"}):
            self.assertEqual(0.0, sf_schedule.order_settings()["aging_batch"], "0: never, the tightest")
        with mock.patch.dict(os.environ, {"SLOTFATHER_WARN_WAIT": "60"}):
            self.assertEqual(60.0, sf_schedule.order_settings()["warn_wait"], "its own warnings: either way")

    def test_durations_read_as_seconds_or_with_a_unit(self):
        self.assertEqual([90.0, 90.0, 1800.0, 5400.0, 5400.0],
                         [sf_config.parse_duration(t) for t in ("90", "90s", "30m", "1.5h", "1h30m")])
        for bad in ("", "-1", "1d", "m"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                sf_config.parse_duration(bad)


class ReservationTests(PlanCase):
    """Once a waiter is the head, a later request starts before it only if `safety` times its expected run ends by
    the head's reserved start, or it takes slots the head will not need; the reserved start only moves earlier."""

    def setUp(self):
        super().setUp()
        self.held = [holder("one", 1, -600)]  # 3000 s of its expected 3600 left: the head can start at M + 3000

    def plan(self, waiters: list[dict[str, Any]], moment: float = M, held: list[dict[str, Any]] | None = None,
             **estimates: float) -> sf_schedule.Plan:
        return sf_schedule.plan(waiters, self.held if held is None else held, 6, moment, None,
                                runs(**{"one": 3600.0, **estimates}), settings=self.settings)

    def test_a_new_head_is_promised_the_start_the_held_leases_give(self):
        found = self.plan([waiter("gate", 6, -700)])
        self.assertEqual(("gate", M + 3000, M + 3000), (found.head and found.head["label"], found.start, found.reserved))

    def test_backfill_admits_short_work_that_ends_in_time_and_refuses_work_that_would_delay_the_head(self):
        gate = dict(waiter("gate", 6, -700), reserved=M + 3000)
        found = self.plan([gate, waiter("short", 1, -10), waiter("long", 1, -5)], short=1000, long=1001)
        self.assertEqual(["short"], [e["label"] for e in found.grants], "3 x 1000 s ends at the reserved start")

    def test_the_promised_start_never_moves_later_when_held_leases_overrun(self):
        # `one` was expected to end at M + 3000; an hour later it still runs, expected to run on for half of it.
        gate = dict(waiter("gate", 6, -700), reserved=M + 3000)
        later = M + 3600
        found = self.plan([gate, waiter("short", 1, -10)], later, short=10)
        self.assertGreater(found.start or 0.0, M + 3000, "the projection moves on with the overrun")
        self.assertEqual(M + 3000, found.reserved, "the promise does not")
        self.assertEqual([], [e["label"] for e in found.grants], "past it, nothing that would end after it passes")

    def test_the_promised_start_moves_earlier_when_a_lease_ends_early(self):
        gate = dict(waiter("gate", 6, -700), reserved=M + 3000)
        found = self.plan([gate, waiter("short", 1, -10)], held=[holder("two", 3, -60)], two=600, short=60)
        self.assertEqual(M + 540, found.reserved)

    def test_grant_keeps_the_promise_on_the_head_and_drops_it_from_the_others(self):
        gate, small = waiter("gate", 6, -700), dict(waiter("small", 1, -900), reserved=M - 5)  # small: once a head
        found = self.plan([gate, small])
        self.assertEqual((["small"], "gate"), ([e["label"] for e in found.grants], found.head and found.head["label"]))
        changed = sf_schedule.reservations([gate, small], found)
        # small runs the fallback (3600 s) in one of the six slots gate needs
        self.assertEqual((2, M + 3600, None), (len(changed), gate.get("reserved"), small.get("reserved")))
        self.assertEqual([], sf_schedule.reservations([gate], self.plan([gate])), "unchanged: nothing to write")
        found = self.plan([gate, waiter("quick", 1, -5)], held=[holder("two", 1, -10)], two=100, quick=10)
        sf_schedule.reservations([gate], found)
        self.assertEqual(M + 90, gate.get("reserved"), "two ends at M + 90: earlier, worked out once one fits")

    def test_a_later_interactive_request_passes_a_head_only_as_backfill(self):
        # Before: an interactive request ranks ahead of a normal one that came less than 1800 s before it, so it
        # started at once in front of the head and moved the head's start later.
        gate = dict(waiter("gate", 5, -700), reserved=M + 3000)
        test = classed(waiter("test", 1, -10), "interactive")
        held = [holder("one", 1, -600), holder("two", 1, -10)]
        self.assertEqual([], [e["label"] for e in self.plan([gate, test], held=held, test=1001).grants])
        self.assertEqual(["test"], [e["label"] for e in self.plan([gate, test], held=held, test=60).grants])
        self.assertEqual(["gate", "test"], [e["label"] for e in sf_schedule.ranked([test, gate], self.settings, M)])

    def test_a_request_that_came_before_the_head_and_ranks_ahead_of_it_keeps_its_place(self):
        gate = dict(waiter("gate", 5, -700), reserved=M + 3000)
        early = classed(waiter("early", 1, -900), "interactive")
        self.assertEqual(["early", "gate"], [e["label"] for e in sf_schedule.ranked([gate, early], self.settings, M)])

    def test_slots_the_head_will_not_need_still_take_long_work(self):
        held = [holder("a", 2, -10), holder("b", 2, -10)]
        head = dict(waiter("head", 3, -700), reserved=M + 590)
        found = sf_schedule.plan([head, waiter("long", 1, -10)], held, 6, M, None, runs(a=600, b=6000, long=99999),
                                 settings=self.settings)
        self.assertEqual(["long"], [e["label"] for e in found.grants])

    def test_with_slots_held_in_an_earlier_queue_the_promise_stays_and_nothing_passes(self):
        gate = dict(waiter("gate", 6, -700), reserved=M + 3000)
        found = sf_schedule.plan([gate, waiter("short", 1, -10)], [], 6, M, None, runs(short=1), other=2,
                                 settings=self.settings)
        self.assertEqual(([], M + 3000), (found.grants, found.reserved))

    def test_a_limit_still_holds_everyone_back_once_the_head_has_waited_it(self):
        self.settings["limit"] = 2700.0
        gate = dict(waiter("gate", 6, -2700), reserved=M + 3000)
        self.assertEqual([], [e["label"] for e in self.plan([gate, waiter("short", 1, -10)], short=1).grants])


class StreamTests(unittest.TestCase):
    """A batch `-n all` measurement among interactive checks that keep coming, one a minute, alternately short (20 s)
    and long (300 s), each check's run known from the history. Before, it waited for the stream to stop."""

    def requests(self, count: int = 240) -> list[dict[str, Any]]:
        start = sf_config.seconds("2026-11-02T10:00:00Z") or 0.0
        found: list[dict[str, Any]] = [
            {"label": "lane-1", "cmd": "make", "count": 1, "arrival": 0.0, "run": 600.0, "priority": "normal"},
            {"label": "safari-profile", "cmd": "measure.sh", "count": 6, "arrival": 10.0, "run": 600.0,
             "priority": "batch"}]
        for n in range(count):
            kind = ("quick", "node --test quick.mjs", 20.0) if n % 2 else ("slow", "node --test slow.mjs", 300.0)
            found.append({"label": kind[0], "cmd": kind[1], "count": 1, "arrival": 20.0 + 60 * n, "run": kind[2],
                          "priority": "interactive"})
        for number, request in enumerate(found):
            request.update(id=number, patience=None, waited=None, source="history", arrival=start + request["arrival"])
        return found

    def play(self, policy: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[tuple[float, float]]]:
        found = self.requests()
        seed = [("quick", "node --test quick.mjs", 20.0), ("slow", "node --test slow.mjs", 300.0),
                ("lane-1", "make", 600.0), ("safari-profile", "measure.sh", 600.0)]
        promised: list[tuple[float, float]] = []
        decide = replay.Replay.decide

        def watching(self: Any, waiting: list[dict[str, Any]], held: list[dict[str, Any]],
                     moment: float) -> list[dict[str, Any]]:
            grants: list[dict[str, Any]] = decide(self, waiting, held, moment)
            mine = next((e for e in waiting if e.get("id") == 1), None)  # its entry as the queue keeps it
            kept = None if mine is None else sf_schedule.reservation(mine)
            if kept is not None:
                promised.append((moment, kept))
            return grants

        with mock.patch.object(replay.Replay, "decide", watching):
            results = replay.Replay(found, 6, replay.POLICIES[policy], seed).simulate()
        return found, results, promised

    def aged(self, found: list[dict[str, Any]], promised: list[tuple[float, float]]) -> list[tuple[float, float]]:
        """The promises after it aged: before, as batch, it is the head only between checks that wait."""
        return [(moment, value) for moment, value in promised if moment >= found[1]["arrival"] + 1800]

    def test_it_starts_within_its_ageing_its_window_and_one_long_check(self):
        found, results, _ = self.play(SHIPPED)
        waited = results[1]["granted"] - found[1]["arrival"]
        self.assertLessEqual(waited, 1800 + 300 + 2 * replay.POLL, "ageing, then the checks it waited for")
        _, before, _ = self.play(BEFORE)
        self.assertGreater(before[1]["granted"] - found[1]["arrival"], 4 * 3600, "before: until the checks stop")

    def test_short_checks_keep_passing_it_and_long_ones_wait_for_it(self):
        found, results, promised = self.play(SHIPPED)
        became, started = self.aged(found, promised)[0][0], results[1]["granted"]
        passed = {found[n]["label"] for n, r in enumerate(results)
                  if n > 1 and r["granted"] is not None and became < r["granted"] < started}
        self.assertEqual({"quick"}, passed, "only checks that end before its reserved start pass it")

    def test_its_promised_start_never_moves_later(self):
        found, _, promised = self.play(SHIPPED)
        values = [value for _, value in self.aged(found, promised)]
        self.assertTrue(values)
        self.assertEqual(sorted(values, reverse=True), values)


class DayReplayTests(unittest.TestCase):
    """The queue of 2026-11-11 12:00 to 23:20 UTC (tests/fixtures/replay-2026-11-11: commands cut to their kind,
    no directories or metadata; queue.log from 2026-11-10 12:00 for the estimates): the Safari profile run asked for
    all eight slots as batch at 16:31:43, waited 6 h 14 min and was given up; queued again as normal and then as
    interactive it lost its place each time."""

    @classmethod
    def setUpClass(cls):
        cls.found = replay.requests(DAY, DAY_NOW)
        cls.safari = next(r["id"] for r in cls.found if r["label"] == "safari-profile-6146c4a" and r["count"] == 8
                          and r["priority"] == "batch" and r["arrival"] == sf_config.seconds("2026-11-11T16:31:43Z"))
        whole = sf_schedule.Estimates((r["label"], r["cmd"], r["run"]) for r in cls.found
                                      if r["run"] is not None and r.get("outcome") not in sf_config.UNFINISHED)
        seed = replay.seed_runs(DAY, cls.found[0]["arrival"])
        cls.results = {name: replay.Replay(cls.found, 8, replay.POLICIES[name], seed, {"estimates": whole}).simulate()
                       for name in (BEFORE, SHIPPED)}

    def test_the_installed_order_replays_the_six_hour_wait(self):
        before = self.results[BEFORE][self.safari]
        self.assertIsNone(before["granted"])
        self.assertEqual(sf_config.seconds("2026-11-11T22:45:50Z"), before["left"])

    def test_the_new_rules_start_it_after_an_hour_and_23_minutes(self):
        # Aged to normal at 17:01:43, it was behind an interactive 8-slot measurement that had been the head since
        # 16:55 and started at 17:48; it started at 17:54:54, once the checks that measurement waited for ended.
        mine = self.results[SHIPPED][self.safari]
        self.assertAlmostEqual(sf_config.seconds("2026-11-11T17:54:54Z"), mine["granted"], delta=replay.POLL)


class SharedReservationTests(PlanCase):
    """Every client keeps the head's reservation by the shared settings, whatever its own environment tightens."""

    def test_a_client_whose_environment_tightens_the_order_keeps_the_shared_head_and_its_reservation(self):
        gate = dict(waiter("gate", 6, -700), reserved=M + 3000)
        held, estimate = [holder("one", 1, -600)], runs(one=3600)
        mine = dict(self.settings, order="window")  # SLOTFATHER_QUEUE=window in this client's environment
        found = sf_schedule.plan([gate], held, 6, M, estimate=estimate, settings=mine)
        self.assertIsNone(found.head)
        keeper = sf_schedule.shared_plan(found, [gate], held, 6, M, estimate, 0, mine, self.settings)
        self.assertEqual([], sf_schedule.reservations([gate], keeper))
        self.assertEqual(M + 3000, gate["reserved"])
        same = sf_schedule.plan([gate], held, 6, M, estimate=estimate, settings=self.settings)
        self.assertIs(same, sf_schedule.shared_plan(same, [gate], held, 6, M, estimate, 0, self.settings,
                                                    dict(self.settings)))

    def test_a_shared_window_order_keeps_no_reservation(self):
        gate = dict(waiter("gate", 6, -700), reserved=M + 3000)
        shared = dict(self.settings, order="window")
        keeper = sf_schedule.shared_plan(sf_schedule.Plan(), [gate], [], 6, M, runs(), 0, self.settings, shared)
        sf_schedule.reservations([gate], keeper)
        self.assertNotIn("reserved", gate)

    def test_a_reprioritized_request_ages_from_its_new_class(self):
        moved = classed(waiter("w", 1, -7200), "batch", reprioritized="interactive", reprioritized_at=M - 60)
        self.assertEqual("batch", sf_schedule.effective_priority(moved, self.settings, M), "moved down: it stays")
        self.assertEqual(M - 7200 + 1800, sf_schedule.rank_time(moved, self.settings, M), "its arrival stays")


class ForecastTests(PlanCase):
    def test_each_waiter_gets_the_moment_the_queue_is_expected_to_start_it(self):
        held = [holder("one", 4, -600), holder("two", 2, -100)]  # one ends at M + 3000, two at M + 500
        queue = [waiter("gate", 6, -700), waiter("pair", 2, -20), waiter("after", 1, -10)]
        starts = sf_schedule.forecast(queue, held, 6, M, runs(one=3600, two=600, pair=400, after=100),
                                      settings=self.settings)
        self.assertEqual({"gate": M + 3000, "pair": M + 500, "after": M + 900},
                         {e["label"]: starts.get(id(e)) for e in queue}, "after waits for pair's two slots")

    def test_a_batch_request_is_forecast_ahead_of_later_work_once_it_has_aged(self):
        held = [holder("one", 5, -60)]  # ends at M + 7140
        queue = [classed(waiter("measure", 6, -10), "batch", all=True), waiter("check", 2, -5)]
        estimate = runs(one=7200, measure=600, check=600)
        starts = sf_schedule.forecast(queue, held, 6, M, estimate, settings=self.settings)
        self.assertEqual((M + 7140, M + 7740), (starts[id(queue[0])], starts[id(queue[1])]))
        self.settings["aging_batch"] = 0.0
        starts = sf_schedule.forecast(queue, held, 6, M, estimate, settings=self.settings)
        self.assertEqual((M + 7140 + 600, M + 7140), (starts[id(queue[0])], starts[id(queue[1])]), "never aged")


class SafetyTests(SlotCase):
    def test_only_the_owner_may_reprioritize_and_a_failing_forecast_breaks_nothing(self):
        from slotfather import leases, platform, query
        pid, start = self.holder()
        cast(Any, self).queue_entry(self.state, pid, start, 1, "theirs", state="waiting")
        with mock.patch.object(platform, "process_uid", return_value=os.geteuid() + 1), \
                mock.patch.dict(os.environ, {"SLOTFATHER_DIR": str(self.state), "SLOTFATHER_LEGACY_DIRS": ""}):
            with self.assertRaisesRegex(sf_config.SlotError, "only its owner"):
                leases.reprioritize(str(pid), "interactive", self.state)
            self.assertEqual("normal", sf_config.priority_of(json.loads(next((self.state / "queue").glob("*.json"))
                                                                         .read_text())))
            with mock.patch.object(query, "expected", side_effect=ValueError("bad estimate")):
                lines = query.status_lines(self.state)
                self.assertIn("waiting: 1 slot(s) theirs", lines[0])
                self.assertNotIn("est-start", lines[0])
                self.assertIsNone(query.long_wait(self.state, next((self.state / "queue").glob("*.json"))))


class CommandTests(SlotCase):
    def seed(self, **estimates: float) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        with open(self.state / "history.jsonl", "a") as handle:
            for label, run in estimates.items():
                handle.write(json.dumps({"v": 2, "label": label, "cmd": f"sh -c {label}", "count": 1, "granted": "x",
                                         "outcome": "released", "ran": run}) + "\n")

    def entry(self, label: str) -> dict[str, Any]:
        for path in sorted((self.state / "queue").glob("*.json")):
            found = json.loads(path.read_text())
            if found.get("label") == label:
                return found
        self.fail(f"no entry for {label}")

    def test_reprioritize_keeps_the_arrival_and_records_the_change(self):
        one = self.request("one", 4)
        self.started("one")
        gate = self.request("gate", 4, options=("--priority", "batch"))
        self.wait_for(lambda: "gate" in self.status(), "gate to wait")
        since = self.entry("gate")["since"]
        moved = self.tool("reprioritize", "gate", "interactive")
        self.assertEqual(0, moved.returncode, moved.stderr)
        self.assertIn("gate (pid", moved.stdout)
        entry = self.entry("gate")
        self.assertEqual(("interactive", "batch", since), (entry["priority"], entry["reprioritized"], entry["since"]))
        self.assertRegex((self.state / "queue.log").read_text(), r"reprioritized gate \(pid \d+\) from batch to interactive")
        status = json.loads(self.tool("status", "--json").stdout)
        mine = next(e for e in status["entries"] if e["label"] == "gate")
        self.assertEqual(("interactive", "batch", since), (mine["priority"], mine["reprioritized"], mine["since"]))
        for bad in (("gate",), ("nobody", "normal"), ("gate", "urgent")):
            with self.subTest(bad=bad):
                self.assertEqual(2, self.tool("reprioritize", *bad).returncode)
        self.release("one")
        self.finish(one)
        self.started("gate")
        self.release("gate")
        self.finish(gate)
        line = [json.loads(text) for text in (self.state / "history.jsonl").read_text().splitlines()][-1]
        self.assertEqual(("gate", "interactive", "batch"), (line["label"], line["priority"], line["reprioritized"]))

    def test_a_long_wait_is_said_on_stderr_in_the_log_and_in_status(self):
        one = self.request("one", 4)
        self.started("one")
        self.env["SLOTFATHER_WARN_WAIT"] = "1"
        gate = self.request("gate", 4, options=("--priority", "batch"))
        self.wait_for(lambda: "waiting long: gate" in (self.state / "queue.log").read_text(), "the long-wait line")
        status = self.status()
        self.assertRegex(status, r"waiting: 4 slot\(s\) gate .*priority=batch .*est-start=\S+ long-wait=\S+")
        entry = next(e for e in json.loads(self.tool("status", "--json").stdout)["entries"] if e["label"] == "gate")
        self.assertTrue(entry["long_wait"])
        self.assertEqual(("batch", False), (entry["effective_priority"], entry["aged"]))
        self.release("one")
        self.finish(one)
        self.started("gate")
        self.release("gate")
        output = self.finish(gate)
        self.assertRegex(output, r"slot: gate \(pid \d+, batch\) waiting \S+ behind 1 running lease: est\. start "
                                 r"\d\d:\d\d; reprioritize with: slotfather reprioritize \d+ normal")

    def test_order_takes_the_ageing_and_warn_settings_as_durations(self):
        saved = self.tool("order", "--aging-batch", "10m", "--aging-normal", "1h", "--warn-wait", "45m")
        self.assertEqual(0, saved.returncode, saved.stderr)
        self.assertIn("batch ages after 600 s, normal after 3600 s; warn after 2700 s waiting; memory pressure warning admits one "
                      "1-slot lease at a time (saved)", saved.stdout)
        self.assertEqual({"aging_batch": 600.0, "warn_wait": 2700.0},
                         {k: v for k, v in json.loads((self.state / "order.json").read_text()).items()
                          if k in ("aging_batch", "aging_normal", "warn_wait")},
                         "a default is not written: an older slotfather refuses keys it does not know")
        self.assertIn("aging_batch, warn_wait", saved.stderr)
        self.assertEqual(2, self.tool("order", "--aging-batch", "soon").returncode)
        self.assertIn("batch ages never", self.tool("order", "--aging-batch", "0").stdout)


class EventsTests(unittest.TestCase):
    def test_events_show_a_priority_change_and_a_long_wait(self):
        import tempfile
        from slotfather import dashboard
        with tempfile.TemporaryDirectory() as tmp:
            (_Path(tmp) / "queue.log").write_text(
                "2026-11-11T22:55:54Z reprioritized safari-profile (pid 49535) from batch to interactive\n"
                "2026-11-11T23:25:54Z waiting long: safari-profile (pid 49535, interactive) waiting 30m00s behind 8 "
                "running leases: est. start 01:40\n")
            events: list[dict[str, Any]] = cast(Any, dashboard).queue_events(_Path(tmp))
            dashboard.describe_events(events, _Path(tmp), [])
        self.assertEqual([("priority", "safari-profile", 49535, 0, "batch → interactive"),
                          ("waiting", "safari-profile", 49535, 0, "30m00s, est. 01:40")],
                         [(e["verb"], e["label"], e["pid"], e["delta"], e["detail"]) for e in events])


if __name__ == "__main__":
    unittest.main()
