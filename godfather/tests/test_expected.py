"""Expected durations, outliers and trends from Slotfather (fathers#106, fathers#108), and entries known by their
`lease_id` (fathers#101): the adapter's join and fallbacks, the run events read from the log, QUEUE's
`12m / ~18m`, DETAILS' expected row, EVENTS' marks and trend rows, and the reducer keeping a selection and a colour
by lease id."""
from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, Optional
from unittest import mock

import helpers  # noqa: F401  # pyright: ignore[reportUnusedImport] - src/ on sys.path
import view as _view
from test_state import Case, entry, model

from godfather import sources
from godfather import theme
from godfather.adapters import slotfather as adapter
from godfather.layout import Rect

DARK = {"SLOTFATHER_TERMINAL_BG": "#282c34", "SLOTFATHER_TERMINAL_FG": "#ffffff"}
NOW = dt.datetime(2026, 11, 13, 10, 0, 0, tzinfo=dt.timezone.utc)
EXPECTED = {"key": "straylight · gate", "p50": 1080.0, "p90": 1860.0, "runs": 23, "fallback": False}


def stamp(moment: dt.datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def raw_item(pid: int, since: str, kind: str = "held", **fields: Any) -> dict[str, Any]:
    return dict(dict(key=[pid, since], kind=kind, count=1, pid=pid, procs=1, yields=0, cpu=10.0, rss=1024, tree=[],
                     queue="", label=f"job{pid}", cmd="work", meta="", workdir="", asked="", context={},
                     priority="normal", ticket=None, served=None, reprioritized=None, yieldable=False,
                     passable=False, all=False, since=stamp(NOW), granted=stamp(NOW) if kind != "waiting" else None,
                     asked_at=None, idle_since=None, est_start=None), **fields)


def snapshot(holders: list[dict[str, Any]], waiters: Optional[list[dict[str, Any]]] = None,
             entries: Optional[list[dict[str, Any]]] = None, events: Optional[list[dict[str, Any]]] = None) -> Any:
    block: dict[str, Any] = {"v": 1, "directory": "/q", "slots": 4, "cores": 8, "saved": False, "basis": {}, "averages": [1, 1, 1],
             "draining": 0, "advice": {}, "holders": holders, "waiters": waiters or [], "used": len(holders),
             "free": 4 - len(holders), "memory_block": None, "admission": None, "order": "window", "start": None,
             "reserved": None, "limit": 0, "memory": {}, "history": [], "events": events or [],
             "updated": stamp(NOW), "overtake_window": 600}
    found: dict[str, Any] = {"v": 2, "host": "mbp", "time": stamp(NOW), "dashboard": block}
    if entries is not None:
        found["entries"] = entries
    return found


def released(when: dt.datetime, pid: int, detail: str = "exit 0, ran 34m00s", label: str = "gate") -> dict[str, Any]:
    return {"when": when, "verb": "released", "count": 1, "pid": pid, "delta": -1, "label": label, "detail": detail}


def log_line(kind: str, when: dt.datetime, key: str, details: dict[str, Any]) -> str:
    return json.dumps({"v": 1, "time": stamp(when), "host": "mbp", "source": "slotfather", "kind": kind,
                       "subject": {"type": "run_type", "id": key}, "actor": {"type": "tool"}, "details": details})


TREND = {"verdict": "slower", "p50_recent": 1416.0, "p50_before": 1080.0, "ratio": 1.31, "recent": 10, "before": 30,
         "plan_change": None, "why": "same plan"}


class AdapterTests(unittest.TestCase):
    def test_an_entry_is_known_by_its_lease_id_with_its_expected_run(self) -> None:
        since = "2026-11-13T09:48:00Z"
        found = adapter.decode(snapshot(
            [raw_item(41, since)], [raw_item(42, since, "waiting")],
            entries=[{"pid": 41, "since": since, "lease_id": "a1", "expected": EXPECTED, "overrun": True},
                     {"pid": 42, "since": since, "lease_id": "b2", "expected": None}]))
        held, waiting = found["holders"][0], found["waiters"][0]
        self.assertEqual(("a1", "b2"), (held["key"], waiting["key"]))
        self.assertEqual((EXPECTED, True), (held["expected"], held["overrun"]))
        self.assertEqual((None, False), (waiting["expected"], waiting["overrun"]))

    def test_two_entries_of_one_pid_and_second_keep_their_own_lease_ids(self) -> None:
        since = "2026-11-13T09:48:00Z"
        found = adapter.decode(snapshot(
            [raw_item(41, since, label="first"), raw_item(41, since, label="second")],
            entries=[{"pid": 41, "since": since, "lease_id": "one"}, {"pid": 41, "since": since, "lease_id": "two"}]))
        self.assertEqual(["one", "two"], [item["key"] for item in found["holders"]])

    def test_an_older_slotfather_without_lease_ids_keeps_the_pid_and_since_key(self) -> None:
        since = "2026-11-13T09:48:00Z"
        for entries in (None, [{"pid": 41, "since": since}]):
            with self.subTest(entries=entries):
                found = adapter.decode(snapshot([raw_item(41, since)], entries=entries))
                self.assertEqual((41, since), found["holders"][0]["key"])
                self.assertIsNone(found["holders"][0]["expected"])

    def test_a_malformed_expected_is_no_expected(self) -> None:
        since = "2026-11-13T09:48:00Z"
        for bad in ({"p50": "soon", "p90": 1, "runs": 3}, {"p50": 10, "p90": None, "runs": 3}, [1, 2], 7,
                    {"p50": 10, "p90": 20, "runs": True}):
            with self.subTest(bad=bad):
                found = adapter.decode(snapshot([raw_item(41, since)],
                                                entries=[{"pid": 41, "since": since, "lease_id": "a", "expected": bad}]))
                self.assertIsNone(found["holders"][0]["expected"])

    def test_run_events_are_read_from_log_lines_and_the_rest_skipped(self) -> None:
        lines = [log_line("run.trend", NOW, "straylight · gate", TREND),
                 log_line("lease.granted", NOW, "x", {}),
                 '{"v": 2, "kind": "run.trend"}', "not json", "",
                 log_line("run.outlier", NOW, "straylight · gate",
                          {"lease_id": "a1", "ran": 2040.0, "p50": 1080.0, "p90": 1860.0, "ended": stamp(NOW),
                           "later": [1000, 1100, 1050]})]
        found = adapter.run_events(lines)
        self.assertEqual([("run.trend", "straylight · gate"), ("run.outlier", "straylight · gate")],
                         [(event["kind"], event["key"]) for event in found])
        self.assertEqual(NOW, found[0]["when"])

    def test_an_outlier_marks_the_release_it_was_and_a_slower_trend_the_release_that_turned_it(self) -> None:
        base = NOW - dt.timedelta(minutes=30)
        events = [released(base, 1), released(base + dt.timedelta(minutes=5), 2),
                  released(base + dt.timedelta(minutes=10), 3)]
        runs = adapter.run_events([
            log_line("run.outlier", base + dt.timedelta(minutes=20), "straylight · gate",
                     {"lease_id": "x", "ran": 2040.0, "ended": stamp(base + dt.timedelta(minutes=5, seconds=1))}),
            log_line("run.trend", base + dt.timedelta(minutes=10, seconds=1), "straylight · gate", TREND)])
        merged = adapter.merge_runs(events, runs)
        self.assertEqual([None, "outlier", "slower", None], [event.get("run") for event in merged])
        trend = merged[3]
        self.assertEqual(("trend", "straylight · gate", TREND), (trend["verb"], trend["label"], trend["trend"]))
        self.assertEqual(events[0]["detail"], merged[0]["detail"], "the events themselves are not changed")
        self.assertNotIn("run", events[1])

    def test_a_release_after_the_trend_or_two_in_its_window_get_no_mark_and_no_events_no_trend_rows(self) -> None:
        trend = adapter.run_events([log_line("run.trend", NOW, "straylight · gate", TREND)])
        after = adapter.merge_runs([released(NOW - dt.timedelta(minutes=1), 9),
                                    released(NOW + dt.timedelta(seconds=1), 1)], trend)
        self.assertEqual([None, "trend", None], [e.get("run") if e["verb"] != "trend" else "trend" for e in after])
        two = adapter.merge_runs([released(NOW, 1), released(NOW - dt.timedelta(seconds=1), 2)], trend)
        self.assertEqual([None, None], [e.get("run") for e in two if e["verb"] == "released"])
        self.assertEqual([], adapter.merge_runs([], trend))

    def test_a_trend_older_than_the_events_shown_is_left_out_and_a_steady_one_marks_nothing(self) -> None:
        events = [released(NOW, 1)]
        runs = adapter.run_events([
            log_line("run.trend", NOW - dt.timedelta(hours=2), "old", TREND),
            log_line("run.trend", NOW + dt.timedelta(seconds=1), "straylight · gate", dict(TREND, verdict="steady"))])
        merged = adapter.merge_runs(events, runs)
        self.assertEqual([None, "straylight · gate"], [event.get("run") or event.get("trend") and event["label"]
                                                       for event in merged])


class LogTailTests(unittest.TestCase):
    def test_the_log_is_read_from_its_end_and_a_cut_first_line_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "events.jsonl"
            path.write_text("".join(f'{{"n": {n}}}\n' for n in range(100)))
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("GODFATHER_FIXTURES", None)
                lines = sources.tail_log("slotfather", str(path), size=64)
        self.assertEqual('{"n": 99}', lines[-1])
        self.assertTrue(all(line.startswith('{"n"') for line in lines), lines[:1])
        self.assertLess(len(lines), 100)

    def test_fixture_mode_reads_the_tools_recorded_log_and_a_missing_one_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "slotfather").mkdir()
            (Path(folder) / "slotfather" / "events.jsonl").write_text('{"a": 1}\n')
            with mock.patch.dict(os.environ, {"GODFATHER_FIXTURES": folder}):
                self.assertEqual(('{"a": 1}',), sources.tail_log("slotfather", "/elsewhere/events.jsonl"))
                self.assertEqual((), sources.tail_log("lanefather", "/elsewhere/events.jsonl"))

    def test_a_cut_at_a_line_start_keeps_that_line_and_an_unchanged_log_is_not_read_again(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "events.jsonl"
            path.write_text('{"n": 1}\n{"n": 2}\n')
            os.environ.pop("GODFATHER_FIXTURES", None)
            self.assertEqual(('{"n": 2}',), sources.tail_log("slotfather", str(path), size=9))
            first = sources.tail_log("slotfather", str(path))
            self.assertIs(first, sources.tail_log("slotfather", str(path)))


class ViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tool: Any = _view.fresh()
        self.addCleanup(_view.fresh)
        self.tool.set_theme(theme.resolve("auto", environ=DARK))

    def queue(self, found: dict[str, Any], width: int = 100) -> Any:
        canvas = self.tool.Canvas(width, 8)
        self.tool.draw_requests(canvas, Rect(0, 0, 8, width), found, 0, NOW)
        return canvas

    @staticmethod
    def find(canvas: Any, text: str) -> tuple[int, int]:
        for y, line in enumerate(canvas.render(False).splitlines()):
            if text in line:
                return y, line.index(text)
        raise AssertionError(f"{text!r} not on screen:\n{canvas.render(False)}")

    def test_queue_running_rows_show_time_over_expected_highlighted_past_p90(self) -> None:
        granted = NOW - dt.timedelta(minutes=12)
        found = model([entry(1, granted=granted, expected=EXPECTED, overrun=False),
                       entry(2, granted=NOW - dt.timedelta(minutes=40), expected=EXPECTED, overrun=True),
                       entry(3, granted=granted)])
        canvas = self.queue(found)
        y, x = self.find(canvas, "12m / ~18m")
        self.assertEqual(found["holders"][0]["style"], canvas.cells[y][x][1])
        y, x = self.find(canvas, "40m / ~18m")
        self.assertEqual("warn", canvas.cells[y][x][1], "past its p90")
        self.find(canvas, "12m00s")  # no expected run: the time as before

    def test_queue_waiting_rows_show_their_expected_run(self) -> None:
        found = model([], [entry(1, "waiting", expected=EXPECTED), entry(2, "waiting")])
        canvas = self.queue(found)
        self.find(canvas, "waiting #1 · ~18m")
        self.assertNotIn("waiting #2 ·", canvas.render(False))

    def test_details_say_the_expected_run_p90_and_runs(self) -> None:
        found = model([entry(1, granted=NOW - dt.timedelta(minutes=12), expected=EXPECTED)],
                      [entry(2, "waiting", expected=dict(EXPECTED, runs=1, p50=45.0, p90=50.0))])
        for selected, text in ((0, "expected  18m · p90 31m · 23 runs"), (1, "expected  45s · p90 50s · 1 run")):
            with self.subTest(selected=selected):
                canvas = self.tool.Canvas(90, 24)
                self.tool.draw_detail(canvas, Rect(0, 0, 24, 90), found, selected, None, NOW)
                self.assertIn(text, self.squeeze(canvas.render(False)))

    @staticmethod
    def squeeze(text: str) -> str:
        """The screen with each run of spaces after the details' label column as two."""
        import re
        return re.sub(r"(expected) +", r"\1  ", text)

    def test_details_highlight_an_overrun(self) -> None:
        found = model([entry(1, granted=NOW - dt.timedelta(minutes=40), expected=EXPECTED, overrun=True)])
        canvas = self.tool.Canvas(90, 24)
        self.tool.draw_detail(canvas, Rect(0, 0, 24, 90), found, 0, None, NOW)
        y, x = self.find(canvas, "18m · p90 31m")
        self.assertEqual("warn", canvas.cells[y][x][1])

    def events(self, found: list[dict[str, Any]], width: int = 80) -> Any:
        canvas = self.tool.Canvas(width, 8)
        self.tool.draw_events(canvas, Rect(0, 0, 8, width), {"events": found})
        return canvas

    def test_events_mark_an_outlier_and_a_run_of_a_slowdown(self) -> None:
        found = [dict(released(NOW, 1, "exit 0, ran 34m00s", "outlier run"), run="outlier"),
                 dict(released(NOW, 2, "exit 0, ran 31m00s", "slower run"), run="slower"),
                 released(NOW, 3, "exit 0, ran 18m00s", "plain run")]
        canvas = self.events(found)
        y, x = self.find(canvas, "ok 34m00s ▲")
        self.assertEqual("warn", canvas.cells[y][x + len("ok 34m00s ")][1])
        self.find(canvas, "ok 31m00s ↗")
        self.assertTrue(canvas.render(False).splitlines()[self.find(canvas, "plain run")[0]].rstrip("│ ").endswith(
            "ok 18m00s"), "no mark on a plain run")

    def test_a_trend_is_its_own_row_with_the_key_and_the_reason(self) -> None:
        trend = {"when": NOW, "verb": "trend", "count": 0, "pid": 0, "delta": 0, "label": "straylight · gate",
                 "trend": TREND}
        grew = dict(trend, label="fathers · check · plan 3f2a",
                    trend=dict(TREND, ratio=1.4, why="plan grew: +2 checks"))
        faster = dict(trend, label="sid-kit · gate", trend=dict(TREND, verdict="faster", ratio=0.62))
        canvas = self.events([trend, grew, faster], 100)
        y, x = self.find(canvas, "Straylight gate · slower +31% · same plan")
        self.assertEqual("warn", canvas.cells[y][x + len("Straylight gate · ")][1])
        self.find(canvas, "Fathers check plan 3f2a · slower +40% · plan grew +2 checks")
        y, x = self.find(canvas, "Sid-kit gate · faster -38% · same plan")
        self.assertEqual("dim", canvas.cells[y][x + len("Sid-kit gate · ")][1])

    def test_a_trend_row_in_a_narrow_events_panel_that_has_dropped_its_slots(self) -> None:
        trend = {"when": NOW, "verb": "trend", "count": 0, "pid": 0, "delta": 0, "label": "straylight · gate",
                 "trend": TREND}
        for width in (40, 30, 24):
            with self.subTest(width=width):
                self.assertIn("Stray", self.events([trend, released(NOW, 1)], width).render(False))

    def test_ascii_terminals_get_ascii_run_marks(self) -> None:
        self.tool.use_marks(True)
        canvas = self.events([dict(released(NOW, 1), run="outlier"), dict(released(NOW, 2), run="slower")])
        self.find(canvas, "ok 34m00s ^")
        self.find(canvas, "ok 34m00s /")


class IdentityReducerTests(Case):
    def test_the_selection_and_the_colour_follow_the_lease_id(self) -> None:
        first, second = entry(1, key="lease-a", label="first"), entry(2, key="lease-b", label="second")
        state = self.screen(model([first, second]))
        state, _ = self.keys(state, "down")
        self.assertEqual("lease-b", state.selection.key[0])
        colour = state.colours[("lease-b", 0)]
        # the first lease ends; the same pid and second come back as a new lease, which must not inherit anything
        reborn = entry(1, key="lease-c", label="first again")
        state, _ = self.send(state, self.tool.ModelLoaded(model([second, reborn])))
        self.assertEqual(("lease-b", 0), state.selection.key)
        self.assertEqual(0, state.selection.index, "the selection moved with its lease")
        self.assertEqual(colour, state.colours[("lease-b", 0)])
        self.assertNotIn(("lease-a", 0), state.colours)


if __name__ == "__main__":
    unittest.main()
