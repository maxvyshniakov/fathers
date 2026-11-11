"""Idle leases (#12): one definition of idle, from the holder's samples of its command's process tree, used by the
bar colour, the QUEUE marker, LEASE DETAILS, `status` and the yield ask."""
from __future__ import annotations

import sys as _path_sys
from pathlib import Path as _Path
_path_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "src"))

import datetime
import importlib
import json
import os
import time
import unittest
from typing import Any
from unittest import mock

from slotfather import config as sf_config
from slotfather import idle as sf_idle
from slotfather import schedule as sf_schedule

from test_order import M, holder, runs, waiter
from test_slotfather import SlotCase


def tree(*cpu: float) -> dict[tuple[int, str], float]:
    """A process tree sample: (pid, start) -> own CPU seconds, pids 100, 101, ..."""
    return {(100 + n, "Mon Nov 11 10:00:00 2026"): seconds for n, seconds in enumerate(cpu)}


class TrackerTests(unittest.TestCase):
    """The holder's tracker: since when its tree has used less than the threshold, sample after sample."""

    def test_low_cpu_starts_an_idle_run_at_the_first_low_interval(self) -> None:
        tracker = sf_idle.Tracker()
        self.assertIsNone(tracker.observe(0, tree(5.0, 1.0), 0.1), "one sample says nothing about a rate")
        self.assertEqual(0, tracker.observe(10, tree(5.5, 1.0), 0.1), "0.05 cores is below 0.1: low since 0")
        self.assertEqual(0, tracker.observe(20, tree(5.6, 1.2), 0.1))

    def test_a_busy_interval_or_a_process_starting_or_ending_breaks_the_run(self) -> None:
        tracker = sf_idle.Tracker()
        tracker.observe(0, tree(5.0), 0.1)
        tracker.observe(10, tree(5.0), 0.1)
        self.assertIsNone(tracker.observe(20, tree(7.0), 0.1), "0.2 cores is busy")
        self.assertEqual(20, tracker.observe(30, tree(7.0), 0.1), "low again from the end of the busy interval")
        self.assertIsNone(tracker.observe(40, tree(7.0, 0.0), 0.1), "a new process is activity")
        tracker.observe(50, tree(7.0, 0.0), 0.1)
        self.assertIsNone(tracker.observe(60, tree(7.0), 0.1), "so is one that ended between samples")

    def test_a_threshold_of_zero_never_finds_idle(self) -> None:
        tracker = sf_idle.Tracker()
        tracker.observe(0, tree(5.0), 0.0)
        self.assertIsNone(tracker.observe(10, tree(5.0), 0.0))


class ReaderTests(unittest.TestCase):
    """What every reader makes of the holder's record: idle once the low run has lasted `idle_after`."""

    def setUp(self) -> None:
        import tempfile
        import shutil
        self.queue = _Path(tempfile.mkdtemp(prefix="slotfather-idle-")) / "queue"
        self.queue.mkdir()
        self.addCleanup(shutil.rmtree, self.queue.parent, ignore_errors=True)
        self.entry = self.queue / "0000000001-123.json"
        self.settings = dict(sf_config.ORDER_DEFAULTS)

    def test_idle_after_the_minimum_duration_and_not_before(self) -> None:
        sf_idle.record(self.entry, since=M - 299, rate=0.0, moment=M)
        self.assertIsNone(sf_idle.idle_since(self.entry, self.settings, M), "a 299 s pause is no idle lease")
        sf_idle.record(self.entry, since=M - 300, rate=0.0, moment=M)
        self.assertEqual(M - 300, sf_idle.idle_since(self.entry, self.settings, M))
        self.settings["idle_after"] = 600
        self.assertIsNone(sf_idle.idle_since(self.entry, self.settings, M), "the threshold is the queue's setting")

    def test_a_record_its_holder_stopped_writing_says_nothing(self) -> None:
        sf_idle.record(self.entry, since=M - 3600, rate=0.0, moment=M - 120)
        self.assertIsNone(sf_idle.idle_since(self.entry, self.settings, M), "stale: inspection unavailable")
        self.assertIsNone(sf_idle.idle_since(self.queue / "0000000002-124.json", self.settings, M), "no record")
        sf_idle.path(self.entry).write_text("{not json")
        self.assertIsNone(sf_idle.idle_since(self.entry, self.settings, M))
        sf_idle.record(self.entry, since=None, rate=0.4, moment=M)
        self.assertIsNone(sf_idle.idle_since(self.entry, self.settings, M), "busy")


class AskTests(unittest.TestCase):
    """An idle yieldable lease is asked to yield while a request waits that its slots would start."""

    def setUp(self) -> None:
        self.settings: dict[str, Any] = dict(sf_config.ORDER_DEFAULTS)
        patcher = mock.patch.dict(os.environ, {"SLOTFATHER_OVERTAKE_WINDOW": "600"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def asks(self, waiters: list[dict[str, Any]], held: list[dict[str, Any]], idle: list[dict[str, Any]],
             slots: int = 2) -> list[tuple[str, str]]:
        found = sf_schedule.idle_asks(waiters, held, idle, slots, M, runs(), 0, self.settings)
        return [(h["label"], w["label"]) for h, w in found]

    def test_only_while_someone_waits_and_only_yieldable_leases(self) -> None:
        lazy = dict(holder("lazy", 1, -3600), yieldable=True)
        stuck = holder("stuck", 1, -3600)
        queue = [waiter("next", 1, -60)]
        self.assertEqual([("lazy", "next")], self.asks(queue, [lazy, stuck], [lazy, stuck]))
        self.assertEqual([], self.asks([], [lazy, stuck], [lazy, stuck]), "nobody waits: nobody is asked")
        self.assertEqual([], self.asks(queue, [lazy, stuck], [stuck]), "a lease that cannot yield is only marked")

    def test_only_when_its_slots_would_start_the_waiter_and_not_past_the_yield_limit(self) -> None:
        lazy = dict(holder("lazy", 1, -3600), yieldable=True)
        self.assertEqual([], self.asks([waiter("big", 3, -60)], [lazy, holder("busy", 1, -60)], [lazy]),
                         "its one slot would not start a request for three")
        tired = dict(lazy, yields=sf_config.YIELDS_MAX)
        self.assertEqual([], self.asks([waiter("next", 1, -60)], [tired, holder("busy", 1, -60)], [tired]))


class MarkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tool = importlib.reload(importlib.import_module("slotfather.dashboard"))
        self.now = datetime.datetime.now(datetime.timezone.utc)

    def model(self) -> dict[str, Any]:
        now, old = self.now, self.now - datetime.timedelta(minutes=30)
        holders = [dict(label="busy", kind="held", count=1, pid=10, style="h0", cpu=150, since=now, granted=now,
                        rss=1024, procs=1, queue="", cmd="work", tree=[]),
                   dict(label="idle", kind="held", count=1, pid=11, style="h1", cpu=0, since=old, granted=old,
                        idle_since=old, rss=1024, procs=1, queue="", cmd="work", tree=[]),
                   dict(label="asked", kind="held", count=1, pid=12, style="h2", cpu=0, since=old, granted=old,
                        idle_since=old, asked="next", asked_at=now, rss=1024, procs=1, queue="", cmd="work", tree=[])]
        waiters = [dict(label="next", kind="waiting", count=1, pid=13, style="text", cpu=0, since=now, granted=None)]
        return {"holders": holders, "waiters": waiters, "slots": 4, "cores": 12}

    def test_asked_to_yield_wins_over_idle(self) -> None:
        markers = [self.tool.queue_marker(item) for item in self.model()["holders"] + self.model()["waiters"]]
        self.assertEqual([("", "text"), ("◌", "dim"), ("↓", "h2"), ("", "text")], markers)

    def test_the_marker_column_sits_beside_the_cursor_and_falls_back_to_ascii(self) -> None:
        for width in (120, 60, 40):
            with self.subTest(width=width):
                canvas = self.tool.Canvas(width, 10)
                self.tool.draw_requests(canvas, 0, 0, 10, width, self.model(), 0, self.now)
                lines = canvas.render(False).splitlines()
                idle = next(line for line in lines if " idle" in line)
                asked = next(line for line in lines if " asked" in line)
                self.assertIn("│  ◌ idle", idle)
                self.assertIn("│  ↓ asked", asked)
                self.assertIn("│ ▶  busy", next(line for line in lines if "busy" in line))
                for line in lines:
                    self.assertLessEqual(len(line.rstrip()), width)
                ascii_lines = canvas.lines(False, ascii_only=True)
                self.assertTrue(all(line.isascii() for line in ascii_lines))
                self.assertTrue(any("o idle" in line for line in ascii_lines))
                self.assertTrue(any("v asked" in line for line in ascii_lines))

    def test_the_bar_warns_by_the_same_idle(self) -> None:
        model = self.model()
        styles = [self.tool.queue_bar_style(item, model, self.now) for item in model["holders"]]
        self.assertEqual(["h0", "warn", "warn"], styles)
        long_ago = dict(model["holders"][0], cpu=0, granted=self.now - datetime.timedelta(hours=1))
        self.assertEqual("h0", self.tool.queue_bar_style(long_ago, model, self.now),
                         "no own idle rule any more: only the holder's record says idle")

    def test_lease_details_say_since_when_and_when_it_was_asked(self) -> None:
        model = dict(self.model(), free=0, used=3, memory_block=None, admission=0, start=None, limit=600.0,
                     order="backfill")
        canvas = self.tool.Canvas(90, 24)
        self.tool.draw_detail(canvas, 0, 0, 24, 90, model, 2, None, self.now)
        text = canvas.render(False)
        self.assertRegex(text, r"idle +30m since \d\d:\d\d")
        self.assertRegex(text, r"yield +asked at \d\d:\d\d for next")


class QueueTests(SlotCase):
    """Through real processes: the holder's record, status, and the ask a waiter's look writes."""

    def test_status_shows_idle_since_and_a_waiter_asks_an_idle_yieldable_lease(self) -> None:
        self.env.update(SLOTFATHER_SAMPLE_MAX="0.2")
        self.assertEqual(0, self.tool("order", "--idle-after", "1").returncode)
        lazy = self.request("lazy", "all", "sleep", "60", options=("--yieldable",))
        self.wait_for(lambda: "held: 4 slot(s) lazy" in self.status(), "the lazy lease to start")
        entry = next((self.state / "queue").glob("*.json"))
        self.wait_for(lambda: " idle=" in self.status(), "the quiet command to turn idle")
        self.assertRegex(self.status(), r"held: 4 slot\(s\) lazy .* idle=\d+s")
        found = json.loads(self.tool("status", "--json").stdout)
        self.assertLess(found["entries"][0]["idle_since"], time.time() - 1)
        self.assertFalse(entry.with_suffix(".yield").exists(), "nobody waits: nobody is asked")
        waiter = self.request("next", 1)
        self.wait_for(lambda: entry.with_suffix(".yield").exists(), "the waiter's look to ask the idle lease")
        self.assertEqual("next", json.loads(entry.with_suffix(".yield").read_text())["label"])
        lazy.terminate()
        lazy.wait()
        self.started("next")
        self.assertFalse(sf_idle.path(entry).exists(), "the record leaves with its lease")
        self.release("next")
        self.finish(waiter)

    def test_a_lease_that_cannot_yield_is_only_marked(self) -> None:
        self.env.update(SLOTFATHER_SAMPLE_MAX="0.2")
        self.assertEqual(0, self.tool("order", "--idle-after", "1").returncode)
        stuck = self.request("stuck", "all", "sleep", "60")
        self.wait_for(lambda: " idle=" in self.status(), "the quiet command to turn idle")
        waiter = self.request("next", 1)
        self.wait_for(lambda: "waiting: 1 slot(s) next" in self.status(), "the waiter to queue")
        time.sleep(1)  # several of the waiter's looks
        self.assertEqual([], list((self.state / "queue").glob("*.yield")))
        stuck.terminate()
        stuck.wait()
        self.started("next")
        self.release("next")
        self.finish(waiter)

    def test_the_holder_records_its_tree_and_a_quiet_command_turns_idle(self) -> None:
        self.env.update(SLOTFATHER_SAMPLE_MAX="0.2")
        quiet = self.request("quiet", 1, "sleep", "30")
        self.wait_for(lambda: list((self.state / "queue").glob("*.idle")), "the holder's first record")
        record = next((self.state / "queue").glob("*.idle"))
        self.wait_for(lambda: json.loads(record.read_text()).get("since") is not None, "a low interval")
        self.assertLess(json.loads(record.read_text())["cpu"], 0.1)
        quiet.terminate()
        quiet.wait()

    def test_order_saves_the_idle_thresholds(self) -> None:
        result = self.tool("order", "--idle-cpu", "0.2", "--idle-after", "600")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("idle below 0.2 cores for 600 s", result.stdout)
        saved = json.loads((self.state / "order.json").read_text())
        self.assertEqual((0.2, 600.0), (saved["idle_cpu"], saved["idle_after"]))


if __name__ == "__main__":
    unittest.main()
