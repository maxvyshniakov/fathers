"""EVENTS as one feed of every source (fathers#84): Slotfather's rows from its event log are the rows its snapshot
gave (the replay comparison), each with a result; Lanefather's and junkfather's rows come between them in time order
with their tags and colours; `f` and `/` filter, the border says so, and `↑ N new` counts only what the filter lets
through; narrow panels give way in the specified order."""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import unittest
from typing import Any

import view as _view
from helpers import frame_of, logged, rows_of
from test_goldens import goldens_tool

from godfather.layout import Rect

NOW = dt.datetime(2026, 11, 4, 9, 59, 39, tzinfo=dt.timezone.utc)


class ReplayTests(unittest.TestCase):
    """The scene's queue operations: what EVENTS drew from Slotfather's snapshot (`dashboard.events`, still in the
    fixture's reply) against what it draws from the same operations in Slotfather's event log."""

    def test_the_log_gives_the_snapshots_rows_and_a_result_for_every_one(self) -> None:
        tool = goldens_tool()
        replies = json.loads((tool.SCENE / "slotfather" / "replies.json").read_text())
        snapshot = replies["status --json --full"]["json"]["dashboard"]["events"]
        with tool.environment(None):
            from godfather import app

            model, error = app.Screen().load()
        self.assertIsNone(error)
        assert model is not None
        rows = model["events"]
        self.assertEqual(len(snapshot), len(rows))
        for before, after in zip(snapshot, rows):
            self.assertEqual((before["when"][:19], before["count"], before["label"], before["pid"]),
                             (after["when"].strftime("%Y-%m-%dT%H:%M:%S"), after["count"], after["label"],
                              after["pid"]))
            self.assertEqual(before["verb"] == "granted", after["filled"], "filled cells for a grant, else hollow")
            self.assertTrue("".join(text for text, _ in after["detail"]), f"a result for {after['kind']}")
        self.assertEqual({"lease.granted", "lease.released"}, {row["kind"] for row in rows})
        self.assertEqual([("slotfather", "slot")], model.get("sources"))

    def test_a_slotfather_whose_capabilities_name_no_log_shows_none_and_says_why(self) -> None:
        import shutil
        import tempfile
        from pathlib import Path

        tool = goldens_tool()
        folder = Path(tempfile.mkdtemp(prefix="godfather-old-"))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        shutil.copytree(tool.SCENE, folder / "scene")
        replies_path = folder / "scene" / "slotfather" / "replies.json"
        replies = json.loads(replies_path.read_text())
        del replies["capabilities --json"]["json"]["dashboard"]["events"]
        replies_path.write_text(json.dumps(replies))
        with tool.environment(None, folder / "scene"):
            from godfather import app

            model, _ = app.Screen().load()
        assert model is not None
        self.assertEqual(([], [], "slotfather too old for events"),
                         (model["events"], model.get("sources"), model.get("events_note")),
                         "no fallback to the snapshot's described events")

    def test_a_slotfather_whose_log_has_only_newer_versions_needs_a_newer_godfather(self) -> None:
        import shutil
        import tempfile
        from pathlib import Path

        tool = goldens_tool()
        folder = Path(tempfile.mkdtemp(prefix="godfather-new-"))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        shutil.copytree(tool.SCENE, folder / "scene")
        replies_path = folder / "scene" / "slotfather" / "replies.json"
        replies = json.loads(replies_path.read_text())
        replies["capabilities --json"]["json"]["dashboard"]["events"]["versions"] = [2]
        replies_path.write_text(json.dumps(replies))
        with tool.environment(None, folder / "scene"):
            from godfather import app

            model, _ = app.Screen().load()
        assert model is not None
        self.assertEqual("slotfather's events need a newer godfather", model.get("events_note"))

    def test_a_rows_identity_survives_reading_its_log_again(self) -> None:
        """fathers#84 review: a Tail made anew numbers lines from one again; `↑ N new` must not take that for new
        rows."""
        import tempfile
        from pathlib import Path

        from godfather import events

        lines = [json.dumps(item) + "\n" for item in (mixed()[0], mixed()[3])]
        keys: list[list[Any]] = []
        with tempfile.TemporaryDirectory() as folder:
            for name, text in (("whole", "".join(lines)), ("later", lines[1])):  # the first line rotated away
                log = Path(folder) / name
                log.write_text(text)
                feed = events.Feed("mbp")
                feed.follow("slotfather", str(log))
                feed.poll()
                keys.append([row["key"] for row in events.rows(feed.events(), TAGS)])
                feed.close()
        self.assertEqual(keys[0][1], keys[1][0], "the same event, the same identity")
        self.assertNotEqual(keys[0][0], keys[0][1])

    def test_every_slotfather_kind_today_has_a_result(self) -> None:
        log = [logged("lease.granted", NOW, "a", 1, count=1, waited=3),
               logged("lease.released", NOW, "a", 1, count=1, exit=0, ran=60),
               logged("request.left", NOW, "b", 2, count=1, gave_up_after=300),
               logged("lease.reaped", NOW, "c", 3, count=1, ran=600, waited=1, reason="owner exited"),
               logged("request.reaped", NOW, "d", 4, count=1, ran=None, waited=700, reason="owner exited"),
               logged("request.reprioritized", NOW, "e", 5, to="interactive", **{"from": "batch"}),
               logged("request.waiting_long", NOW, "f", 6, waited=2700, est_start="2026-11-04T10:30:00.000Z")]
        found = {row["kind"]: row["detail"][0] for row in rows_of(log)}
        self.assertEqual({"lease.granted": ("wait 3s", "text"), "lease.released": ("ok 1m00s", "dim"),
                          "request.left": ("gave up 5m", "dim"), "lease.reaped": ("owner died · 10m", "bad"),
                          "request.reaped": ("owner died · 11m", "warn"),
                          "request.reprioritized": ("batch → interactive", "dim"),
                          "request.waiting_long": ("waiting 45m", "warn")}, found)


def mixed() -> list[dict[str, Any]]:
    return [logged("lease.granted", NOW - dt.timedelta(seconds=50), "build", 101, count=1, waited=0),
            logged("lane.checked", NOW - dt.timedelta(seconds=40), source="lanefather", ident="710-speech",
                   status="green", suites=["tooling", "native"], message="check 710-speech tree=abc green"),
            logged("entry.owned", NOW - dt.timedelta(seconds=30), "vite dev", 90412, source="junkfather",
                   ttl=7200),
            logged("lease.released", NOW - dt.timedelta(seconds=20), "build", 101, count=1, exit=0, ran=30),
            dict(logged("gate.finished", NOW - dt.timedelta(seconds=10), source="lanefather", level="error",
                        status="red", seconds=1177),
                 subject={"type": "commit", "id": "cedf8b972dd8ed11d8af", "project": "Straylight"}),
            logged("queue.updated", NOW, source="lanefather", ident="Straylight", reason="policy", moved=2)]


class FormatterTests(unittest.TestCase):
    """Lanefather's and junkfather's kinds from their structured fields (their writers: lanefather/lanefather/*.py
    journal.ledger(details=...), junkfather/junkfather/*.py events.write())."""

    def test_lanefather_kinds(self) -> None:
        from godfather import events

        cases = [("lane.started", {"base": "main", "claims": ["a/**"]}, "started", ("on main · 1 claim", "dim")),
                 ("lane.checked", {"status": "red", "suites": ["x"]}, "checked", ("red · 1 suite", "bad")),
                 ("lane.post_merge", {"status": "green", "scope": "full"}, "post-merge", ("green · full", "dim")),
                 ("claims.added", {"added": ["b/**"], "overlaps": [{"lane": "x", "claim": "b/**"}]}, "claims",
                  ("+1 · 1 overlap", "warn")),
                 ("lane.done", {"freed_bytes": 3 * 2**30}, "done", ("freed 3.0 GB", "dim")),
                 ("review.finished", {"status": "failed", "seconds": 61}, "reviewed", ("failed · 1m", "warn")),
                 ("promote.step", {"step": "install", "exit": 2}, "promote", ("install · exit 2", "bad")),
                 ("sync.finished", {"moved": ["dev"], "failed": []}, "synced", ("1 moved", "dim")),
                 ("load.waited", {"seconds": 300, "gave_up": True}, "load wait", ("5m · gave up", "warn"))]
        for kind, details, event, detail in cases:
            with self.subTest(kind=kind):
                found = events.lanefather_shown(kind, dict(details, message="prose is never read"))
                assert found is not None
                self.assertEqual((event, detail), (found.event, found.detail[0]))
        self.assertIsNone(events.lanefather_shown("ticket.failed", {}), "the generic form for the rest")

    def test_junkfather_kinds(self) -> None:
        from godfather import events

        cases = [("entry.owned", {"ttl": 7200}, "owned", ("ttl 2h00m", "dim")),
                 ("entry.exited", {"exit": 3}, "exited", ("exit 3", "warn")),
                 ("entry.reaped", {"reason": "orphan", "result": "killed"}, "reaped", ("orphan · killed", "dim")),
                 ("entry.reap_failed", {"reason": "orphan"}, "reap fail", ("orphan", "bad")),
                 ("entry.handed", {"to": {"lane": "710"}}, "handed", ("to 710", "dim")),
                 ("unowned.reported", {"cmd": "node dev", "why": "orphan"}, "unowned", ("orphan", "warn")),
                 ("hook.failed", {"error": "boom"}, "hook fail", ("boom", "bad"))]
        for kind, details, event, detail in cases:
            with self.subTest(kind=kind):
                found = events.junkfather_shown(kind, details)
                assert found is not None
                self.assertEqual((event, detail), (found.event, found.detail[0]))
        reported = events.junkfather_shown("unowned.reported", {"cmd": "node dev", "why": "orphan"})
        self.assertEqual("node dev", reported and reported.label, "an unowned process by its command")

    def test_an_unknown_kind_shows_itself_and_its_fields(self) -> None:
        from godfather import events

        found = rows_of([logged("cpu.reported", NOW, source="junkfather", level="error", reaped=2,
                                errors=["x"], nested={"a": 1})])  # sweep.finished has its words now (fathers#86)
        self.assertEqual(("cpu.reported", [("reaped=2 errors=x", "bad")]), (found[0]["event"], found[0]["detail"]))
        self.assertEqual("reason=x", events.compact({"message": "prose", "reason": "x"}))
        self.assertEqual("prose", events.compact({"message": "prose"}))


TAGS = {"slotfather": "slot", "lanefather": "lane", "junkfather": "junk"}
SOURCES = [("slotfather", "slot"), ("lanefather", "lane"), ("junkfather", "junk")]


class LogViewTests(unittest.TestCase):
    """fathers#152: one tool's log at a time, the title the switch, each log its own columns, one row per run."""

    def setUp(self) -> None:
        self.tool: Any = _view.fresh()
        self.addCleanup(_view.fresh)

    def draw(self, width: int, found: Any = None, rows: Any = None, height: int = 12,
             sources: Any = None) -> tuple[Any, list[str]]:
        canvas = self.tool.Canvas(width, height)
        model = {"events": rows if rows is not None else rows_of(mixed(), TAGS),
                 "sources": SOURCES if sources is None else sources}
        self.tool.draw_events(canvas, Rect(0, 0, height, width), model, NOW, found)
        return canvas, canvas.render(False).splitlines()

    def body(self, lines: list[str]) -> list[str]:
        return [line for line in lines[2:-1] if line.strip("│ ")]

    def test_slots_log_is_the_default_one_row_per_run_its_title_the_switch(self) -> None:
        canvas, lines = self.draw(120)
        self.assertTrue(lines[0].startswith("╭─ ◂ SLOTS LOG ▸ ─"), lines[0])
        places = [lines[0].index(title) for title in ("label", "waited", "outcome", "pid")]
        self.assertEqual(sorted(places), places)
        body = self.body(lines)
        self.assertEqual(1, len(body), "the grant and the release of `build` are one row")
        self.assertRegex(body[0], r"▫ +build +no wait +ok 30s +101")
        self.assertEqual("key", canvas.cells[0][lines[0].index("SLOTS")][1], "the switchable title stands out")
        self.assertEqual((0, 3, 16), canvas.switch_at, "a click target")

    def test_a_run_reads_running_while_it_holds_its_slots(self) -> None:
        rows = rows_of(mixed()[:1], TAGS)
        _, lines = self.draw(120, rows=rows)
        self.assertRegex(self.body(lines)[0], r"▪ +build +no wait +running 50s +101")
        _, lines = self.draw(120, self.tool.Filter(raw=True))
        self.assertIn("every event", lines[0])
        self.assertEqual(2, len(self.body(lines)), "r: the grant and the release as logged")

    def test_lanes_and_procs_logs_have_their_own_columns(self) -> None:
        _, lines = self.draw(120, self.tool.Filter("lanefather"))
        self.assertIn("◂ LANES LOG ▸", lines[0])
        for title in ("lane", "result"):  # `event` gives way to the title over it
            self.assertIn(title, lines[0])
        body = self.body(lines)
        self.assertRegex(body[2], r"checked +710-speech +green · 2 suites")
        self.assertRegex(body[1], r"gate +Straylight cedf8b972d +red · 19m")
        self.assertRegex(body[0], r"queue\.upd… +Straylight +reason=policy moved=2", "an unknown kind")
        _, lines = self.draw(120, self.tool.Filter("junkfather"))
        self.assertIn("◂ PROCS LOG ▸", lines[0])
        self.assertRegex(self.body(lines)[0], r"owned +vite dev +ttl 2h00m +90412")

    def test_all_log_is_time_type_and_one_sentence(self) -> None:
        canvas, lines = self.draw(120, self.tool.Filter("all"))
        self.assertIn("◂ ALL LOG ▸", lines[0])
        self.assertIn("description", lines[0])
        body = self.body(lines)
        self.assertEqual(["lane", "lane", "slot", "proc", "lane"], [line.split()[2] for line in body])
        self.assertRegex(body[2], r"slot run +build · ok 30s · 1 slot")
        self.assertRegex(body[3], r"proc owned +vite dev · ttl 2h00m")
        self.assertRegex(body[4], r"lane checked +710-speech · green · 2 suites")
        y = next(n for n, line in enumerate(lines) if "proc owned" in line)
        self.assertEqual("source-junk", canvas.cells[y][lines[y].index("proc owned")][1], "the type in its tool's colour")

    def test_no_label_is_cut_shorter_than_16_cells_and_no_row_spans_columns_at_156(self) -> None:
        for view in ("slotfather", "lanefather", "junkfather", "all"):
            with self.subTest(view=view):
                found = self.tool.Filter(view)
                rows = rows_of(mixed(), TAGS)
                spec = self.tool.narrowed(self.tool.columns(view, 85, rows, [
                    self.tool.row_values(view, row, self.tool.EMOJI, NOW, 1) for row in rows]), 85)
                self.assertGreaterEqual(self.tool.room(spec, 85), 16)
                for line in self.draw(89, found)[1]:
                    self.assertEqual(89, len(line))

    def test_narrow_panels_drop_pid_then_slots_then_the_wait_and_keep_the_bare_title(self) -> None:
        rows = rows_of(mixed(), TAGS)
        values = [self.tool.row_values("slotfather", row, self.tool.EMOJI, NOW, 1) for row in rows]

        def keys(inner: int) -> list[str]:
            return [key for key, *_ in self.tool.narrowed(self.tool.columns("slotfather", inner, rows, values), inner)]

        self.assertEqual(["time", "slots", "label", "waited", "outcome", "pid"], keys(114))
        steps = [next(inner for inner in range(114, 20, -1) if gone not in keys(inner))
                 for gone in ("pid", "slots", "waited")]
        self.assertEqual(sorted(steps, reverse=True), steps, "pid goes, then slots, then the wait")
        _, lines = self.draw(36)
        self.assertTrue(lines[0].startswith("╭─ SLOTS LOG ─"), "no room for the arrows: the bare title")
        for width in range(24, 121):
            for line in self.draw(width)[1]:
                self.assertEqual(width, len(line), f"nothing drawn past the panel at {width}")

    def test_the_text_filter_filters_within_the_log(self) -> None:
        Filter = self.tool.Filter
        _, lines = self.draw(120, Filter("all", "VITE"))
        self.assertIn('ALL LOG ▸ ── "VITE" ──', lines[0])
        self.assertEqual(["proc"], [line.split()[2] for line in self.body(lines)], "label, any case")
        _, lines = self.draw(120, Filter(None, "vite"))
        self.assertIn("No events match the filter", lines[2], "SLOTS LOG has no vite")
        for needle, kind in (("cedf8b", "gate"), ("suites", "checked")):
            body = self.body(self.draw(120, Filter("lanefather", needle))[1])
            self.assertEqual([kind], [line.split()[2] for line in body], f"{needle}: subject id and detail")
        _, lines = self.draw(120, Filter("lanefather", "71", editing=True))
        self.assertTrue(lines[-1].startswith("╰─ / 71_ ─"), lines[-1])

    def test_one_source_has_a_bare_title_and_nothing_to_switch(self) -> None:
        canvas, lines = self.draw(100, rows=rows_of(mixed()[:1]), sources=SOURCES[:1], height=8)
        self.assertTrue(lines[0].startswith("╭─ SLOTS LOG ─"), lines[0])
        self.assertIsNone(canvas.switch_at)
        self.assertEqual("title", canvas.cells[0][3][1])

    def test_a_slotfather_without_an_event_log_says_so_and_shows_none(self) -> None:
        _, lines = self.draw(100, rows=[], sources=[], height=8)
        canvas = self.tool.Canvas(100, 8)
        self.tool.draw_events(canvas, Rect(0, 0, 8, 100), {"events": [], "sources": [],
                                                          "events_note": "slotfather too old for events"}, NOW)
        lines = canvas.render(False).splitlines()
        self.assertIn("SLOTS LOG ── slotfather too old for events ──", lines[0])
        self.assertEqual("", "".join(line.strip("│ ") for line in lines[1:-1]))

    def test_a_filter_is_named_even_when_its_note_does_not_fit(self) -> None:
        """fathers#84 review: a filter that hides rows is always visible."""
        _, lines = self.draw(60, self.tool.Filter("lanefather", "x" * 40))
        self.assertIn('LANES LOG ▸ ── "xxx', lines[0])
        self.assertRegex(lines[0], r"x… ─*╮$", "cut with an ellipsis, the corner kept")

    def test_skipped_lines_are_counted_in_the_bottom_border(self) -> None:
        canvas = self.tool.Canvas(100, 8)
        model = {"events": rows_of(mixed(), TAGS), "sources": SOURCES,
                 "events_skipped": {"slotfather": {"invalid": 0, "version": 0, "host": 3, "source": 0},
                                    "lanefather": {"invalid": 1, "version": 0, "host": 0, "source": 0}}}
        self.tool.draw_events(canvas, Rect(0, 0, 8, 100), model, NOW)
        last = canvas.render(False).splitlines()[-1]
        self.assertTrue(last.endswith(" 4 lines skipped: invalid, other host ─╯"), last)
        self.assertEqual("warn", canvas.cells[7][last.index("4 lines")][1])

    def test_the_help_and_the_footer_name_the_log_keys(self) -> None:
        keys = dict(entry[:2] for entry in self.tool.HELP[0][1])
        for key in ("← → · f", "r", "/"):
            self.assertIn(key, keys)
        self.assertEqual(("↑↓", "tab", "s", "?", "q"), tuple(key for key, _ in self.tool.FOOTER_HINTS))
        self.assertIn(("←→", "log"), self.tool.footer_hints("events"))
        self.assertNotIn(("←→", "log"), self.tool.footer_hints("requests"))


class FilterReducerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tool: Any = _view.fresh()
        self.addCleanup(_view.fresh)

    def state(self, rows: Any = None, scroll: int = 0, focus: str = "requests") -> Any:
        tool = self.tool
        model = {"holders": [], "waiters": [], "events": rows if rows is not None else rows_of(mixed(), TAGS),
                 "sources": SOURCES}
        canvas = tool.Canvas(156, 43)
        tool.draw_events(canvas, Rect(0, 0, 20, 90), model, NOW)
        canvas.tables["events"] = dict(x=0, y=0, width=90, height=20, top=2, visible=16, count=6, offset=0,
                                       base=None, row_items=None)
        state = dataclasses.replace(tool.AppState(model=model), frame=frame_of(canvas), scroll={"events": scroll},
                                    focus=focus)
        return tool.follow_events(state, model["events"])

    def keys(self, state: Any, *keys: str) -> Any:
        for key in keys:
            state, _ = self.tool.reduce(state, self.tool.Key(key), 0.0)
        return state

    def test_f_and_the_arrows_cycle_the_logs_present_then_all(self) -> None:
        state = self.state()
        seen: list[Any] = []
        for _ in range(4):
            state = self.keys(state, "f")
            seen.append(state.events_filter.source)
        self.assertEqual(["lanefather", "junkfather", "all", "slotfather"], seen, "from SLOTS LOG, the default")
        focused = self.state(focus="events")
        self.assertEqual("all", self.keys(focused, "left").events_filter.source, "← back, around")
        self.assertEqual("lanefather", self.keys(focused, "right").events_filter.source)
        self.assertIsNone(self.keys(self.state(), "right").events_filter.source, "→ with QUEUE focused: nothing")

    def test_a_click_on_the_title_switches_the_log_and_focuses_events(self) -> None:
        state = self.state()
        row, start, _ = state.frame.switch
        state, _ = self.tool.reduce(state, self.tool.Mouse(0, start + 2, row, True), 0.0)
        self.assertEqual(("lanefather", "events"), (state.events_filter.source, state.focus))

    def test_r_shows_every_event_as_logged_and_back(self) -> None:
        state = self.keys(self.state(), "r")
        self.assertTrue(state.events_filter.raw)
        self.assertFalse(self.keys(state, "r").events_filter.raw)

    def test_slash_types_into_the_field_enter_keeps_it_and_esc_clears_it(self) -> None:
        state = self.keys(self.state(), "/")
        self.assertTrue(state.events_filter.editing)
        self.assertEqual("filter", self.tool.mode(state))
        state = self.keys(state, "7", "1", "q", "\x7f", "0")
        self.assertEqual("710", state.events_filter.text, "q types, it does not quit; backspace deletes")
        state = self.keys(state, "\r")
        self.assertEqual(("710", False), (state.events_filter.text, state.events_filter.editing))
        state = self.keys(state, "f", "/", "esc")
        self.assertEqual(self.tool.Filter("lanefather"), state.events_filter, "esc clears the text, not the log")

    def test_esc_clears_a_filter_enter_kept(self) -> None:
        state = self.keys(self.state(), "f", "/", "7", "\r")
        self.assertEqual(self.tool.Filter("lanefather", "7"), state.events_filter)
        state = self.keys(state, "esc")
        self.assertEqual(self.tool.Filter("lanefather"), state.events_filter, "the text goes, the log stays")

    def test_typed_text_stays_as_typed_on_another_layout(self) -> None:
        state = self.keys(self.state(), "/", "й")
        self.assertEqual("й", state.events_filter.text)

    def test_f_and_slash_do_nothing_where_events_is_not_drawn(self) -> None:
        state = dataclasses.replace(self.state(), frame=self.tool.Frame())
        self.assertEqual(self.tool.Filter(), self.keys(state, "f", "/", "r").events_filter)

    def test_new_rows_count_only_what_the_log_lets_through_and_a_switch_keeps_the_pin(self) -> None:
        rows = rows_of(mixed(), TAGS)
        state = self.keys(self.state(rows[:2], scroll=1), "f")  # LANES LOG, from its top (fathers#88 review 8)
        self.assertEqual(("lanefather", None), (state.events_filter.source, state.scroll.get("events")))
        state = dataclasses.replace(state, scroll={**state.scroll, "events": 1})  # then scrolled down
        later = rows_of(mixed() + [logged("lane.landed", NOW + dt.timedelta(seconds=5), source="lanefather",
                                          ident="710", onto="main"),
                                   logged("entry.owned", NOW + dt.timedelta(seconds=6), "x", 9, source="junkfather")],
                        TAGS)
        moved = self.tool.follow_events(state, later)
        self.assertEqual(3, moved.fresh_events, "gate, queue.updated and landed, not the slot and junk rows")
        pinned = self.keys(self.state(scroll=0), "f", "f", "f", "f")  # around, back to SLOTS LOG
        self.assertEqual((0, 0), (pinned.scroll.get("events", 0), pinned.fresh_events))
        self.assertEqual(rows_of(mixed(), TAGS)[0]["key"], pinned.newest, "the newest run, by its grant")


class LiveFilterTests(unittest.TestCase):
    """The log switch, `/`, Esc and scrolling in the live dashboard, in a terminal, over a copy of the mixed fixture
    whose logs gain lines while it runs."""

    def test_keys_switch_the_live_log_and_new_rows_count_through_it(self) -> None:
        import shutil
        import sys
        import tempfile
        from pathlib import Path

        import terminal

        tool = goldens_tool()
        folder = Path(tempfile.mkdtemp(prefix="godfather-feed-"))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        shutil.copytree(tool.MIXED, folder / "mixed")
        root = folder / "mixed"

        def append(source: str, kind: str, second: int, ident: str, **details: Any) -> None:
            line = json.dumps({"v": 1, "time": f"2026-11-04T10:00:{second:02d}.000Z", "host": "mbp", "source": source,
                               "kind": kind, "subject": {"type": "lane", "id": ident, "pid": 4242},
                               "actor": {"type": "tool"}, "details": details or {"status": "green"}})
            with open(root / source / "events.jsonl", "a") as handle:
                handle.write(line + "\n")

        with tool.environment(None, root):
            screen = terminal.Terminal(self, dict(__import__("os").environ), 43, 156,
                                       [sys.executable, str(terminal.ENTRY), "--interval", "0.2"])
            screen.receive("◂ SLOTS LOG ▸", "running")
            at = screen.mark()
            screen.write("f")
            screen.receive("◂ LANES LOG ▸", "710-speech-paste-phase", since=at)
            at = screen.mark()
            screen.write("/zzz")
            screen.receive("╰─ / zzz_ ", 'LANES LOG ▸ ── "zzz"', "No events match the filter", since=at)
            at = screen.mark()
            screen.write("\x1b")
            screen.receive("◂ LANES LOG ▸ ─", since=at)
            at = screen.mark()
            screen.write("fff")  # PROCS LOG, ALL LOG, back to SLOTS LOG
            screen.receive("◂ SLOTS LOG ▸", since=at)
            screen.write("\t")  # EVENTS focused, scrolled down by the wheel (↑↓ move its cursor, fathers#152)
            screen.write("\x1b[<65;20;30M")
            at = screen.mark()
            append("lanefather", "lane.checked", 1, "710-speech-paste-phase")
            append("slotfather", "lease.granted", 2, "0000000999-4242", count=1, waited=0)
            screen.receive("↑ 1 new", since=at)  # the run, not the lane row this log hides
            at = screen.mark()
            screen.write("g")
            screen.until(lambda text: "SLOTS LOG" in text and "↑ 1 new" not in text, what="the count gone at the top",
                         since=at)
            at = screen.mark()
            screen.write("\x1b[C")  # → while EVENTS has the focus: LANES LOG
            screen.receive("◂ LANES LOG ▸", since=at)
            self.assertEqual(0, screen.quit())


if __name__ == "__main__":
    unittest.main()
