"""The app state, consolidated UiState, and one reducer (fathers#90, fathers#225): every one of the 14 Mode values'
keys from the mode table, UiState constructor invariants making illegal states unrepresentable, the overlay as one
value (fathers#17), the selection by entry identity (fathers#21), the details' load states (fathers#23), notices
apart from the refresh state (fathers#24), and the audit and UI state-machine regression cases."""
from __future__ import annotations

import dataclasses
import datetime
import unittest
from typing import Any, Callable, Optional

import helpers  # noqa: F401 - src/ on sys.path
import view as _view
from test_settings import INTERVAL, SAFETY, SLOTS, THEME, PanelCase, Settings, idle

from godfather import actions, events as events_module, reducer, settings_layout, settings_model, state as state_module
from godfather.layout import Rect


def entry(n: int, kind: str = "held", since: Any = None, **fields: Any) -> dict[str, Any]:
    now = datetime.datetime.now(datetime.UTC)
    return dict(dict(label=f"job{n}", kind=kind, count=1, pid=10 + n, style="h0", cpu=50, since=now,
                     granted=None if kind == "waiting" else now, rss=1024, procs=1, queue="", cmd="work", tree=[],
                     key=(10 + n, since), priority="normal", ticket=None, meta="", context={}, workdir="",
                     yieldable=False, yields=0, asked="", asked_at=None, idle_since=None), **fields)


def model(holders: list[dict[str, Any]], waiters: Optional[list[dict[str, Any]]] = None, **fields: Any) -> dict[str, Any]:
    now = datetime.datetime.now(datetime.UTC)
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
        state, _ = self.tool.reduce(self.tool.AppState(), self.tool.ModelLoaded(found, 8), 0.0)
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
    """Each (mode, key) pair across all 14 Mode values has a single dispatch entry, and this table says what each
    does and that nothing else changes. A binding added to reducer.MODES without a row here fails
    test_every_binding_is_described."""

    def expected(self) -> dict[Any, dict[str, Row]]:
        Mode = self.tool.Mode

        def selected(index: int) -> Check:
            return lambda state: state.selection.index == index

        def nothing(_: Any) -> object:
            return True

        def overlay(kind: Optional[str], **fields: int) -> Check:
            return lambda state: (
                state.ui.overlay is None
                if kind is None
                else state.ui.overlay is not None
                and state.ui.overlay.kind == kind
                and all(getattr(state.ui.overlay, name) == value for name, value in fields.items())
            )

        def focus(name: str, shows: Optional[str] = None) -> Check:
            return lambda state: state.ui.focus == name and (shows is None or state.ui.shows == shows)

        def scrolled_far(state: Any) -> object:
            return state.ui.overlay.scroll > 3

        def in_changelog(state: Any) -> object:
            return state.ui.overlay.kind == "changelog"

        def no_actions(state: Any) -> object:
            return state.notice is not None and state.notice.text == "no actions"

        quit_: Row = (set[str](), ["quit"], nothing)
        none: Row = (set[str](), [], nothing)
        main: dict[str, Row] = {
            "q": quit_, "Q": quit_,
            "\t": ({"ui"}, [], focus("events", "events")), "back_tab": ({"ui"}, [], focus("details", "requests")),
            "?": ({"ui"}, [], overlay("help")), "v": ({"ui"}, ["load-news"], overlay("changelog")),
            "s": (set[str](), ["open-settings"], nothing),
            "\r": ({"notice"}, [], no_actions), "\n": ({"notice"}, [], no_actions), "a": ({"notice"}, [], no_actions),
            "j": ({"selection"}, [], selected(3)), "down": ({"selection"}, [], selected(3)),
            "k": ({"selection"}, [], selected(1)), "up": ({"selection"}, [], selected(1)),
            "g": ({"selection"}, [], selected(0)), "home": ({"selection"}, [], selected(0)),
            "page_up": ({"selection"}, [], selected(0)),
            "G": ({"selection"}, [], selected(4)), "end": ({"selection"}, [], selected(4)),
            "page_down": ({"selection"}, [], selected(4)),
            "esc": none, "x": none,
            "f": none, "/": ({"ui"}, [], lambda state: state.ui.editing_filter),
            "r": ({"ui"}, [], lambda state: state.ui.events_filter.raw),
            "1": none, "2": none, "3": none,
        }
        events = self.start(Mode.OVERVIEW_EVENTS).frame.tables["events"]
        page, last = events["visible"] - 1, events["count"] - events["visible"]

        def cursor(index: int, offset: Optional[int]) -> Check:
            return lambda state: (self.tool.event_index(state), state.scroll.get("events")) == (index, offset)

        overview_base = {key: value for key, value in main.items() if key not in ("\r", "\n", "a")}
        overview_events: dict[str, Row] = {
            **overview_base,
            "\t": ({"ui"}, [], focus("details", "events")),
            "back_tab": ({"ui"}, [], focus("requests", "requests")),
            **{key: ({"picked"}, [], cursor(4, 3)) for key in ("j", "down")},
            **{key: ({"picked", "scroll", "glow"}, [], cursor(2, 2)) for key in ("k", "up")},
            "page_down": ({"picked"}, [], cursor(3 + page, 3)),
            **{key: ({"picked", "scroll"}, [], cursor(0, None)) for key in ("page_up", "g", "home")},
            **{key: ({"picked", "scroll", "glow"}, [], cursor(39, last)) for key in ("G", "end")},
            "r": ({"ui", "picked", "scroll"}, [], lambda state: state.ui.events_filter.raw),
            "left": none, "h": none, "right": none, "l": none,
        }

        def detail_scroll(offset: int) -> Check:
            return lambda state: state.scroll.get("details") == offset

        overview_details_view = self.start(Mode.OVERVIEW_DETAILS).frame.tables["details"]
        ov_step = overview_details_view["visible"] - 1
        ov_bottom = overview_details_view["count"] - overview_details_view["visible"]
        overview_details: dict[str, Row] = {
            **overview_base,
            "\t": ({"ui"}, [], focus("requests", "requests")),
            "back_tab": ({"ui"}, [], focus("events", "events")),
            **{key: ({"scroll", "glow"}, [], detail_scroll(offset)) for key, offset in (
                ("j", min(ov_bottom, 2)), ("down", min(ov_bottom, 2)), ("k", 0), ("up", 0),
                ("page_down", min(ov_bottom, 1 + ov_step)), ("page_up", 0),
                ("g", 0), ("home", 0), ("G", ov_bottom), ("end", ov_bottom))},
        }

        overlay_scroll: dict[str, Row] = {
            key: ({"ui", "glow"}, [], overlay("help", scroll=offset))
            for key, offset in (
                ("j", 4), ("down", 4), ("k", 2), ("up", 2), ("g", 0), ("home", 0),
                (" ", 3 + 35), ("page_down", 3 + 35), ("page_up", 0),
            )
        }
        help_: dict[str, Row] = {
            **overlay_scroll,
            "G": ({"ui", "glow"}, [], scrolled_far),
            "end": ({"ui", "glow"}, [], scrolled_far),
            "?": ({"ui"}, [], overlay(None)), "esc": ({"ui"}, [], overlay(None)),
            "v": ({"ui"}, ["load-news"], overlay("changelog", scroll=0)),
            "q": quit_, "Q": quit_,
            "s": none, "\t": none, "x": none,
        }
        news: dict[str, Row] = {
            key: (fields, effects, in_changelog if fields else check)
            for key, (fields, effects, check) in help_.items()
        }
        news.update({
            "v": ({"ui", "read"}, ["save-read"], overlay(None)),
            "esc": ({"ui", "read"}, ["save-read"], overlay(None)),
            "?": ({"ui", "read"}, ["save-read"], overlay("help", scroll=0)),
            "left": ({"ui"}, [], overlay("changelog", tab=1, scroll=0)),
            "h": ({"ui"}, [], overlay("changelog", tab=1, scroll=0)),
            "back_tab": ({"ui"}, [], overlay("changelog", tab=1, scroll=0)),
            "right": ({"ui"}, [], overlay("changelog", tab=1, scroll=0)),
            "l": ({"ui"}, [], overlay("changelog", tab=1, scroll=0)),
            "\t": ({"ui"}, [], overlay("changelog", tab=1, scroll=0)),
            "q": ({"read"}, ["save-read", "quit"], nothing),
            "Q": ({"read"}, ["save-read", "quit"], nothing),
        })

        lanes = [row["key"] for row in self.tool.page_rows(self.start(Mode.PAGE), "lanes") if row.get("key")]

        def lane(index: int) -> Check:
            return lambda state: state.picked.get("lanes") == lanes[index]

        def page_shown(name: str) -> Check:
            return lambda state: state.ui.page == name

        pages: dict[str, Row] = {
            "q": quit_, "Q": quit_, "?": main["?"], "v": main["v"], "s": main["s"],
            "esc": ({"ui"}, [], page_shown("overview")),
            "1": ({"ui"}, [], page_shown("overview")),
            "2": none, "3": none,
        }
        page_table: dict[str, Row] = {
            **pages,
            "\t": ({"ui"}, [], focus("events")),
            "back_tab": ({"ui"}, [], focus("details")),
            "\r": ({"ui"}, [], overlay("actions")),
            "\n": ({"ui"}, [], overlay("actions")),
            "a": ({"ui"}, [], overlay("actions")),
            **{key: ({"picked"}, [], lane(2)) for key in ("j", "down")},
            **{key: ({"picked"}, [], lane(0)) for key in ("k", "up", "g", "home", "page_up")},
            **{key: ({"picked"}, [], lane(len(lanes) - 1)) for key in ("G", "end", "page_down")},
        }
        view = self.start(Mode.PAGE_PANEL).frame.tables["details"]
        step, bottom = view["visible"] - 1, view["count"] - view["visible"]
        page_panel: dict[str, Row] = {
            **pages,
            "esc": ({"ui", "scroll"}, [], page_shown("overview")),
            "1": ({"ui", "scroll"}, [], page_shown("overview")),
            "\t": ({"ui"}, [], focus("lanes")),
            "back_tab": ({"ui"}, [], focus("events")),
            **{key: ({"scroll", "glow"}, [], detail_scroll(offset)) for key, offset in (
                ("j", min(bottom, 2)), ("down", min(bottom, 2)), ("k", 0), ("up", 0),
                ("page_down", min(bottom, 1 + step)), ("page_up", 0),
                ("g", 0), ("home", 0), ("G", bottom), ("end", bottom))},
        }

        filter_mode: dict[str, Row] = {
            "esc": ({"ui"}, [], lambda state: not state.ui.editing_filter and state.ui.events_filter.text == ""),
            "\r": ({"ui"}, [], lambda state: not state.ui.editing_filter and state.ui.events_filter.text == "71"),
            "\n": ({"ui"}, [], lambda state: not state.ui.editing_filter and state.ui.events_filter.text == "71"),
            "\x7f": ({"ui"}, [], lambda state: state.ui.editing_filter and state.ui.events_filter.text == "7"),
            "\x08": ({"ui"}, [], lambda state: state.ui.editing_filter and state.ui.events_filter.text == "7"),
            "0": ({"ui"}, [], lambda state: state.ui.editing_filter and state.ui.events_filter.text == "710"),
            "q": ({"ui"}, [], lambda state: state.ui.editing_filter and state.ui.events_filter.text == "71q"),
            "up": none,
        }

        def action_cancelled(state: Any) -> object:
            return state.ui.modal is None and state.notice is not None and state.notice.text == "nothing run"

        def action_started(state: Any) -> object:
            return state.ui.modal is None and bool(state.acting)

        actions_mode: dict[str, Row] = {
            "q": quit_, "Q": quit_,
            "esc": ({"ui", "notice"}, [], action_cancelled),
            **{key: ({"ui"}, [], lambda state: state.ui.modal.cursor == 0) for key in ("k", "up", "g", "home")},
            **{key: ({"ui"}, [], lambda state: state.ui.modal.cursor == 2) for key in ("j", "down", "G", "end")},
            **{key: ({"ui", "notice", "acting"}, ["action"], action_started) for key in ("\r", "\n", " ")},
            "x": none,
        }

        reason_mode: dict[str, Row] = {
            "esc": ({"ui", "notice"}, [], action_cancelled),
            "\r": ({"ui"}, [], lambda state: isinstance(state.ui.modal, self.tool.Confirm)
                   and state.ui.modal.reason == "hi"),
            "\n": ({"ui"}, [], lambda state: isinstance(state.ui.modal, self.tool.Confirm)
                   and state.ui.modal.reason == "hi"),
            "\x7f": ({"ui"}, [], lambda state: isinstance(state.ui.modal, self.tool.Reason)
                     and state.ui.modal.text == "h"),
            "\x08": ({"ui"}, [], lambda state: isinstance(state.ui.modal, self.tool.Reason)
                     and state.ui.modal.text == "h"),
            "q": ({"ui"}, [], lambda state: isinstance(state.ui.modal, self.tool.Reason)
                  and state.ui.modal.text == "hiq"),
            "up": none,
        }

        confirm_mode: dict[str, Row] = {
            "y": ({"ui", "notice", "acting"}, ["action"], action_started),
            "Y": ({"ui", "notice", "acting"}, ["action"], action_started),
            "n": ({"ui", "notice"}, [], action_cancelled),
            "N": ({"ui", "notice"}, [], action_cancelled),
            "esc": ({"ui", "notice"}, [], action_cancelled),
            "q": none, "\r": none,
        }

        settings_mode: dict[str, Row] = {
            "esc": ({"ui"}, ["repaint"], overlay(None)),
            "s": ({"ui"}, ["repaint"], overlay(None)),
            "q": quit_, "Q": quit_,
            **{key: ({"ui"}, [], lambda state: state.ui.modal.focus == ("godfather", "theme"))
               for key in ("down", "j", "\t")},
            **{key: ({"ui", "glow"}, [], lambda state: state.ui.modal.focus == ("slotfather", "order.limit"))
               for key in ("up", "k", "back_tab", "end", "G")},
            **{key: ({"ui", "glow"}, [], lambda state: state.ui.modal.focus == ("godfather", "interval")
                     and state.ui.modal.offset == 1)
               for key in ("home", "g")},
            **{key: ({"ui"}, [], lambda state: state.ui.modal.pending is not None
                     and state.ui.modal.pending.value == 1.0)
               for key in ("left", "h", "-")},
            **{key: ({"ui"}, [], lambda state: state.ui.modal.pending is not None
                     and state.ui.modal.pending.value == 3.0)
               for key in ("right", "l", "+")},
            **{key: ({"ui"}, [], lambda state: state.ui.modal.editing and state.ui.modal.buffer == "2")
               for key in ("\r", "\n", " ")},
            **{digit: ({"ui"}, [], lambda state, d=digit: state.ui.modal.editing and state.ui.modal.buffer == d)
               for digit in self.tool.DIGITS},
            "d": ({"ui", "interval"}, ["remember"], lambda state: state.interval == 1.0),
            "D": ({"ui"}, [], lambda state: state.ui.modal.armed is not None),
            "page_up": ({"ui", "glow"}, [], lambda state: state.ui.modal.offset == 0),
            "page_down": ({"ui", "glow"}, [], lambda state: state.ui.modal.offset > 2),
        }

        settings_edit_mode: dict[str, Row] = {
            "esc": ({"ui"}, [], lambda state: not state.ui.modal.editing),
            "\r": ({"ui", "interval"}, ["remember"], lambda state: not state.ui.modal.editing and state.interval == 3.0),
            "\n": ({"ui", "interval"}, ["remember"], lambda state: not state.ui.modal.editing and state.interval == 3.0),
            "\t": ({"ui", "interval"}, ["remember"], lambda state: not state.ui.modal.editing
                   and state.ui.modal.focus == ("godfather", "theme")),
            **{key: ({"ui"}, [], lambda state: state.ui.modal.buffer == "") for key in ("\x7f", "\b", "\x15")},
            **{digit: ({"ui"}, [], lambda state, d=digit: state.ui.modal.buffer == "3" + d)
               for digit in self.tool.DIGITS},
            "q": none, "s": none,
        }

        settings_small_mode: dict[str, Row] = {
            "esc": ({"ui"}, ["repaint"], overlay(None)),
            "s": ({"ui"}, ["repaint"], overlay(None)),
            "q": quit_, "Q": quit_,
            "j": none, "\r": none, "5": none,
        }

        return {
            Mode.MAIN: main,
            Mode.OVERVIEW_EVENTS: overview_events,
            Mode.OVERVIEW_DETAILS: overview_details,
            Mode.PAGE: page_table,
            Mode.PAGE_PANEL: page_panel,
            Mode.FILTER: filter_mode,
            Mode.ACTIONS: actions_mode,
            Mode.REASON: reason_mode,
            Mode.CONFIRM: confirm_mode,
            Mode.HELP: help_,
            Mode.NEWS: news,
            Mode.SETTINGS: settings_mode,
            Mode.SETTINGS_EDIT: settings_edit_mode,
            Mode.SETTINGS_SMALL: settings_small_mode,
        }

    def start(self, mode: Any) -> Any:
        """A canonical starting AppState for each of the 14 Mode values."""
        tool = self.tool
        Mode = tool.Mode
        events = [dict(when=datetime.datetime(2026, 11, 12, tzinfo=datetime.UTC)
                       + datetime.timedelta(seconds=n), verb="granted", count=1,
                       label=f"e{n} 710 71q" if n == 39 else f"e{n}", pid=n, delta=1)
                  for n in range(40)]
        tree = [(100 + n, 1.0, 1024, f"proc{n}") for n in range(25)]
        state = self.screen(model([entry(n, tree=tree) for n in range(3)],
                                  [entry(n, "waiting") for n in range(3, 5)],
                                  events=events), 156, 43)
        state = tool.move(state, 2)
        from godfather import news
        state = dataclasses.replace(state, news=news.News((), (news.Item("slotfather", "1.0.0", "2026-11-01", "Added",
                                                                         "a"),)))
        if mode in (Mode.PAGE, Mode.PAGE_PANEL):
            from test_lanes import decoded

            state = dataclasses.replace(state, tools={"lanefather": tool.Tool("lanefather", "ok", None, decoded())})

            def second(_position: int, _count: int) -> int:
                return 1

            state = tool.page_move(tool.open_page(self.draw(state, 156, 30), "lanes"), "lanes", second)
            state = self.draw(state, 156, 30)
            if mode == Mode.PAGE_PANEL:
                ui = dataclasses.replace(state.ui, screen=dataclasses.replace(state.ui.screen, focus="details"))
                state = self.draw(dataclasses.replace(state, ui=ui, scroll={"details": 1}), 156, 16)
        elif mode == Mode.OVERVIEW_EVENTS:
            ui = dataclasses.replace(state.ui, screen=tool.OverviewScreen("events", "events"))
            state = dataclasses.replace(state, ui=ui, scroll={"events": 3},
                                        picked={"events": tool.event_id(tool.event_rows(state)[3])})
        elif mode == Mode.OVERVIEW_DETAILS:
            item = state.model["holders"][2]
            key = tool.details_key(item, state.selection.key)
            state, _ = tool.reduce(state, tool.DetailsLoaded(key, None, "/work"), 0.0)
            ui = dataclasses.replace(state.ui, screen=tool.OverviewScreen("details", "requests"))
            state = self.draw(dataclasses.replace(state, ui=ui, scroll={"details": 1}), 156, 24)
        elif mode == Mode.FILTER:
            ui = dataclasses.replace(state.ui, modal=tool.FilterEdit(), filter=tool.Filter(text="71"))
            state = dataclasses.replace(state, ui=ui)
        elif mode == Mode.ACTIONS:
            acts = tuple(
                actions.Action("slotfather", f"Act {n}", ("reprioritize", "12", name),
                               identity=("overview", (12, None), 12, "t0"))
                for n, name in enumerate(("interactive", "normal", "batch"))
            )
            state = tool.with_modal(state, tool.ActionMenu(acts, cursor=1))
        elif mode == Mode.REASON:
            park = actions.Action("lanefather", "Park", ("park", "710"), confirm=True, reason=True,
                                  identity=("overview", (12, None), 12, "t0"))
            state = tool.with_modal(state, tool.Reason(park, text="hi"))
        elif mode == Mode.CONFIRM:
            reap = actions.Action("junkfather", "Reap", ("reap", "--id", "j1"), confirm=True,
                                  identity=("overview", (12, None), 12, "t0"))
            state = tool.with_modal(state, tool.Confirm(reap))
        elif mode == Mode.HELP:
            state = tool.with_modal(self.draw(state, 100, 43), tool.Help(scroll=3))
        elif mode == Mode.NEWS:
            state = tool.with_modal(state, tool.News(scroll=3))
        elif mode in (Mode.SETTINGS, Mode.SETTINGS_EDIT, Mode.SETTINGS_SMALL):
            rows = [
                {"key": f"order.{name}", "label": name.capitalize(), "type": "duration", "value": 60,
                 "writable": True, "revision": "r1", "step": 30, "min": 0}
                for name in ("safety", "fallback", "overrun", "limit")
            ]
            section = settings_model.Section(
                "slotfather",
                reply={"v": 1, "tool": "slotfather", "writable": True, "status": [{"label": "queue", "value": "/q"}],
                       "sections": [{"id": "order", "settings": rows}]},
            )
            state = dataclasses.replace(state, interval=2.0, frame= dataclasses.replace(state.frame, width=90, height=16))
            panel = settings_model.Panel(
                sections=(section,),
                focus=("godfather", "interval"),
                offset=2,
                reveal=False,
            )
            if mode == Mode.SETTINGS_EDIT:
                panel = dataclasses.replace(panel, edit=settings_model.EditState("3", replace=False))
            elif mode == Mode.SETTINGS_SMALL:
                state = dataclasses.replace(state, frame=dataclasses.replace(state.frame, width=20, height=8))
            state = tool.with_modal(state, panel)
        self.assertEqual(mode, tool.mode_of(state))
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
        self.assertEqual(set(self.tool.Mode), set(expected), "all 14 Mode values are tested")
        self.assertEqual(set(self.tool.Mode), set(self.tool.MODES), "all 14 Mode values are in reducer.MODES")
        for mode, table in self.tool.MODES.items():
            with self.subTest(mode=mode):
                self.assertLessEqual(set(table), set(expected[mode]), "a binding the table above does not describe")

    def test_hotkeys_by_position_in_every_mode(self) -> None:
        Mode = self.tool.Mode
        for mode, key, meant in ((Mode.MAIN, "й", "q"), (Mode.MAIN, "о", "j"), (Mode.HELP, ",", "?"),
                                 (Mode.NEWS, "м", "v")):
            with self.subTest(mode=mode, key=key):
                state = self.start(mode)
                self.assertEqual(self.send(state, self.tool.Key(meant))[1], self.send(state, self.tool.Key(key))[1])
                self.assertEqual(self.send(state, self.tool.Key(meant))[0], self.send(state, self.tool.Key(key))[0])

    def test_a_click_on_a_footer_hint_is_its_key_in_every_mode(self) -> None:
        state = self.start(self.tool.Mode.MAIN)
        canvas = self.tool.Canvas(156, 43)
        self.tool.render(canvas, state, 0.0)
        row, start, _, _ = next(hint for hint in canvas.hint_hits if hint[3] == "?")
        after, _ = self.send(state, self.tool.Mouse(0, start, row, True))
        self.assertEqual("help", after.ui.overlay.kind)


class UiStateInvariantTests(unittest.TestCase):
    """Constructor invariants on OverviewScreen, PageScreen, modals, and UiState raise ValueError on impossible
    states (fathers#225)."""

    def test_overview_screen_invariants(self) -> None:
        self.assertEqual(("requests", "requests"),
                         (state_module.OverviewScreen().focus, state_module.OverviewScreen().shows))
        self.assertEqual(("events", "events"),
                         (state_module.OverviewScreen("events").focus, state_module.OverviewScreen("events").shows))
        self.assertEqual(("details", "events"),
                         (state_module.OverviewScreen("details", "events").focus,
                          state_module.OverviewScreen("details", "events").shows))
        for bad_focus in ("lanes", "procs", "machine", ""):
            with self.subTest(focus=bad_focus), self.assertRaises(ValueError):
                state_module.OverviewScreen(bad_focus)
        with self.assertRaises(ValueError):
            state_module.OverviewScreen("details", "details")
        with self.assertRaises(ValueError):
            state_module.OverviewScreen("requests", "events")
        with self.assertRaises(ValueError):
            state_module.OverviewScreen("events", "requests")

    def test_page_screen_invariants(self) -> None:
        self.assertEqual(("lanes", "lanes"),
                         (state_module.PageScreen("lanes").page, state_module.PageScreen("lanes").focus))
        self.assertEqual(("processes", "procs"),
                         (state_module.PageScreen("processes").page, state_module.PageScreen("processes").focus))
        for bad_page in ("overview", "unknown", ""):
            with self.subTest(page=bad_page), self.assertRaises(ValueError):
                state_module.PageScreen(bad_page)
        for page, bad_focus in (("lanes", "requests"), ("lanes", "procs"), ("processes", "requests"),
                                ("processes", "lanes")):
            with self.subTest(page=page, focus=bad_focus), self.assertRaises(ValueError):
                state_module.PageScreen(page, bad_focus)
        with self.assertRaises(ValueError):
            state_module.PageScreen("lanes", came_from=("nowhere",))

    def test_modal_and_uistate_invariants(self) -> None:
        req_act = actions.Action("slotfather", "Reprioritize", ("reprioritize",), identity=("overview", ("k", 0)))
        lane_park = actions.Action("lanefather", "Park", ("park", "l1"), confirm=True, reason=True,
                                   identity=("lanes", ("/s", "l1")))
        proc_reap = actions.Action("junkfather", "Reap", ("reap", "--id", "j1"), confirm=True,
                                   identity=("processes", ("entry", "j1")))

        # Two simultaneous modals
        with self.assertRaises(ValueError):
            state_module.UiState(modal=state_module.FilterEdit(), overlay=state_module.Help())
        with self.assertRaises(ValueError):
            state_module.UiState(modal=state_module.Help(), filter=events_module.Filter(editing=True))
        with self.assertRaises(ValueError):
            state_module.UiState(filter=events_module.Filter(editing=True))

        # FilterEdit on a PageScreen
        with self.assertRaises(ValueError):
            state_module.UiState(state_module.PageScreen("lanes"), state_module.FilterEdit())

        # Action modals with empty actions, out-of-range cursor, wrong focus, or wrong page
        with self.assertRaises(ValueError):
            state_module.ActionMenu(())
        with self.assertRaises(ValueError):
            state_module.ActionMenu((req_act,), cursor=1)
        with self.assertRaises(ValueError):
            state_module.Reason(req_act)
        with self.assertRaises(ValueError):
            state_module.Confirm(req_act)
        with self.assertRaises(ValueError):
            state_module.Confirm(lane_park, "")
        with self.assertRaises(ValueError):
            state_module.Confirm(proc_reap, "unexpected reason")
        with self.assertRaises(ValueError):
            state_module.UiState(state_module.OverviewScreen("events"), state_module.ActionMenu((req_act,)))
        with self.assertRaises(ValueError):
            state_module.UiState(state_module.OverviewScreen("requests"), state_module.ActionMenu((lane_park,)))
        with self.assertRaises(ValueError):
            state_module.UiState(state_module.PageScreen("lanes"), state_module.Confirm(proc_reap))


class UiBugRegressionTests(Case):
    """Regression tests for the 6 concrete UI state-machine bugs fixed in fathers#225."""

    def test_resizing_below_83_cols_while_in_filter_edit_exits_filter_mode_so_q_quits(self) -> None:
        state = self.screen(width=156, height=43)
        state, _ = self.keys(state, "/", "a", "b")
        self.assertEqual(self.tool.Mode.FILTER, self.tool.mode_of(state))
        state = self.draw(state, 82, 40)
        self.assertFalse(state.ui.editing_filter)
        self.assertEqual("ab", state.ui.events_filter.text)
        self.assertEqual(self.tool.Mode.MAIN, self.tool.mode_of(state))
        _, effects = self.keys(state, "q")
        self.assertEqual(["quit"], effects)

    def test_clicking_main_footer_q_while_reason_is_open_cancels_reason_instead_of_typing_q(self) -> None:
        from test_lanes import decoded

        state = dataclasses.replace(
            self.screen(),
            tools={"lanefather": self.tool.Tool("lanefather", "ok", None, decoded(), actions=("park",))},
        )
        state = self.draw(self.tool.open_page(state, "lanes"))
        state, _ = self.keys(state, "\r", "G", "\r")
        state = self.draw(state)
        self.assertEqual(self.tool.Mode.REASON, self.tool.mode_of(state))
        row, start, _, _ = next(hint for hint in state.frame.hints if hint[3] == "q")
        after, effects = self.send(state, self.tool.Mouse(0, start, row, True), now=2.0)
        self.assertEqual([], effects)
        self.assertIsNone(after.ui.modal, "clicking outside Reason on the main footer cancels Reason")
        self.assertEqual("nothing run", after.notice.text)

    def test_russian_hotkeys_close_and_quit_settings_small_even_when_editing_is_true(self) -> None:
        panel = settings_model.Panel(
            focus=("godfather", "interval"),
            edit=settings_model.EditState("2", replace=False),
        )
        small_state = dataclasses.replace(
            self.screen(width=20, height=8),
            ui=self.tool.UiState(modal=panel),
        )
        self.assertEqual(self.tool.Mode.SETTINGS_SMALL, self.tool.mode_of(small_state))
        closed, effects = self.keys(small_state, "ы")  # Russian s
        self.assertIsNone(closed.ui.modal)
        self.assertIn("repaint", effects)
        _, quit_effects = self.keys(small_state, "й")  # Russian q
        self.assertIn("quit", quit_effects)

    def test_go_back_skips_an_absent_intermediate_page_in_came_from(self) -> None:
        from test_lanes import decoded as lanes_decoded
        from test_procs import decoded as procs_decoded

        state = dataclasses.replace(
            self.screen(),
            tools={
                "lanefather": self.tool.Tool("lanefather", "ok", None, lanes_decoded()),
                "junkfather": self.tool.Tool("junkfather", "ok", None, procs_decoded()),
            },
        )
        state = self.tool.follow_link(state, ("lanes", ("/fixture/straylight/lanes", "710-speech-paste-phase")))
        state = self.tool.follow_link(state, ("processes", ("entry", "j3f9a1c")))
        self.assertEqual(("processes", ("overview", "lanes")), (state.ui.page, state.ui.came_from))
        # Lanefather disappears while the user is on Processes
        state, _ = self.send(state, self.tool.ToolLoaded(self.tool.Tool("lanefather", "absent")))
        self.assertEqual("processes", state.ui.page)
        state, _ = self.keys(state, "esc")
        self.assertEqual(("overview", ()), (state.ui.page, state.ui.came_from))

    def test_leave_absent_dismisses_an_open_action_modal_for_the_departed_page(self) -> None:
        from test_lanes import decoded as lanes_decoded
        from test_procs import decoded as procs_decoded

        base = dataclasses.replace(
            self.screen(),
            tools={
                "lanefather": self.tool.Tool("lanefather", "ok", None, lanes_decoded(), actions=("park",)),
                "junkfather": self.tool.Tool("junkfather", "ok", None, procs_decoded(), actions=("reap",)),
            },
        )
        # ActionMenu and Reason on Lanes
        lanes_state = self.tool.open_page(base, "lanes")
        menu_state, _ = self.keys(lanes_state, "\r")
        self.assertIsInstance(menu_state.ui.modal, self.tool.ActionMenu)
        reason_state, _ = self.keys(menu_state, "G", "\r")
        self.assertIsInstance(reason_state.ui.modal, self.tool.Reason)
        confirm_state, _ = self.keys(reason_state, "w", "h", "y", "\r")
        self.assertIsInstance(confirm_state.ui.modal, self.tool.Confirm)
        for active in (menu_state, reason_state, confirm_state):
            after, _ = self.send(active, self.tool.ToolLoaded(self.tool.Tool("lanefather", "absent")))
            self.assertEqual("overview", after.ui.page)
            self.assertIsNone(after.ui.modal)

    def test_slash_then_esc_on_an_empty_filter_preserves_events_scroll_picked_and_fresh_events(self) -> None:
        when = datetime.datetime(2026, 11, 12, tzinfo=datetime.UTC)
        events = [dict(when=when + datetime.timedelta(seconds=n), verb="granted", count=1, label=f"e{n}", pid=n,
                       delta=1) for n in range(40)]
        state = self.screen(model([entry(0)], events=events))
        picked_id = self.tool.event_id(self.tool.event_rows(state)[5])
        state = dataclasses.replace(state, scroll={"events": 5}, picked={"events": picked_id}, fresh_events=3)
        state, _ = self.keys(state, "/")
        self.assertTrue(state.ui.editing_filter)
        state, _ = self.keys(state, "esc")
        self.assertFalse(state.ui.editing_filter)
        self.assertEqual(5, state.scroll.get("events"))
        self.assertEqual(picked_id, state.picked.get("events"))
        self.assertEqual(3, state.fresh_events)


class SettingsModeTableTests(PanelCase):
    """The Settings modes: keys from the mode table, translated to their hotkey unless a value is typed, too small
    a panel taking only the ways out; and a click on a footer hint is the hint's key there too."""

    def test_the_mode_follows_the_panel(self) -> None:
        settings = Settings(self.clock)
        self.assertEqual(reducer.Mode.SETTINGS, reducer.mode_of(settings.state))
        settings.select(SAFETY)
        settings.key("\r")
        self.assertEqual(reducer.Mode.SETTINGS_EDIT, reducer.mode_of(settings.state))
        settings.key("esc")
        settings.show(width=20, height=8)
        self.assertEqual(reducer.Mode.SETTINGS_SMALL, reducer.mode_of(settings.state))

    def test_keys_are_hotkeys_unless_a_value_is_typed(self) -> None:
        settings = Settings(self.clock)
        settings.select(SAFETY)
        settings.key("д")  # l: the next value
        self.assertIsNotNone(settings.panel.pending)
        settings.key("esc", "\r", "ы")  # typing: ы stays ы, which a number does not take
        self.assertEqual((True, "3"), (settings.panel.editing, settings.panel.buffer))

    def test_a_click_on_a_footer_hint_is_the_hint_key_for_the_panel_too(self) -> None:
        settings = Settings(self.clock)
        canvas = settings.canvas()
        settings.state = dataclasses.replace(settings.state, frame=dataclasses.replace(
            settings.state.frame, hints=tuple(canvas.hint_hits)))
        row, start, _, _ = next(hint for hint in canvas.hint_hits if hint[3] == "esc")
        self.assertIn("repaint", settings.click(start, row))
        self.assertIsNone(settings.state.ui.overlay)


class MouseTableTests(Case):
    """The mouse in each mode, by button and where it lands."""

    def test_each_mode_button_and_place(self) -> None:
        tool = self.tool
        events = [dict(when=datetime.datetime(2026, 11, 12, tzinfo=datetime.UTC)
                       + datetime.timedelta(seconds=n), verb="granted", count=1, label=f"e{n}", pid=n, delta=1)
                  for n in range(40)]
        base = tool.move(self.screen(model([entry(n) for n in range(3)], [entry(n, "waiting") for n in range(3, 5)],
                                           events=events)), 2)
        queue, details, evs = (base.frame.tables[name] for name in ("requests", "details", "events"))
        version = base.frame.version_at
        gap = queue["top"] + 3  # the row between running and waiting
        help_ = tool.with_modal(self.draw(base, 100, 43), tool.Help(scroll=3))  # one column: it scrolls
        box = tool.overlay_box(help_, help_.ui.overlay)
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
             lambda state: state.ui.focus == "details" and state.selection.index == 2),
            ("the wheel on EVENTS scrolls it, not the selection", base, (65, 10, evs["y"] + 3, True),
             lambda state: state.scroll.get("events") == 3 and state.selection.index == 2
             and "events" in state.glow),
            ("the wheel up at the top stays there", base, (64, 10, evs["y"] + 3, True),
             lambda state: state.scroll.get("events") == 0),
            ("Ctrl+wheel is the wheel", base, (65 | 16, 10, evs["y"] + 3, True),
             lambda state: state.scroll.get("events") == 3),
            ("what's new in the footer opens it", base, (0, version[1], version[0], True),
             lambda state: state.ui.overlay == tool.Changelog()),
            ("the wheel in help scrolls it", help_, (65, *inside, True), lambda state: state.ui.overlay.scroll == 6),
            ("a click in help does nothing", help_, (0, *inside, True), lambda state: state == help_),
            ("a click outside help closes it", help_, (0, *outside, True), lambda state: state.ui.overlay is None),
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
        self.assertEqual("help", state.ui.overlay.kind)
        state, _ = self.keys(state, "v")
        self.assertEqual("changelog", state.ui.overlay.kind, "v in help: the changelog")
        state, _ = self.keys(state, "?")
        self.assertEqual("help", state.ui.overlay.kind, "? in the changelog: help")
        state, _ = self.keys(state, "?")
        self.assertIsNone(state.ui.overlay)

    def test_keys_after_v_in_the_same_batch_scroll_the_changelog(self) -> None:
        for opener, kind in (("v", "changelog"), ("?", "help")):
            with self.subTest(opener=opener):
                state, _ = self.keys(self.screen(width=100), opener, "j", "j")  # never drawn between them
                self.assertEqual((kind, 2), (state.ui.overlay.kind, state.ui.overlay.scroll))

    def test_with_one_filter_the_tab_keys_go_back_to_its_top(self) -> None:
        from godfather import news
        state = dataclasses.replace(self.screen(), news=news.News((), ()))
        state, _ = self.keys(state, "v", "j", "j", "right")
        self.assertEqual(self.tool.Changelog(tab=0, scroll=0), state.ui.overlay)


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
        self.assertEqual((self.tool.Changelog(), ["load-news"]), (state.ui.overlay, effects))
        found = self.news()
        state, effects = self.send(state, self.tool.NewsLoaded(found))
        self.assertEqual((found, [], self.tool.Changelog()), (state.news, effects, state.ui.overlay))
        self.assertTrue(self.tool.unread(state), "nothing read yet")

    def test_the_filter_steps_through_all_and_each_tool_with_entries(self) -> None:
        state = dataclasses.replace(self.screen(), news=self.news())
        tabs: list[int] = []
        for key in ("v", "right", "right", "right", "left"):
            state, _ = self.keys(state, key)
            tabs.append(state.ui.overlay.tab)
        self.assertEqual([0, 1, 2, 0, 2], tabs, "all, slotfather, junkfather, then round")
        state, _ = self.keys(state, "j")
        state, _ = self.keys(state, "\t")
        self.assertEqual(self.tool.Changelog(tab=0, scroll=0), state.ui.overlay, "a new filter starts at its top")

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
        self.assertEqual(2, state.ui.overlay.tab, "Junkfather")
        fewer = news.News((), tuple(item for item in self.news().items if item.tool == "junkfather"))
        state, _ = self.send(state, self.tool.NewsLoaded(fewer))
        self.assertEqual(1, state.ui.overlay.tab, "still Junkfather, now the second filter")
        state, _ = self.send(state, self.tool.NewsLoaded(news.News((), ())))
        self.assertEqual(0, state.ui.overlay.tab, "gone: back to all")

    def opened(self) -> tuple[Any, Any]:
        """What's new open over the overview, drawn: the state and the canvas it drew."""
        state = dataclasses.replace(self.screen(), news=self.news())
        state, _ = self.keys(state, "v")
        canvas = self.tool.Canvas(156, 43)
        self.tool.render(canvas, state, 0.0)
        state, _ = self.tool.reduce(state, self.tool.Drawn(helpers.frame_of(canvas)), 0.0)
        return state, canvas

    def test_the_filters_hit_test_is_the_cells_each_name_was_drawn_in(self) -> None:
        state, canvas = self.opened()
        row = canvas.help_view.y
        line = "".join(cell[0] for cell in canvas.cells[row])
        labels = ["All", "Slotfather", "Junkfather"]
        start = line.index(" · ".join(labels))
        expected: list[tuple[int, int, int, int]] = []
        x = start
        for tab, label in enumerate(labels):
            expected.append((row, x, x + len(label), tab))
            x += len(label) + len(" · ")
        self.assertEqual(tuple(expected), state.frame.filters, "from the drawing, not recomputed")
        for _, begin, end, tab in expected:
            self.assertEqual([tab] * (end - begin), [state.frame.filter_at(x, row) for x in range(begin, end)])
        between = [state.frame.filter_at(x, row) for x in (start - 1, start + len("All") + 1, x - len(" · ") + 1)]
        self.assertEqual([None, None, None], between, "the space and the separators are not targets")
        self.assertIsNone(state.frame.filter_at(start, row + 1), "only the border row")

    def test_a_click_on_a_filter_shows_that_tool_as_the_arrows_do(self) -> None:
        state, canvas = self.opened()
        state, _ = self.keys(state, "j")
        clicks: list[Any] = []
        filters: tuple[tuple[int, int, int, int], ...] = state.frame.filters
        for _, _, end, tab in filters[::-1]:
            clicked, effects = self.send(state, self.tool.Mouse(0, end - 1, canvas.help_view.y, True))
            self.assertEqual([], effects, "a filter click neither closes nor saves")
            clicks.append(clicked.ui.overlay)
            arrows, _ = self.keys(state, *(["right"] * tab)) if tab else (
                self.tool.with_modal(state, self.tool.Changelog(0)), []
            )
            self.assertEqual(arrows.ui.overlay, clicked.ui.overlay, "the same as ←→, from the filter's top")
        self.assertEqual([self.tool.Changelog(2), self.tool.Changelog(1), self.tool.Changelog(0)], clicks)
        released, _ = self.send(state, self.tool.Mouse(0, filters[1][1], canvas.help_view.y, False))
        self.assertEqual(state.ui.overlay, released.ui.overlay, "a release does nothing")

    def test_a_click_on_the_footers_whats_new_opens_it(self) -> None:
        state = dataclasses.replace(self.screen(), news=self.news())
        row, begin, end = state.frame.version_at
        self.assertEqual((42, 1, 1 + len("Godfather what's new?")), (row, begin, end))
        for x in (begin, end - 1):
            opened, effects = self.send(state, self.tool.Mouse(0, x, row, True))
            self.assertEqual((self.tool.Changelog(), ["load-news"]), (opened.ui.overlay, effects))

    def test_a_click_outside_closes_and_marks_read_too(self) -> None:
        state = dataclasses.replace(self.screen(), news=self.news())
        state, _ = self.keys(state, "v")
        state, effects = self.send(state, self.tool.Mouse(0, 0, 0, True))
        self.assertEqual((None, ["save-read"]), (state.ui.overlay, effects))
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
        when = datetime.datetime(2026, 11, 12, tzinfo=datetime.UTC)
        events = [dict(when=when + datetime.timedelta(seconds=n), verb="granted", count=1, label=f"e{n}", pid=n,
                       delta=1) for n in range(40)]
        state = self.screen(model([entry(0)], events=events))
        state = dataclasses.replace(state, scroll={"events": 5}, glow={})
        state, _ = self.send(state, tool.ModelLoaded(model([entry(0)], events=events + [
            dict(events[0], when=when + datetime.timedelta(seconds=99))]), 8), now=2.0)
        self.assertEqual((6, {}), (state.scroll["events"], state.glow), "kept in place, and no flash")

    def test_each_overlay_owns_its_scroll_and_the_panels_keep_theirs(self) -> None:
        state = dataclasses.replace(self.screen(), scroll={"events": 2})
        state, _ = self.keys(state, "?", "j", "j", "j", "v")
        self.assertEqual(0, state.ui.overlay.scroll, "the changelog opens at its top")
        state, _ = self.keys(state, "?")
        self.assertEqual(0, state.ui.overlay.scroll, "help opens at its top again")
        self.assertEqual({"events": 2}, state.scroll)

    def test_s_v_and_a_version_click_while_settings_is_open(self) -> None:
        tool = self.tool
        state = self.screen()
        version = state.frame.version_at
        state = tool.with_modal(state, tool.SettingsOverlay())
        after, kinds = self.keys(state, "v")
        self.assertEqual((state.ui.overlay, []), (after.ui.overlay, kinds), "v does nothing in Settings")
        after, kinds = self.keys(state, "s")
        self.assertIsNone(after.ui.overlay, "s closes Settings, as Esc does")
        self.assertIn("repaint", kinds)
        after, _ = self.send(state, tool.Mouse(0, version[1], version[0], True))
        self.assertIsNone(after.ui.overlay, "a click on the version is a click outside: it closes Settings")

    def test_a_click_outside_help_closes_it_and_the_wheel_inside_scrolls_it(self) -> None:
        state, _ = self.keys(self.screen(width=100), "?")  # one column: it scrolls
        view = self.tool.overlay_box(state, state.ui.overlay)
        after, _ = self.send(state, self.tool.Mouse(65, view.x + 3, view.y + 3, True))
        self.assertEqual(3, after.ui.overlay.scroll)
        after, _ = self.send(state, self.tool.Mouse(0, 0, 0, True))
        self.assertIsNone(after.ui.overlay)
        after, _ = self.send(state, self.tool.Mouse(0, 0, 0, False))
        self.assertEqual(state.ui.overlay, after.ui.overlay, "a release outside does nothing")


class SelectionTests(Case):
    """fathers#21: the selection is the entry, not the row."""

    def reload(self, state: Any, found: dict[str, Any]) -> Any:
        return self.send(state, self.tool.ModelLoaded(found, 8))[0]

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
                zip(found["holders"], self.tool.entry_ids(found["holders"]), strict=True)}
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
        state, effects = tool.reduce(state, tool.ModelLoaded(model([dict(waiting, kind="held")]), 8), 1.5)
        self.assertEqual([(key[0], "lease")], [effect.key for effect in effects if effect.kind == "load-details"],
                         "a lease's facts, not the request's, within the fresh window")

    def test_the_loads_kept_are_bounded_by_the_model(self) -> None:
        tool = self.tool
        a, b = entry(0), entry(1)
        state = self.screen(model([a, b]))
        for item, identity in zip([a, b], tool.entry_ids([a, b]), strict=True):
            state, _ = tool.reduce(state, tool.DetailsLoaded(tool.details_key(item, identity)), 1.0)
        self.assertEqual(2, len(state.details))
        state, _ = tool.reduce(state, tool.ModelLoaded(model([b]), 8), 2.0)
        self.assertEqual([((11, None), 0)], [key[0] for key in state.details], "a's left with it")

    def test_the_focus_on_details_while_they_load_keeps_their_keys(self) -> None:
        base = self.screen()
        state = dataclasses.replace(base, ui=self.tool.UiState(self.tool.OverviewScreen("details")))
        self.assertEqual({}, state.details, "still loading")
        for key in ("j", "k", "down", "page_down", "home", "G"):
            after, _ = self.keys(state, key)
            self.assertEqual((state.selection, "details"), (after.selection, after.ui.focus), key)


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

    def test_settings_opens_at_once_and_reads_the_tools_on_its_worker(self) -> None:
        """fathers#87 review: no tool is asked on the screen's thread; the sections come as an event."""
        import threading
        import time

        tool = self.tool
        asked: list[Any] = []

        class Screen(tool.Screen):
            def settings_sections(self) -> Any:
                asked.append(threading.current_thread())
                return [settings_model.Section("slotfather", problem="slotfather settings: capacity.json is unreadable")]

        live = tool.Live(0, 1, Screen(), 1.0, "auto")
        self.addCleanup(live.close)
        live.state = self.screen()
        live.send(tool.Key("s"))
        self.assertEqual("settings", live.state.ui.overlay.kind, "open at once")
        self.assertTrue(live.state.ui.overlay.loading)
        deadline = time.monotonic() + 30
        while live.state.ui.overlay.loading and time.monotonic() < deadline:
            live.read(0.1)
            live.take()
        self.assertEqual(1, len(asked))
        self.assertNotIn(threading.current_thread(), asked, "nothing read on the screen's thread")
        self.assertIn("capacity.json is unreadable", " ".join(
            part[1] for line in settings_layout.lines(live.state.ui.overlay, state_module.settings_context(live.state))
            for part in line.parts))

    def test_settings_whose_tools_cannot_be_read_says_why(self) -> None:
        tool = self.tool

        class Screen(tool.Screen):
            def settings_sections(self) -> Any:
                raise OSError("too many open files")

        live = tool.Live(0, 1, Screen(), 1.0, "auto")
        self.addCleanup(live.close)
        live.state = self.screen()
        live.send(tool.Key("s"))
        live.send(live.settings_work(settings_model.Request("open")))
        self.assertFalse(live.state.ui.overlay.loading)
        self.assertIn("too many open files", live.state.ui.overlay.flash.text)

    def test_a_failed_refresh_is_stale_and_keeps_the_model(self) -> None:
        tool = self.tool
        state = self.screen()
        found = state.model
        state, _ = self.send(state, tool.ModelFailed("slotfather: timed out"))
        self.assertEqual(("stale", "slotfather: timed out"), (state.refresh.kind, state.refresh.error))
        self.assertIs(found, state.model)
        state, _ = self.send(state, tool.ModelLoaded(found, 8, "snapshot is 30 s old"))
        self.assertEqual("stale", state.refresh.kind)
        state, _ = self.send(state, tool.ModelLoaded(found, 8))
        self.assertEqual(("live", None), (state.refresh.kind, state.refresh.error))


class FrameTests(Case):
    def test_a_focus_whose_panel_is_not_drawn_goes_back_to_queue(self) -> None:
        base = self.screen(width=156)
        state = dataclasses.replace(base, ui=self.tool.UiState(self.tool.OverviewScreen("events")))
        self.assertEqual("requests", self.draw(state, 82, 40).ui.focus, "EVENTS is not drawn under 83 columns")
        self.assertEqual("events", self.draw(state, 156, 43).ui.focus)


class SettingsModeTests(PanelCase):
    """The Settings mode through the reducer, against the fake Slotfather: the audit's conflict and small-panel
    cases, and what a Settings change does to the app."""

    def test_a_conflict_is_said_and_nothing_is_saved_or_reloaded(self) -> None:
        self.assertEqual(0, self.tool("slots", "4").returncode)
        settings = Settings(self.clock)
        settings.select(SLOTS)
        settings.key("right")  # 5 slots, pending
        self.assertEqual(0, self.tool("slots", "6").returncode)  # another client, not read yet
        kinds = settings.tick(0.5)
        kinds += settings.tick(0.5)
        self.assertNotIn("load-model", kinds, "nothing was saved")
        self.assertEqual({"slots": 6}, self.saved_capacity(), "the other client's change stands")
        self.assertIsNotNone(settings.state.ui.overlay, "the panel stays open")
        self.assertIn("changed elsewhere", settings.text(idle()))

    def test_esc_in_a_small_panel_cancels_first_and_then_closes(self) -> None:
        self.assertEqual(0, self.tool("slots", "4").returncode)
        settings = Settings(self.clock)
        settings.select(SLOTS)
        settings.key("right")
        settings.show(width=20, height=8)
        settings.key("esc")
        self.assertIsNotNone(settings.state.ui.overlay, "the first Esc cancels the pending change")
        self.assertIsNone(settings.panel.pending)
        self.assertIn("repaint", settings.key("esc"))
        self.assertIsNone(settings.state.ui.overlay)
        self.assertEqual({"slots": 4}, self.saved_capacity())

    def test_the_wheel_in_settings_flashes_its_thumb(self) -> None:
        settings = Settings(self.clock, height=16)  # short enough to scroll
        y, x, height, width = settings.geometry().rect
        settings.click(x + width // 2, y + height // 2, 65)
        self.assertGreater(settings.panel.offset, 0)
        self.assertIn("settings", settings.state.glow)

    def test_a_settled_change_and_a_theme_apply_at_once(self) -> None:
        settings = Settings(self.clock)
        settings.select(THEME)
        settings.key("right")
        self.assertEqual("auto", settings.state.theme, "pending, not applied")
        kinds = settings.tick(1.0)
        self.assertEqual("terminal", settings.state.theme)
        self.assertEqual(["apply-theme", "repaint", "remember"], [kind for kind in kinds if kind != "settings"],
                         "besides the reads that came due")
        settings.select(INTERVAL)
        settings.key("right")
        self.assertIn("remember", settings.key("\r"))
        self.assertEqual(2.0, settings.state.interval, "Refresh every follows at once")
        self.assertEqual(["quit"], settings.key("q"))

    def test_a_tool_save_reloads_the_model_and_its_calls_are_effects(self) -> None:
        settings = Settings(self.clock)
        settings.select(SAFETY)
        settings.key("right")
        state, effects = reducer.reduce(settings.state, state_module.Key("\r"), self.clock.now)
        self.assertEqual([("settings", "set")], [(effect.kind, effect.item.kind) for effect in effects])
        answer = settings.screen.settings_call(effects[0].item)
        _, effects = reducer.reduce(state, answer, self.clock.now)
        self.assertIn("load-model", [effect.kind for effect in effects])


class RectTests(unittest.TestCase):
    def test_inset(self) -> None:
        self.assertEqual(Rect(2, 3, 6, 14), Rect(0, 0, 10, 20).inset(2, 3, 2, 3))
        self.assertEqual(Rect(2, 3, 0, 0), Rect(0, 0, 3, 4).inset(2, 3, 2, 3))


if __name__ == "__main__":
    unittest.main()
