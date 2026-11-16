"""EVENTS rows are selectable and DETAILS follows the focused panel (fathers#152, owner 2026-11-16): with EVENTS
focused ↑↓ move a row cursor over what it shows, DETAILS shows that event or run (a run's marks explained, another
event's structured fields); with QUEUE focused it shows QUEUE's entry. The cursor on the newest row stays there as
new rows arrive; scrolled down, the rows above are counted and Home goes back to the newest."""
from __future__ import annotations

import datetime
import unittest
from typing import Any, cast

import helpers
from helpers import logged, rows_of
from test_state import entry, model

from godfather import reducer, state as state_module, tui
from godfather.model import Dashboard
from godfather.views import common, details, overview

WHEN = datetime.datetime(2026, 11, 16, 9, 0, tzinfo=datetime.timezone.utc)


def at(seconds: float) -> datetime.datetime:
    return WHEN + datetime.timedelta(seconds=seconds)


def runs(count: int) -> list[dict[str, Any]]:
    """`count` finished runs, one a minute, oldest first, as Slotfather logs a grant and its release."""
    found: list[dict[str, Any]] = []
    for n in range(count):
        found.append(logged("lease.granted", at(60 * n), f"run{n}", 100 + n, count=1, waited=0))
        found.append(logged("lease.released", at(60 * n + 30), f"run{n}", 100 + n, count=1, exit=0, ran=30))
    return found


class CursorCase(unittest.TestCase):
    def screen(self, events: list[dict[str, Any]], width: int = 156, height: int = 43) -> Any:
        found = model([entry(0), entry(1)], events=rows_of(events), updated=at(3600))
        state, _ = reducer.reduce(state_module.AppState(), state_module.ModelLoaded(cast(Dashboard, found), 8), 0.0)
        return self.draw(state, width, height)

    def draw(self, state: Any, width: int = 156, height: int = 43) -> Any:
        canvas = tui.Canvas(width, height)
        overview.render(canvas, state, 0.0)
        self.canvas = canvas
        state, _ = reducer.reduce(state, state_module.Drawn(helpers.frame_of(canvas)), 0.0)
        return state

    def keys(self, state: Any, *keys: str) -> Any:
        for key in keys:
            state, _ = reducer.reduce(state, state_module.Key(key), 0.0)
            state = self.draw(state)
        return state

    def plain(self) -> str:
        return self.canvas.render(False)

    def details_text(self) -> str:
        view = self.canvas.tables["details"]
        return "\n".join("".join(str(cell[0]) for cell in row[view["x"]:view["x"] + view["width"]])
                         for row in self.canvas.cells[view["y"]:view["y"] + view["height"]])


class FocusTests(CursorCase):
    def test_details_follow_the_focused_panel(self) -> None:
        state = self.screen(runs(3))
        self.assertIn("LEASE DETAILS", self.plain(), "QUEUE focused: its entry")
        state = self.keys(state, "\t")  # QUEUE, EVENTS, DETAILS by columns
        self.assertEqual(("events", "events"), (state.focus, state.shows))
        self.assertIn("RUN DETAILS", self.plain())
        self.assertRegex(self.details_text(), r"label\s+run2")
        state = self.keys(state, "\t")  # DETAILS focused: it keeps showing the event, to scroll it
        self.assertEqual(("details", "events"), (state.focus, state.shows))
        self.assertIn("RUN DETAILS", self.plain())
        state = self.keys(state, "\t")
        self.assertEqual("requests", state.shows)
        self.assertIn("LEASE DETAILS", self.plain())

    def test_arrows_move_the_cursor_and_details_show_its_row(self) -> None:
        state = self.keys(self.screen(runs(3)), "\t", "down")
        self.assertEqual(1, state_module.event_index(state))
        self.assertRegex(self.details_text(), r"label\s+run1")
        row = self.canvas.tables["events"]["top"] + 1
        self.assertEqual("┃", self.canvas.cells[row][self.canvas.tables["events"]["x"]][0], "the selection bar")
        state = self.keys(state, "down", "down", "down")
        self.assertEqual(2, state_module.event_index(state), "kept in range")
        state = self.keys(state, "home")
        self.assertEqual((0, None), (state_module.event_index(state), state.picked.get("events")))

    def test_a_click_on_a_row_selects_it_and_focuses_events(self) -> None:
        state = self.screen(runs(3))
        view = self.canvas.tables["events"]
        state, _ = reducer.reduce(state, state_module.Mouse(0, view["x"] + 5, view["top"] + 2, True), 0.0)
        self.assertEqual(("events", 2), (state.focus, state_module.event_index(state)))
        self.draw(state)
        self.assertRegex(self.details_text(), r"label\s+run0")

    def test_one_column_without_events_shows_queues_entry(self) -> None:
        state = self.keys(self.screen(runs(3)), "\t")
        state = self.draw(state, 76, 40)
        self.assertEqual("requests", state.shows)


class NewRowsTests(CursorCase):
    def arrive(self, state: Any, events: list[dict[str, Any]]) -> Any:
        found = model([entry(0), entry(1)], events=rows_of(events), updated=at(3600))
        state, _ = reducer.reduce(state, state_module.ModelLoaded(cast(Dashboard, found), 8), 0.0)
        return self.draw(state)

    def test_the_cursor_on_the_newest_stays_on_the_newest(self) -> None:
        state = self.keys(self.screen(runs(3)), "\t")
        state = self.arrive(state, runs(4))
        self.assertEqual(0, state_module.event_index(state))
        self.assertRegex(self.details_text(), r"label\s+run3")

    def test_a_picked_row_stays_picked_and_scrolled_rows_are_counted(self) -> None:
        state = self.keys(self.screen(runs(60), height=30), "\t", *["down"] * 30)
        picked = state.picked["events"]
        self.assertGreater(state.scroll["events"], 0, "the scroll follows the cursor")
        state = self.arrive(state, runs(62))
        self.assertEqual(picked, state.picked["events"])
        self.assertEqual(32, state_module.event_index(state))
        self.assertEqual(2, state.fresh_events, "↑ 2 new")
        state = self.keys(state, "home")
        self.assertEqual((0, 0, None), (state_module.event_index(state), state.fresh_events,
                                        state.scroll.get("events")))

    def test_a_filter_change_goes_back_to_the_newest(self) -> None:
        state = self.keys(self.screen(runs(5)), "\t", "down", "down")
        state = self.keys(state, "r")
        self.assertIsNone(state.picked.get("events"))

    def test_a_filter_change_shows_the_newest_it_selects(self) -> None:
        """fathers#88 review 8: the view goes back to the top with the selection, not left scrolled away."""
        state = self.keys(self.screen(runs(60), height=30), "\t", *["down"] * 30)
        self.assertGreater(state.scroll.get("events", 0), 0)
        state = self.keys(state, "r")
        self.assertEqual((None, 0), (state.scroll.get("events"), state_module.event_index(state)))


class FactsTests(unittest.TestCase):
    marks = common.marks_for()

    def facts(self, row: dict[str, Any]) -> dict[str, str]:
        return {name: "".join(text for text, _ in parts) for name, parts in details.event_facts(row, at(9999),
                                                                                                 self.marks)}

    def test_a_run_with_its_marks_explained(self) -> None:
        from godfather import events

        rows = events.runs(rows_of([
            logged("lease.granted", at(0), "gate", 7, count=2, waited=12, priority="batch"),
            logged("lease.released", at(73), "gate", 7, count=2, exit=0, ran=73),
            logged("run.outlier", at(74), source="slotfather", ident="straylight · gate", ran=73.0, p90=52.0,
                   ended="2026-11-16T09:01:13Z"),
            logged("run.trend", at(75), source="slotfather", ident="straylight · gate", verdict="slower", ratio=1.38,
                   why="plan grew: +2 checks")]))
        run = next(row for row in rows if row["kind"] == "run")
        found = self.facts(run)
        self.assertEqual("gate", found["label"])
        self.assertEqual("2 slots", found["slots"])
        self.assertEqual("batch", found["priority"])
        self.assertEqual("1m13s", found["ran"])
        self.assertEqual("7", found["pid"])
        self.assertIn("▲ outlier · ran 1m13s · its run type's p90 52s", found["mark"])

    def test_a_trend_mark_says_how_much_and_why(self) -> None:
        row = {"kind": "run", "label": "check", "count": 1, "detail": [("ok 5s", "dim")], "run": "slower",
               "trend": {"verdict": "slower", "ratio": 1.38, "why": "plan grew: +2 checks"}, "start": at(0),
               "when": at(5), "details": {"ran": 5}}
        found = details.event_facts(row, at(10), self.marks)
        text = ["".join(t for t, _ in parts) for _, parts in found]
        self.assertIn("↗ slower +38% than its run type's p50", text)
        self.assertIn("plan grew: +2 checks", text)

    def test_a_lane_event_shows_its_structured_fields(self) -> None:
        row = rows_of([logged("lane.checked", at(0), source="lanefather", ident="710-speech", status="green",
                              suites=["tooling", "native"], checks=14)])[0]
        row["subject"] = dict(row["subject"], type="lane", project="Straylight", ticket="710")
        found = self.facts(row)
        self.assertEqual("lane.checked", found["event"])
        self.assertEqual("Lanefather", found["tool"])
        self.assertEqual("Straylight", found["project"])
        self.assertEqual("green", found["status"])
        self.assertEqual("tooling, native", found["suites"])
        self.assertEqual("14", found["checks"])


if __name__ == "__main__":
    unittest.main()
