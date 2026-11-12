"""The app state and its one reducer (fathers#90): every mode's keys from the mode table, the overlay as one value
(fathers#17), the selection by entry identity (fathers#21), the details' load states (fathers#23), notices apart from
the refresh state (fathers#24), and the audit's repro cases: the focus while details load, a Settings conflict, Esc
in a small panel."""
from __future__ import annotations

import dataclasses
import datetime
import unittest
from typing import Any, Callable, Optional, cast

import helpers  # noqa: F401 - src/ on sys.path
import view as _view
from test_settings import Clock, PanelCase, idle

from godfather.layout import Rect


def entry(n: int, kind: str = "held", since: Any = None, **fields: Any) -> dict[str, Any]:
    now = datetime.datetime.now(datetime.timezone.utc)
    return dict(dict(label=f"job{n}", kind=kind, count=1, pid=10 + n, style="h0", cpu=50, since=now,
                     granted=None if kind == "waiting" else now, rss=1024, procs=1, queue="", cmd="work", tree=[],
                     key=(10 + n, since), priority="normal", ticket=None, meta="", context={}, workdir="",
                     yieldable=False, yields=0, asked="", asked_at=None, idle_since=None), **fields)


def model(holders: list[dict[str, Any]], waiters: Optional[list[dict[str, Any]]] = None, **fields: Any) -> dict[str, Any]:
    now = datetime.datetime.now(datetime.timezone.utc)
    return dict(dict(holders=holders, waiters=waiters or [], slots=8, cores=12, saved=False, averages=(1.0, 1.0, 1.0),
                     advice=dict(unleased_load=0, jobs=4, capacity=8), admission=3, memory_block=None,
                     memory=dict(level=1, used=512, total=1024, swap_total=0, swap_free=None, low=False), history=[],
                     events=[], updated=now, order="backfill", start=None, limit=2700, used=len(holders),
                     free=8 - len(holders)), **fields)


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self.tool: Any = _view.fresh()
        self.addCleanup(_view.fresh)

    def screen(self, found: Optional[dict[str, Any]] = None, width: int = 156, height: int = 43) -> Any:
        """An app state over a drawn overview of `found` (three running, two waiting by default)."""
        found = found if found is not None else model([entry(n) for n in range(3)],
                                                      [entry(n, "waiting") for n in range(3, 5)])
        state, _ = self.tool.reduce(self.tool.AppState(), self.tool.ModelLoaded(found), 0.0)
        return self.draw(state, width, height)

    def draw(self, state: Any, width: int = 156, height: int = 43) -> Any:
        canvas = self.tool.Canvas(width, height)
        self.tool.render(canvas, state, 0.0)
        state, _ = self.tool.reduce(state, self.tool.Drawn(helpers.frame_of(canvas)), 0.0)
        return state

    def send(self, state: Any, *events: Any, now: float = 0.0) -> tuple[Any, list[str]]:
        kinds: list[str] = []
        for event in events:
            state, effects = self.tool.reduce(state, event, now)
            kinds += [effect.kind for effect in effects if effect.kind != "load-details"]
        return state, kinds

    def keys(self, state: Any, *keys: str) -> tuple[Any, list[str]]:
        return self.send(state, *(self.tool.Key(key) for key in keys))


def changed(before: Any, after: Any) -> set[str]:
    """The AppState fields an event changed."""
    return {name.name for name in dataclasses.fields(before) if getattr(before, name.name) != getattr(after, name.name)}


Check = Callable[[Any], object]
Row = tuple[set[str], list[str], Check]


class ModeTableTests(Case):
    """Each (mode, key) pair has exactly one handler, and this table says what each does and that nothing else
    changes. A binding added to modes.TABLE without a row here fails test_every_binding_is_described."""

    def expected(self) -> dict[str, dict[str, Row]]:
        def selected(index: int) -> Check:
            return lambda state: state.selection.index == index

        def nothing(_: Any) -> object:
            return True

        def overlay(kind: Optional[str], **fields: int) -> Check:
            return lambda state: (state.overlay is None if kind is None else state.overlay.kind == kind
                                  and all(getattr(state.overlay, name) == value for name, value in fields.items()))

        def focus(name: str) -> Check:
            return lambda state: state.focus == name

        def scrolled(offset: int) -> Check:
            return lambda state: state.scroll.get("events") == offset

        def scrolled_far(state: Any) -> object:
            return state.overlay.scroll > 3

        def in_changelog(state: Any) -> object:
            return state.overlay.kind == "changelog"

        quit_: Row = (set[str](), ["quit"], nothing)
        none: Row = (set[str](), [], nothing)
        main: dict[str, Row] = {
            "q": quit_, "Q": quit_,
            "\t": ({"focus"}, [], focus("events")), "back_tab": ({"focus"}, [], focus("details")),
            "?": ({"overlay"}, [], overlay("help")), "v": ({"overlay"}, ["load-news"], overlay("changelog")),
            "s": (set[str](), ["open-settings"], nothing),
            "j": ({"selection"}, [], selected(3)), "down": ({"selection"}, [], selected(3)),
            "k": ({"selection"}, [], selected(1)), "up": ({"selection"}, [], selected(1)),
            "g": ({"selection"}, [], selected(0)), "home": ({"selection"}, [], selected(0)),
            "page_up": ({"selection"}, [], selected(0)),
            "G": ({"selection"}, [], selected(4)), "end": ({"selection"}, [], selected(4)),
            "page_down": ({"selection"}, [], selected(4)),
            "esc": none, "x": none,
        }
        events = self.start("panel").frame.tables["events"]
        page, last = events["visible"] - 1, events["count"] - events["visible"]
        panel: dict[str, Row] = {**main, "\t": ({"focus"}, [], focus("details")), "back_tab": ({"focus"}, [], focus("requests")),
                 **{key: ({"scroll", "glow"}, [], scrolled(offset)) for key, offset in (
                     ("j", 4), ("down", 4), ("k", 2), ("up", 2), ("page_down", min(last, 3 + page)), ("page_up", 0),
                     ("g", 0), ("home", 0), ("G", last), ("end", last))}}
        overlay_scroll: dict[str, Row] = {key: ({"overlay", "glow"}, [], overlay("help", scroll=offset))
                          for key, offset in (("j", 4), ("down", 4), ("k", 2), ("up", 2), ("g", 0), ("home", 0),
                                              (" ", 3 + 35), ("page_down", 3 + 35), ("page_up", 0))}
        help_: dict[str, Row] = {
                 **overlay_scroll, "G": ({"overlay", "glow"}, [], scrolled_far),
                 "end": ({"overlay", "glow"}, [], scrolled_far),
                 "?": ({"overlay"}, [], overlay(None)), "esc": ({"overlay"}, [], overlay(None)),
                 "v": ({"overlay"}, ["load-news"], overlay("changelog", scroll=0)), "q": quit_, "Q": quit_,
                 "s": none, "\t": none, "x": none}
        changelog: dict[str, Row] = {key: (fields, effects, in_changelog if fields else check)
                                     for key, (fields, effects, check) in help_.items()}
        changelog.update({  # closing what's new, to help too, marks everything read
            "v": ({"overlay", "read"}, ["save-read"], overlay(None)),
            "esc": ({"overlay", "read"}, ["save-read"], overlay(None)),
            "?": ({"overlay", "read"}, ["save-read"], overlay("help", scroll=0)),
            "left": ({"overlay"}, [], overlay("changelog", tab=1, scroll=0)),
            "h": ({"overlay"}, [], overlay("changelog", tab=1, scroll=0)),
            "back_tab": ({"overlay"}, [], overlay("changelog", tab=1, scroll=0)),
            "right": ({"overlay"}, [], overlay("changelog", tab=1, scroll=0)),
            "l": ({"overlay"}, [], overlay("changelog", tab=1, scroll=0)),
            "\t": ({"overlay"}, [], overlay("changelog", tab=1, scroll=0)),
            "q": ({"read"}, ["save-read", "quit"], nothing), "Q": ({"read"}, ["save-read", "quit"], nothing)})
        return {"main": main, "panel": panel, "help": help_, "changelog": changelog}

    def start(self, mode: str) -> Any:
        """Each mode's state: QUEUE focused on the middle of five entries; EVENTS focused, scrolled to 3 of 40
        events; help and what's new scrolled to 3 (what's new with one tool's entries: two filters)."""
        tool = self.tool
        events = [dict(when=datetime.datetime(2026, 11, 12, tzinfo=datetime.timezone.utc)
                       + datetime.timedelta(seconds=n), verb="granted", count=1, label=f"e{n}", pid=n, delta=1)
                  for n in range(40)]
        state = self.screen(model([entry(n) for n in range(3)], [entry(n, "waiting") for n in range(3, 5)],
                                  events=events), 156, 43)
        state = tool.move(state, 2)
        from godfather import news
        state = dataclasses.replace(state, news=news.News((), (news.Item("slotfather", "1.0.0", "2026-11-01", "Added",
                                                                         "a"),)))
        if mode == "panel":
            state = dataclasses.replace(state, focus="events", scroll={"events": 3})
        elif mode == "help":
            state = dataclasses.replace(state, overlay=tool.Help(scroll=3))
        elif mode == "changelog":
            state = dataclasses.replace(state, overlay=tool.Changelog(scroll=3))
        self.assertEqual(mode, tool.mode(state))
        return state

    def test_each_mode_and_key_does_one_thing_and_nothing_else(self) -> None:
        for mode, table in self.expected().items():
            for key, (fields, effects, check) in table.items():
                with self.subTest(mode=mode, key=key):
                    before = self.start(mode)
                    after, kinds = self.send(before, self.tool.Key(key), now=1.0)
                    self.assertEqual(effects, kinds)
                    self.assertEqual(fields, changed(before, after))
                    self.assertTrue(check(after))

    def test_every_binding_is_described(self) -> None:
        expected = self.expected()
        for mode, table in self.tool.TABLE.items():
            with self.subTest(mode=mode):
                self.assertLessEqual(set(table), set(expected[mode]), "a binding the table above does not describe")

    def test_hotkeys_by_position_in_every_mode(self) -> None:
        for mode, key, meant in (("main", "й", "q"), ("main", "о", "j"), ("help", ",", "?"), ("changelog", "м", "v")):
            with self.subTest(mode=mode, key=key):
                state = self.start(mode)
                self.assertEqual(self.send(state, self.tool.Key(meant))[1], self.send(state, self.tool.Key(key))[1])
                self.assertEqual(self.send(state, self.tool.Key(meant))[0], self.send(state, self.tool.Key(key))[0])

    def test_a_click_on_a_footer_hint_is_its_key_in_every_mode(self) -> None:
        state = self.start("main")
        canvas = self.tool.Canvas(156, 43)
        self.tool.render(canvas, state, 0.0)
        row, start, _, _ = next(hint for hint in canvas.hint_hits if hint[3] == "?")
        after, _ = self.send(state, self.tool.Mouse(0, start, row, True))
        self.assertEqual("help", after.overlay.kind)


class StubPanel:
    """A Settings panel that answers every key and click with one action, to drive the Settings mode."""

    def __init__(self, action: str, editing: bool = False, theme: str = "auto", interval: float = 1.0) -> None:
        self.action, self.editing, self.theme, self.interval = action, editing, theme, interval
        self.offset, self.pressed = 0, cast(list[Any], [])

    def key(self, key: str) -> str:
        self.pressed.append(key)
        return self.action

    def mouse(self, event: tuple[str, int, int, int, bool]) -> str:
        self.pressed.append(event)
        return self.action

    def tick(self) -> bool:
        return False


class SettingsModeTableTests(Case):
    """The Settings mode: every key and click goes to the panel, translated to its hotkey unless a value is typed,
    and its answer does one thing to the app."""

    def test_each_answer_of_the_panel(self) -> None:
        tool = self.tool
        cases: list[tuple[str, dict[str, Any], Optional[str], list[str], float, str]] = [
            # (answer, panel fields, overlay kind after, effects, Refresh every, theme)
            ("handled", {}, "settings", ["remember"], 1.0, "auto"),
            ("changed", {}, "settings", ["remember", "load-model"], 1.0, "auto"),
            ("close", {}, None, ["remember", "repaint"], 1.0, "auto"),
            ("quit", {}, "settings", ["quit"], 1.0, "auto"),
            ("handled", {"interval": 2.0}, "settings", ["remember"], 2.0, "auto"),
            ("handled", {"theme": "builtin"}, "settings", ["apply-theme", "repaint", "remember"], 1.0, "builtin"),
        ]
        for answer, fields, kind, effects, interval, theme in cases:
            for event in (tool.Key("x"), tool.Mouse(0, 5, 5, True)):
                with self.subTest(answer=answer, fields=fields, event=type(event).__name__):
                    panel = StubPanel(answer, **fields)
                    state = dataclasses.replace(self.screen(), overlay=tool.SettingsOverlay(panel))
                    after, kinds = self.send(state, event)
                    self.assertEqual(kind, after.overlay.kind if after.overlay else None)
                    self.assertEqual(effects, kinds)
                    self.assertEqual((interval, theme), (after.interval, after.theme))
                    self.assertEqual(1, len(panel.pressed), "the panel got it, once")

    def test_keys_are_hotkeys_unless_a_value_is_typed(self) -> None:
        tool = self.tool
        for editing, expected in ((False, "s"), (True, "ы")):
            panel = StubPanel("handled", editing=editing)
            self.send(dataclasses.replace(self.screen(), overlay=tool.SettingsOverlay(panel)), tool.Key("ы"))
            self.assertEqual([expected], panel.pressed)

    def test_a_click_on_a_footer_hint_is_the_hint_key_for_the_panel_too(self) -> None:
        tool = self.tool
        state = self.screen()
        row, start, _, _ = state.frame.hints[0]
        panel = StubPanel("handled")
        self.send(dataclasses.replace(state, overlay=tool.SettingsOverlay(panel)), tool.Mouse(0, start, row, True))
        self.assertEqual([state.frame.hints[0][3]], panel.pressed)


class MouseTableTests(Case):
    """The mouse in each mode, by button and where it lands."""

    def test_each_mode_button_and_place(self) -> None:
        tool = self.tool
        events = [dict(when=datetime.datetime(2026, 11, 12, tzinfo=datetime.timezone.utc)
                       + datetime.timedelta(seconds=n), verb="granted", count=1, label=f"e{n}", pid=n, delta=1)
                  for n in range(40)]
        base = tool.move(self.screen(model([entry(n) for n in range(3)], [entry(n, "waiting") for n in range(3, 5)],
                                           events=events)), 2)
        queue, details, evs = (base.frame.tables[name] for name in ("requests", "details", "events"))
        version = base.frame.version_at
        gap = queue["top"] + 3  # the row between running and waiting
        help_ = dataclasses.replace(base, overlay=tool.Help(scroll=3))
        box = tool.overlay_box(help_, help_.overlay)
        inside, outside = (box.x + 3, box.y + 3), (0, 0)
        cases: list[tuple[str, Any, tuple[int, int, int, bool], Callable[[Any], object]]] = [
            ("a click on a running row selects it", base, (0, 10, queue["top"], True),
             lambda state: state.selection.index == 0),
            ("a click on a waiting row selects it", base, (0, 10, queue["top"] + 4, True),
             lambda state: state.selection.index == 3),
            ("the gap selects nothing", base, (0, 10, gap, True), lambda state: state.selection.index == 2),
            ("a padding row selects nothing", base, (0, 10, queue["top"] - 1, True),
             lambda state: state.selection.index == 2),
            ("a release does nothing", base, (0, 10, queue["top"], False), lambda state: state == base),
            ("a click on DETAILS focuses it", base, (0, details["x"] + 5, details["y"] + 5, True),
             lambda state: state.focus == "details" and state.selection.index == 2),
            ("the wheel on EVENTS scrolls it, not the selection", base, (65, 10, evs["y"] + 3, True),
             lambda state: state.scroll.get("events") == 3 and state.selection.index == 2
             and "events" in state.glow),
            ("the wheel up at the top stays there", base, (64, 10, evs["y"] + 3, True),
             lambda state: state.scroll.get("events") == 0),
            ("Ctrl+wheel is the wheel", base, (65 | 16, 10, evs["y"] + 3, True),
             lambda state: state.scroll.get("events") == 3),
            ("what's new in the footer opens it", base, (0, version[1], version[0], True),
             lambda state: state.overlay == tool.Changelog()),
            ("the wheel in help scrolls it", help_, (65, *inside, True), lambda state: state.overlay.scroll == 6),
            ("a click in help does nothing", help_, (0, *inside, True), lambda state: state == help_),
            ("a click outside help closes it", help_, (0, *outside, True), lambda state: state.overlay is None),
            ("the wheel outside help does nothing", help_, (65, *outside, True), lambda state: state == help_),
            ("a release outside help does nothing", help_, (0, *outside, False), lambda state: state == help_),
        ]
        for what, state, (button, x, y, pressed), check in cases:
            with self.subTest(what):
                self.assertTrue(check(self.send(state, tool.Mouse(button, x, y, pressed), now=1.0)[0]))


class OverlayTests(Case):
    """fathers#17: one overlay at a time, each with its own scroll."""

    def test_question_mark_and_v_switch_between_help_and_the_changelog(self) -> None:
        state = self.screen()
        state, _ = self.keys(state, "?")
        self.assertEqual("help", state.overlay.kind)
        state, _ = self.keys(state, "v")
        self.assertEqual("changelog", state.overlay.kind, "v in help: the changelog")
        state, _ = self.keys(state, "?")
        self.assertEqual("help", state.overlay.kind, "? in the changelog: help")
        state, _ = self.keys(state, "?")
        self.assertIsNone(state.overlay)

    def test_keys_after_v_in_the_same_batch_scroll_the_changelog(self) -> None:
        for opener, kind in (("v", "changelog"), ("?", "help")):
            with self.subTest(opener=opener):
                state, _ = self.keys(self.screen(), opener, "j", "j")  # never drawn between them
                self.assertEqual((kind, 2), (state.overlay.kind, state.overlay.scroll))

    def test_with_one_filter_the_tab_keys_go_back_to_its_top(self) -> None:
        from godfather import news
        state = dataclasses.replace(self.screen(), news=news.News((), ()))
        state, _ = self.keys(state, "v", "j", "j", "right")
        self.assertEqual(self.tool.Changelog(tab=0, scroll=0), state.overlay)


class WhatsNewTests(Case):
    """fathers#102: what's new opens (asking every tool again), filters by tool, and closing it marks it read."""

    def news(self) -> Any:
        from godfather import news
        return news.News((news.Installed("slotfather", "0.2.0", "ok"),), (
            news.Item("slotfather", "0.2.0", "2026-11-12", "Added", "s2"),
            news.Item("junkfather", "0.1.0", "2026-11-12", "Added", "j1"),
            news.Item("slotfather", "0.1.0", "2026-11-01", "Added", "s1")))

    def test_opening_asks_for_the_news_and_a_reply_replaces_it(self) -> None:
        state, effects = self.keys(self.screen(), "v")
        self.assertEqual((self.tool.Changelog(), ["load-news"]), (state.overlay, effects))
        found = self.news()
        state, effects = self.send(state, self.tool.NewsLoaded(found))
        self.assertEqual((found, [], self.tool.Changelog()), (state.news, effects, state.overlay))
        self.assertTrue(self.tool.unread(state), "nothing read yet")

    def test_the_filter_steps_through_all_and_each_tool_with_entries(self) -> None:
        state = dataclasses.replace(self.screen(), news=self.news())
        tabs: list[int] = []
        for key in ("v", "right", "right", "right", "left"):
            state, _ = self.keys(state, key)
            tabs.append(state.overlay.tab)
        self.assertEqual([0, 1, 2, 0, 2], tabs, "all, slotfather, junkfather, then round")
        state, _ = self.keys(state, "j")
        state, _ = self.keys(state, "\t")
        self.assertEqual(self.tool.Changelog(tab=0, scroll=0), state.overlay, "a new filter starts at its top")

    def test_closing_marks_everything_read_and_saves_it_once(self) -> None:
        for close in ("esc", "v", "?"):
            with self.subTest(close=close):
                state = dataclasses.replace(self.screen(), news=self.news(), read={"lanefather": "0.3.0"})
                state, _ = self.keys(state, "v")
                state, effects = self.keys(state, close)
                self.assertEqual(["save-read"], effects)
                self.assertEqual({"slotfather": "0.2.0", "junkfather": "0.1.0", "lanefather": "0.3.0"}, state.read)
                self.assertFalse(self.tool.unread(state))
                state, _ = self.keys(state, "v")
                self.assertEqual([], self.keys(state, "esc")[1], "nothing new: nothing to save")

    def test_quitting_from_whats_new_saves_it_read_first(self) -> None:
        state = dataclasses.replace(self.screen(), news=self.news())
        state, _ = self.keys(state, "v")
        state, effects = self.keys(state, "q")
        self.assertEqual(["save-read", "quit"], effects)
        self.assertFalse(self.tool.unread(state))

    def test_news_arriving_keeps_the_filter_on_its_tool(self) -> None:
        from godfather import news
        state = dataclasses.replace(self.screen(), news=self.news())
        state, _ = self.keys(state, "v", "right", "right")
        self.assertEqual(2, state.overlay.tab, "Junkfather")
        fewer = news.News((), tuple(item for item in self.news().items if item.tool == "junkfather"))
        state, _ = self.send(state, self.tool.NewsLoaded(fewer))
        self.assertEqual(1, state.overlay.tab, "still Junkfather, now the second filter")
        state, _ = self.send(state, self.tool.NewsLoaded(news.News((), ())))
        self.assertEqual(0, state.overlay.tab, "gone: back to all")

    def test_a_click_outside_closes_and_marks_read_too(self) -> None:
        state = dataclasses.replace(self.screen(), news=self.news())
        state, _ = self.keys(state, "v")
        state, effects = self.send(state, self.tool.Mouse(0, 0, 0, True))
        self.assertEqual((None, ["save-read"]), (state.overlay, effects))
        self.assertFalse(self.tool.unread(state))

    def test_news_arriving_after_it_closed_is_unread_again(self) -> None:
        from godfather import news
        state = dataclasses.replace(self.screen(), news=self.news())
        state, _ = self.keys(state, "v", "esc")
        newer = news.News((), (news.Item("slotfather", "0.3.0", "2026-11-13", "Fixed", "s3"), *self.news().items))
        state, _ = self.send(state, self.tool.NewsLoaded(newer))
        self.assertEqual(["s3"], [item.text for item in news.unread(state.news, state.read)])

    def test_a_refresh_that_moves_events_down_does_not_flash_its_thumb(self) -> None:
        tool = self.tool
        when = datetime.datetime(2026, 11, 12, tzinfo=datetime.timezone.utc)
        events = [dict(when=when + datetime.timedelta(seconds=n), verb="granted", count=1, label=f"e{n}", pid=n,
                       delta=1) for n in range(40)]
        state = self.screen(model([entry(0)], events=events))
        state = dataclasses.replace(state, scroll={"events": 5}, glow={})
        state, _ = self.send(state, tool.ModelLoaded(model([entry(0)], events=events + [
            dict(events[0], when=when + datetime.timedelta(seconds=99))])), now=2.0)
        self.assertEqual((6, {}), (state.scroll["events"], state.glow), "kept in place, and no flash")

    def test_each_overlay_owns_its_scroll_and_the_panels_keep_theirs(self) -> None:
        state = dataclasses.replace(self.screen(), scroll={"events": 2})
        state, _ = self.keys(state, "?", "j", "j", "j", "v")
        self.assertEqual(0, state.overlay.scroll, "the changelog opens at its top")
        state, _ = self.keys(state, "?")
        self.assertEqual(0, state.overlay.scroll, "help opens at its top again")
        self.assertEqual({"events": 2}, state.scroll)

    def test_s_v_and_a_version_click_while_settings_is_open(self) -> None:
        tool = self.tool
        panel = tool.Settings(1.0, local_only=True)
        state = self.screen()
        version = state.frame.version_at
        canvas = tool.Canvas(156, 43)
        panel.draw(canvas, state.model)
        state = dataclasses.replace(state, overlay=tool.SettingsOverlay(panel))
        after, kinds = self.keys(state, "v")
        self.assertEqual((state.overlay, ["remember"]), (after.overlay, kinds), "v does nothing in Settings")
        after, kinds = self.keys(state, "s")
        self.assertIsNone(after.overlay, "s closes Settings, as Esc does")
        self.assertIn("repaint", kinds)
        after, _ = self.send(state, tool.Mouse(0, version[1], version[0], True))
        self.assertIsNone(after.overlay, "a click on the version is a click outside: it closes Settings")

    def test_a_click_outside_help_closes_it_and_the_wheel_inside_scrolls_it(self) -> None:
        state, _ = self.keys(self.screen(), "?")
        view = self.tool.overlay_box(state, state.overlay)
        after, _ = self.send(state, self.tool.Mouse(65, view.x + 3, view.y + 3, True))
        self.assertEqual(3, after.overlay.scroll)
        after, _ = self.send(state, self.tool.Mouse(0, 0, 0, True))
        self.assertIsNone(after.overlay)
        after, _ = self.send(state, self.tool.Mouse(0, 0, 0, False))
        self.assertEqual(state.overlay, after.overlay, "a release outside does nothing")


class SelectionTests(Case):
    """fathers#21: the selection is the entry, not the row."""

    def reload(self, state: Any, found: dict[str, Any]) -> Any:
        return self.send(state, self.tool.ModelLoaded(found))[0]

    def test_it_follows_its_entry_through_a_reorder_a_grant_and_a_yield(self) -> None:
        a, b, c = entry(0), entry(1), entry(2, "waiting")
        state = self.tool.move(self.screen(model([a, b], [c])), 2)
        state = self.reload(state, model([b, a], [c]))
        self.assertEqual(2, state.selection.index, "a backfill reorder")
        state = self.reload(state, model([b, a, dict(c, kind="held")]))
        self.assertEqual(((12, None), 0), state.selection.key, "granted: the same entry, now running")
        self.assertEqual(2, state.selection.index)
        state = self.reload(state, model([b], [dict(c, kind="waiting", yields=1), a]))
        self.assertEqual(1, state.selection.index, "yielded, back in the queue")

    def test_the_first_model_selects_its_first_row(self) -> None:
        state, _ = self.send(self.tool.AppState(), self.tool.Tick())  # nothing to select yet
        state = self.reload(state, model([entry(0)], [entry(1, "waiting")]))
        self.assertEqual((0, "holders"), (state.selection.index, state.selection.group))

    def test_when_its_entry_ends_the_neighbour_in_its_group_takes_it(self) -> None:
        a, b, c, d = entry(0), entry(1), entry(2, "waiting"), entry(3, "waiting")
        state = self.tool.move(self.screen(model([a, b], [c, d])), 1)
        state = self.reload(state, model([a], [c, d]))
        self.assertEqual((0, "holders"), (state.selection.index, state.selection.group),
                         "b ended: a, its neighbour among the running, never the first waiting")
        state = self.tool.move(state, 2)  # d
        state = self.reload(state, model([a], [c]))
        self.assertEqual(1, state.selection.index, "d ended: c, the last waiting")
        state = self.reload(state, model([a]))
        self.assertEqual(0, state.selection.index, "nothing waits any more: the nearest row")

    def test_two_entries_with_one_key_are_two_selections_two_colours_and_two_loads(self) -> None:
        twin = dict(entry(0), label="twin")
        found = model([entry(0), twin])
        state = self.tool.move(self.screen(found), 1)
        self.assertEqual(((10, None), 1), state.selection.key)
        self.assertNotEqual(found["holders"][0]["style"], found["holders"][1]["style"])
        keys = {self.tool.details_key(item, identity) for item, identity in
                zip(found["holders"], self.tool.entry_ids(found["holders"]))}
        self.assertEqual(2, len(keys))
        state = self.reload(state, model([dict(twin), entry(0)]))
        self.assertEqual(1, state.selection.index, "the second of the two stays the second")

    def test_a_yielded_lease_keeps_its_colour(self) -> None:
        a, b, c = entry(0), entry(1), entry(2)
        state = self.screen(model([a, b, c]))
        colour = state.model["holders"][1]["style"]
        state = self.reload(state, model([a, c], [dict(b, kind="waiting", yields=1)]))
        state = self.reload(state, model([a, c, entry(3)], [dict(b, kind="waiting", yields=1)]))
        self.assertNotEqual(colour, state.model["holders"][2]["style"], "a new lease does not take the colour")
        state = self.reload(state, model([a, c, entry(3), dict(b)]))
        self.assertEqual(colour, state.model["holders"][3]["style"], "resumed: its own colour again")

    def test_the_mouse_and_the_keys_move_it_through_one_function(self) -> None:
        state = dataclasses.replace(self.tool.move(self.screen(), 2), scroll={"details": 4, "requests": 1})
        after, _ = self.keys(state, "k")
        self.assertEqual({}, after.scroll, "a key move starts QUEUE's and DETAILS' scroll over")
        queue = state.frame.tables["requests"]
        after, _ = self.send(state, self.tool.Mouse(0, 10, queue["top"], True))
        self.assertEqual((0, {"requests": 1}), (after.selection.index, after.scroll),
                         "a click on another row starts DETAILS' over and keeps QUEUE where it was scrolled")


class DetailsTests(Case):
    """fathers#23: loading, fresh, stale and failed, by entry and kind, bounded by the model."""

    def test_a_failing_load_shows_its_error_and_is_tried_again(self) -> None:
        tool = self.tool
        state = self.screen(model([entry(0)]))
        key = tool.details_key(state.model["holders"][0], state.selection.key)
        state, effects = tool.reduce(state, tool.DetailsLoaded(key, error="lanefather timed out"), 10.0)
        self.assertEqual([], [effect for effect in effects if effect.kind == "load-details"], "not at once")
        canvas = tool.Canvas(156, 43)
        tool.render(canvas, state, 10.0)
        self.assertIn("not loaded · lanefather timed out · retrying", canvas.render(False))
        self.assertIn("LEASE DETAILS", canvas.render(False))
        _, effects = tool.reduce(state, tool.Tick(), 10.0 + tool.DETAILS_RETRY)
        self.assertEqual([key], [effect.key for effect in effects if effect.kind == "load-details"], "tried again")

    def test_a_grant_loads_again_at_once(self) -> None:
        tool = self.tool
        waiting = entry(0, "waiting")
        state = self.screen(model([], [waiting]))
        key = tool.details_key(waiting, state.selection.key)
        state, _ = tool.reduce(state, tool.DetailsLoaded(key, None, None), 1.0)
        state, effects = tool.reduce(state, tool.ModelLoaded(model([dict(waiting, kind="held")])), 1.5)
        self.assertEqual([(key[0], "lease")], [effect.key for effect in effects if effect.kind == "load-details"],
                         "a lease's facts, not the request's, within the fresh window")

    def test_the_loads_kept_are_bounded_by_the_model(self) -> None:
        tool = self.tool
        a, b = entry(0), entry(1)
        state = self.screen(model([a, b]))
        for item, identity in zip([a, b], tool.entry_ids([a, b])):
            state, _ = tool.reduce(state, tool.DetailsLoaded(tool.details_key(item, identity)), 1.0)
        self.assertEqual(2, len(state.details))
        state, _ = tool.reduce(state, tool.ModelLoaded(model([b])), 2.0)
        self.assertEqual([((11, None), 0)], [key[0] for key in state.details], "a's left with it")

    def test_the_focus_on_details_while_they_load_keeps_their_keys(self) -> None:
        state = dataclasses.replace(self.screen(), focus="details")  # the audit's probe: j moved QUEUE's selection
        self.assertEqual({}, state.details, "still loading")
        for key in ("j", "k", "down", "page_down", "home", "G"):
            after, _ = self.keys(state, key)
            self.assertEqual((state.selection, "details"), (after.selection, after.focus), key)


class NoticeTests(Case):
    """fathers#24: a Settings panel that will not open says so, and the refresh stays live."""

    def test_a_settings_open_failure_is_a_notice_and_the_indicator_stays_live(self) -> None:
        tool = self.tool
        state = self.screen()
        state, _ = self.send(state, tool.SettingsFailed("capacity.json?"), now=5.0)
        self.assertEqual(("live", None), (state.refresh.kind, state.refresh.error))
        canvas = tool.Canvas(156, 43)
        tool.render(canvas, state, 5.0)
        footer = canvas.render(False).splitlines()[-1]
        self.assertIn("capacity.json?", footer)
        self.assertIn("● live", footer)
        self.assertNotIn("refresh failed", footer)
        state, _ = self.send(state, tool.Tick(), now=5.0 + tool.NOTICE_SECONDS)
        self.assertIsNone(state.notice, "a notice passes")

    def test_opening_settings_on_an_unreadable_capacity_json(self) -> None:
        tool = self.tool

        class Screen(tool.Screen):
            def settings(self, interval: float, theme: str) -> Any:
                raise tool.SettingsError("slotfather settings: capacity.json is unreadable")

        live = tool.Live(0, 1, Screen(), 1.0, "auto")
        self.addCleanup(live.close)
        live.state = self.screen()
        live.send(tool.Key("s"))
        self.assertIsNone(live.state.overlay)
        self.assertEqual("live", live.state.refresh.kind)
        self.assertIn("unreadable", live.state.notice.text)

    def test_a_failed_refresh_is_stale_and_keeps_the_model(self) -> None:
        tool = self.tool
        state = self.screen()
        found = state.model
        state, _ = self.send(state, tool.ModelFailed("slotfather: timed out"))
        self.assertEqual(("stale", "slotfather: timed out"), (state.refresh.kind, state.refresh.error))
        self.assertIs(found, state.model)
        state, _ = self.send(state, tool.ModelLoaded(found, "snapshot is 30 s old"))
        self.assertEqual("stale", state.refresh.kind)
        state, _ = self.send(state, tool.ModelLoaded(found))
        self.assertEqual(("live", None), (state.refresh.kind, state.refresh.error))


class FrameTests(Case):
    def test_a_focus_whose_panel_is_not_drawn_goes_back_to_queue(self) -> None:
        state = dataclasses.replace(self.screen(width=156), focus="events")
        self.assertEqual("requests", self.draw(state, 82, 40).focus, "EVENTS is not drawn under 83 columns")
        self.assertEqual("events", self.draw(state, 156, 43).focus)


class SettingsModeTests(PanelCase):
    """The Settings mode through the reducer, against the fake Slotfather: the audit's conflict and small-panel
    cases, and one function for what a Settings action does to the app."""

    def setUp(self) -> None:
        super().setUp()
        self.clock = Clock()
        self.state_module: Any = _view.current()

    def open(self, width: int = 90, height: int = 40) -> tuple[Any, Any]:
        tool = self.state_module
        dialog = self.settings.Settings(1.0, clock=self.clock)
        self.panel(dialog, idle(), width, height)
        return dialog, tool.AppState(overlay=tool.SettingsOverlay(dialog))

    def send(self, state: Any, event: Any) -> tuple[Any, list[str]]:
        state, effects = self.state_module.reduce(state, event, self.clock.now)
        return state, [effect.kind for effect in effects]

    def test_a_conflict_is_said_and_nothing_is_saved_or_reloaded(self) -> None:
        tool = self.state_module
        self.assertEqual(0, self.tool("slots", "4").returncode)
        dialog, state = self.open()
        dialog.focus = "amount"
        state, _ = self.send(state, tool.Key("right"))  # 5 slots, pending
        self.assertEqual(0, self.tool("slots", "6").returncode)  # another client, not read yet
        self.clock.now += self.settings.SETTLE_SECONDS
        state, kinds = self.send(state, tool.Tick())
        self.assertNotIn("load-model", kinds, "nothing was saved")
        self.assertEqual({"slots": 6}, self.saved_capacity(), "the other client's change stands")
        self.assertIsNotNone(state.overlay, "the panel stays open")
        self.assertIn("changed elsewhere", self.panel(dialog, idle()))

    def test_esc_in_a_small_panel_cancels_first_and_then_closes(self) -> None:
        tool = self.state_module
        self.assertEqual(0, self.tool("slots", "4").returncode)
        dialog, state = self.open()
        dialog.focus = "amount"
        state, _ = self.send(state, tool.Key("right"))
        self.panel(dialog, idle(), width=20, height=8)
        self.assertTrue(dialog.small)
        state, kinds = self.send(state, tool.Key("esc"))
        self.assertIsNotNone(state.overlay, "the first Esc cancels the pending change")
        self.assertIsNone(dialog.pending)
        state, kinds = self.send(state, tool.Key("esc"))
        self.assertIsNone(state.overlay)
        self.assertIn("repaint", kinds)
        self.assertEqual({"slots": 4}, self.saved_capacity())

    def test_the_wheel_in_settings_flashes_its_thumb(self) -> None:
        tool = self.state_module
        dialog, state = self.open(height=16)  # short enough to scroll
        x, y, width, height = dialog.bounds
        state, _ = self.send(state, tool.Mouse(65, x + width // 2, y + height // 2, True))
        self.assertGreater(dialog.offset, 0)
        self.assertIn("settings", state.glow)

    def test_a_settled_change_and_a_theme_apply_at_once(self) -> None:
        tool = self.state_module
        dialog, state = self.open()
        dialog.focus = "theme"
        state, kinds = self.send(state, tool.Key("right"))
        self.assertEqual("auto", state.theme, "pending, not applied")
        self.clock.now += self.settings.SETTLE_SECONDS
        state, kinds = self.send(state, tool.Tick())
        self.assertEqual(str(dialog.theme), state.theme)
        self.assertEqual(["apply-theme", "repaint", "remember", "load-model"], kinds)
        dialog.focus = "interval"
        state, kinds = self.send(state, tool.Key("right"))
        state, kinds = self.send(state, tool.Key("\r"))
        self.assertEqual(2.0, state.interval, "Refresh every follows at once")
        self.assertIn("remember", kinds)
        state, kinds = self.send(state, tool.Key("q"))
        self.assertEqual(["quit"], kinds)


class RectTests(unittest.TestCase):
    def test_inset(self) -> None:
        self.assertEqual(Rect(2, 3, 6, 14), Rect(0, 0, 10, 20).inset(2, 3, 2, 3))
        self.assertEqual(Rect(2, 3, 0, 0), Rect(0, 0, 3, 4).inset(2, 3, 2, 3))


if __name__ == "__main__":
    unittest.main()
