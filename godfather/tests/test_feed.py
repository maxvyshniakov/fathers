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
        self.assertEqual([("slotfather", "slot")], model["sources"])

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
                         (model["events"], model["sources"], model["events_note"]),
                         "no fallback to the snapshot's described events")

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

        found = rows_of([logged("sweep.finished", NOW, source="junkfather", level="error", reaped=2,
                                errors=["x"], nested={"a": 1})])
        self.assertEqual(("sweep.finished", [("reaped=2 errors=x", "bad")]), (found[0]["event"], found[0]["detail"]))
        self.assertEqual("reason=x", events.compact({"message": "prose", "reason": "x"}))
        self.assertEqual("prose", events.compact({"message": "prose"}))


TAGS = {"slotfather": "slot", "lanefather": "lane", "junkfather": "junk"}
SOURCES = [("slotfather", "slot"), ("lanefather", "lane"), ("junkfather", "junk")]


class MixedViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tool: Any = _view.fresh()
        self.addCleanup(_view.fresh)

    def draw(self, width: int, found: Any = None, rows: Any = None, height: int = 12) -> tuple[Any, list[str]]:
        canvas = self.tool.Canvas(width, height)
        model = {"events": rows if rows is not None else rows_of(mixed(), TAGS), "sources": SOURCES}
        self.tool.draw_events(canvas, Rect(0, 0, height, width), model, NOW, found)
        return canvas, canvas.render(False).splitlines()

    def test_other_sources_come_between_slotfathers_rows_in_time_order_with_their_tag_and_colour(self) -> None:
        canvas, lines = self.draw(120)
        header = lines[0]
        self.assertIn("EVENTS ── all sources ──", header, "the note takes the place of the source and event labels")
        places = [header.index(title) for title in ("all sources", "slots", "label", "detail", "pid")]
        self.assertEqual(sorted(places), places)
        body = [line for line in lines[2:-1] if line.strip("│ ")]
        self.assertEqual(["lane", "lane", "slot", "junk", "lane", "slot"], [line.split()[2] for line in body])
        self.assertRegex(body[3], r"junk +owned +vite dev +ttl 2h00m +90412")
        self.assertRegex(body[4], r"lane +checked +710-speech +green · 2 suites")
        self.assertRegex(body[0], r"lane +queue\.upd… +Straylight +reason=policy moved=2", "an unknown kind")
        self.assertRegex(body[1], r"lane +gate +Straylight cedf8b972d +red · 19m")
        self.assertRegex(body[2], r"slot +released +▫ +build +ok 30s +101")
        for row, tag in ((2, "lane"), (4, "slot"), (5, "junk")):
            column = lines[row].index(f" {tag} ") + 1
            self.assertEqual(f"source-{tag}", canvas.cells[row][column][1])
        y = next(n for n, line in enumerate(lines) if " gate " in line)
        self.assertEqual("bad", canvas.cells[y][lines[y].index("red · 19m")][1])

    def test_narrow_panels_drop_pid_then_slots_then_the_source_shrinks_and_the_label_goes_first(self) -> None:
        rows = rows_of(mixed(), TAGS)
        results = [self.tool.event_outcome(row, self.tool.EMOJI) for row in rows]

        def widths(inner: int) -> dict[str, int]:
            return {key: size for key, _, size, _, _ in self.tool.columns(inner, rows, results, True, True)}

        wide = widths(114)
        self.assertEqual(["time", "source", "event", "slots", "label", "detail", "pid"], list(wide))
        def lost(step: int, found: dict[str, int]) -> bool:
            return ("pid" not in found, "slots" not in found, found["source"] == 1)[step]

        steps = [next(inner for inner in range(114, 20, -1) if lost(step, widths(inner))) for step in range(3)]
        self.assertEqual(sorted(steps, reverse=True), steps, "pid goes, then slots, then the source shrinks")
        for inner in (steps[0] + 1, steps[1] + 1, steps[2] + 1):
            self.assertEqual(0, wide["label"], "the label is the flexible one")
            found = widths(inner)
            label = inner - sum(found.values()) - 2 * (len(found) - 1)
            self.assertGreaterEqual(label, 10, "each step only once the label is down to its least")
        _, lines = self.draw(steps[2] + 6)
        self.assertRegex(next(line for line in lines if "vite" in line), r"\d +j +owned +vite dev")
        for width in range(30, 121):
            for line in self.draw(width)[1]:
                self.assertEqual(width, len(line), f"nothing drawn past the panel at {width}")

    def test_the_source_filter_and_the_text_filter(self) -> None:
        Filter = self.tool.Filter
        _, lines = self.draw(120, Filter("lanefather"))
        self.assertIn("EVENTS ── lanefather ──", lines[0])
        self.assertEqual({"lane"}, {line.split()[2] for line in lines[2:-1] if line.strip("│ ")})
        _, lines = self.draw(120, Filter(None, "VITE"))
        self.assertIn('EVENTS ── all sources · "VITE" ──', lines[0])
        self.assertEqual(["junk"], [line.split()[2] for line in lines[2:-1] if line.strip("│ ")], "label, any case")
        for needle, kind in (("cedf8b", "gate"), ("suites", "checked"), ("entry.owned", "owned")):
            body = [line for line in self.draw(120, Filter(None, needle))[1][2:-1] if line.strip("│ ")]
            self.assertEqual([kind], [line.split()[3] for line in body], f"{needle}: subject id, detail and kind")
        _, lines = self.draw(120, Filter(None, "nothing"))
        self.assertIn("No events match the filter", lines[2])
        _, lines = self.draw(120, Filter("lanefather", "71", editing=True))
        self.assertTrue(lines[-1].startswith("╰─ / 71_ ─"), lines[-1])

    def test_one_source_draws_no_note_until_a_filter_is_set(self) -> None:
        canvas = self.tool.Canvas(100, 8)
        rows = rows_of(mixed()[:1])
        model = {"events": rows, "sources": SOURCES[:1]}
        self.tool.draw_events(canvas, Rect(0, 0, 8, 100), model, NOW)
        self.assertNotIn("all sources", canvas.render(False))
        self.tool.draw_events(canvas, Rect(0, 0, 8, 100), model, NOW, self.tool.Filter(None, "bu"))
        self.assertIn('EVENTS ── "bu" ──', canvas.render(False))

    def test_a_slotfather_without_an_event_log_says_so_and_shows_none(self) -> None:
        canvas = self.tool.Canvas(100, 8)
        self.tool.draw_events(canvas, Rect(0, 0, 8, 100), {"events": [], "sources": [],
                                                          "events_note": "slotfather too old for events"}, NOW)
        lines = canvas.render(False).splitlines()
        self.assertIn("EVENTS ── slotfather too old for events ──", lines[0])
        self.assertEqual("", "".join(line.strip("│ ") for line in lines[1:-1]))

    def test_the_help_lists_f_and_slash_and_the_footer_hints_stay(self) -> None:
        keys = dict(entry[:2] for entry in self.tool.HELP[0][1])
        self.assertIn("f", keys)
        self.assertIn("/", keys)
        self.assertEqual(("↑↓", "tab", "s", "?", "q"), tuple(key for key, _ in self.tool.FOOTER_HINTS))


class FilterReducerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tool: Any = _view.fresh()
        self.addCleanup(_view.fresh)

    def state(self, rows: Any = None, scroll: int = 0) -> Any:
        tool = self.tool
        model = {"holders": [], "waiters": [], "events": rows if rows is not None else rows_of(mixed(), TAGS),
                 "sources": SOURCES}
        canvas = tool.Canvas(156, 43)
        canvas.tables["events"] = dict(x=0, y=0, width=90, height=20, top=2, visible=16, count=6, offset=0,
                                       base=None, row_items=None)
        state = dataclasses.replace(tool.AppState(model=model), frame=frame_of(canvas), scroll={"events": scroll})
        return tool.follow_events(state, model["events"])

    def keys(self, state: Any, *keys: str) -> Any:
        for key in keys:
            state, _ = self.tool.reduce(state, self.tool.Key(key), 0.0)
        return state

    def test_f_cycles_every_source_in_order_then_all(self) -> None:
        state = self.state()
        seen: list[Any] = []
        for _ in range(4):
            state = self.keys(state, "f")
            seen.append(state.events_filter.source)
        self.assertEqual(["slotfather", "lanefather", "junkfather", None], seen)

    def test_slash_types_into_the_field_enter_keeps_it_and_esc_clears_it(self) -> None:
        state = self.keys(self.state(), "/")
        self.assertTrue(state.events_filter.editing)
        self.assertEqual("filter", self.tool.mode(state))
        state = self.keys(state, "7", "1", "q", "\x7f", "0")
        self.assertEqual("710", state.events_filter.text, "q types, it does not quit; backspace deletes")
        state = self.keys(state, "\r")
        self.assertEqual(("710", False), (state.events_filter.text, state.events_filter.editing))
        state = self.keys(state, "f", "/", "esc")
        self.assertEqual(self.tool.Filter("slotfather"), state.events_filter, "esc clears the text, not the source")

    def test_typed_text_stays_as_typed_on_another_layout(self) -> None:
        state = self.keys(self.state(), "/", "й")
        self.assertEqual("й", state.events_filter.text)

    def test_f_and_slash_do_nothing_where_events_is_not_drawn(self) -> None:
        state = dataclasses.replace(self.state(), frame=self.tool.Frame())
        self.assertEqual(self.tool.Filter(), self.keys(state, "f", "/").events_filter)

    def test_new_rows_count_only_what_the_filter_lets_through_and_a_filter_change_keeps_the_pin(self) -> None:
        rows = rows_of(mixed(), TAGS)
        state = self.keys(self.state(rows[:2], scroll=1), "f", "f")  # lanefather, scrolled down
        self.assertEqual("lanefather", state.events_filter.source)
        later = rows_of(mixed() + [logged("lane.landed", NOW + dt.timedelta(seconds=5), source="lanefather",
                                          ident="710", onto="main"),
                                   logged("entry.owned", NOW + dt.timedelta(seconds=6), "x", 9, source="junkfather")],
                        TAGS)
        moved = self.tool.follow_events(state, later)
        self.assertEqual(3, moved.fresh_events, "gate, queue.updated and landed, not the slot and junk rows")
        pinned = self.keys(self.state(scroll=0), "f")
        self.assertEqual((0, 0), (pinned.scroll.get("events", 0), pinned.fresh_events))
        self.assertEqual(rows_of(mixed(), TAGS)[3]["key"], pinned.newest, "the newest Slotfather row")


class LiveFilterTests(unittest.TestCase):
    """`f`, `/`, Esc and scrolling in the live dashboard, in a terminal, over a copy of the mixed fixture whose logs
    gain lines while it runs."""

    def test_keys_filter_the_live_feed_and_new_rows_count_through_the_filter(self) -> None:
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

        def append(source: str, kind: str, second: int, ident: str) -> None:
            line = json.dumps({"v": 1, "time": f"2026-11-04T10:00:{second:02d}.000Z", "host": "mbp", "source": source,
                               "kind": kind, "subject": {"type": "lane", "id": ident}, "actor": {"type": "tool"},
                               "details": {"status": "green"}})
            with open(root / source / "events.jsonl", "a") as handle:
                handle.write(line + "\n")

        with tool.environment(None, root):
            screen = terminal.Terminal(self, dict(__import__("os").environ), 43, 156,
                                       [sys.executable, str(terminal.ENTRY), "--interval", "0.2"])
            screen.receive("EVENTS ── all sources ──", "vite dev")
            at = screen.mark()
            screen.write("f")
            screen.receive("EVENTS ── slotfather ──", since=at)
            at = screen.mark()
            screen.write("/vite")
            screen.receive("╰─ / vite_ ", 'slotfather · "vite"', "No events match the filter", since=at)
            at = screen.mark()
            screen.write("\x1b")
            screen.receive("EVENTS ── slotfather ──", since=at)
            screen.write("\t")  # EVENTS focused, scrolled down one row
            screen.write("j")
            at = screen.mark()
            append("lanefather", "lane.checked", 1, "710-speech-paste-phase")
            append("slotfather", "lease.granted", 2, "0000000999-4242")
            screen.receive("↑ 1 new", since=at)  # the slot row, not the lane one the filter hides
            at = screen.mark()
            screen.write("g")
            screen.until(lambda text: "EVENTS" in text and "↑ 1 new" not in text, what="the count gone at the top",
                         since=at)
            at = screen.mark()
            screen.write("ff")  # lanefather, then junkfather
            screen.receive("EVENTS ━━ junkfather ━━", "vite dev", since=at)  # focused: heavy rules
            self.assertEqual(0, screen.quit())


if __name__ == "__main__":
    unittest.main()
