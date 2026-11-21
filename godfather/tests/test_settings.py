"""Settings (#7, fathers#87): one section per present tool after this dashboard's own, rendered as text and driven
through the one reducer by keys and the mouse, every settings call answered at once (app.Screen.settings_call)
against a fake `slotfather settings` (tests/fake_slotfather.py) that keeps Slotfather's capacity rules and messages,
fake Lanefather and Junkfather (tests/fake_tool.py), or, with GODFATHER_REAL_SLOTFATHER=/path/to/slotfather, against
that real Slotfather (SF-2) in an isolated SLOTFATHER_DIR.

Snapshots live in tests/snapshots/; `SNAPSHOT_UPDATE=1` rewrites them after a deliberate layout change."""
from __future__ import annotations

import copy
import dataclasses
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Optional, cast
from unittest import mock

from helpers import FakeSlotfather, FakeTool

from godfather import app, reducer, settings_model, state as state_module, tui
from godfather.model import Dashboard
from godfather.text import span
from godfather.views import settings as settings_view

SNAPSHOTS = Path(__file__).resolve().parent / "snapshots"
UPDATE = bool(os.environ.get("SNAPSHOT_UPDATE"))
MODE, SLOTS, SAFETY = ("slotfather", "capacity.mode"), ("slotfather", "capacity.slots"), ("slotfather", "order.safety")
INTERVAL, THEME = ("godfather", "interval"), ("godfather", "theme")


def idle(slots: int = 4) -> dict[str, Any]:
    return {"slots": slots, "free": slots, "used": 0, "waiters": [], "holders": []}


def waiters(*counts: int) -> list[dict[str, Any]]:
    return [{"key": ("w", n), "count": count, "kind": "waiting"} for n, count in enumerate(counts)]


def busy(slots: int, held: int, waiting: int) -> dict[str, Any]:
    return {"slots": slots, "free": max(0, slots - held), "used": held, "waiters": waiters(*[1] * waiting),
            "holders": []}


REAL = os.environ.get("GODFATHER_REAL_SLOTFATHER")


class RealSlotfather:
    """A real Slotfather with FakeSlotfather's interface: an isolated queue, its argv logged by a wrapper."""

    def __init__(self, executable: str) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.calls = self.root / "calls.jsonl"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        wrapper = self.bin / "slotfather"
        wrapper.write_text(f"#!{sys.executable}\nimport json, os, sys\n"
                           f"open({str(self.calls)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
                           f"os.execv({executable!r}, [{executable!r}, *sys.argv[1:]])\n")
        wrapper.chmod(0o755)

    def environ(self) -> dict[str, str]:
        return {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.root), "SLOTFATHER_DIR": str(self.state),
                "SLOTFATHER_LEGACY_DIRS": "", "SLOTFATHER_CORES": "12", "SLOTFATHER_MEMORY_GIB": "64",
                "XDG_CONFIG_HOME": str(self.root / "config")}

    def argv(self) -> list[list[str]]:
        if not self.calls.exists():
            return []
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def read(self) -> dict[str, Any]:
        def load(name: str) -> Any:
            path = self.state / name
            return json.loads(path.read_text()) if path.exists() else None
        return {"capacity": load("capacity.json"), "order": load("order.json") or {}}

    def run(self, *args: str) -> Any:
        return subprocess.run([str(self.bin / "slotfather"), *args], capture_output=True, text=True,
                              env=self.environ())

    def close(self) -> None:
        self.tmp.cleanup()


class Clock:
    """A settable monotonic clock for the reducer's settle, flash, reset-all and read timing."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Settings:
    """Settings in an app state, as the live view drives it: keys, clicks and ticks through reducer.reduce(), every
    settings call it asks for carried out at once (app.Screen.settings_call) and its answer sent back."""

    def __init__(self, clock: Clock, interval: float = 1.0, theme: str = "auto",
                 model: Optional[dict[str, Any]] = None, width: int = 90, height: int = 40) -> None:
        self.clock, self.screen = clock, app.Screen()
        self.deferred = False  # True: settings calls wait in `waiting` until answer() (a slow tool, the live worker)
        self.waiting: list[Any] = []
        self.state = state_module.AppState(interval=interval, theme=theme, model=cast(Dashboard, model or idle()),
                                           frame=state_module.Frame(width, height))
        self.send(state_module.SettingsOpened(tuple(self.screen.settings_sections())))

    def send(self, event: Any) -> list[str]:
        """The effects' kinds, the answers' too; each settings call is answered before this returns."""
        self.state, effects = reducer.reduce(self.state, event, self.clock.now)
        kinds = [effect.kind for effect in effects]
        for effect in effects:
            if effect.kind == "settings" and self.deferred:
                self.waiting.append(effect.item)
            elif effect.kind == "settings":
                kinds += self.send(self.screen.settings_call(effect.item))
        return kinds

    def answer(self, count: int = 1) -> list[str]:
        """The oldest `count` deferred calls carried out and answered, in order, as the live worker does."""
        kinds: list[str] = []
        for _ in range(count):
            kinds += self.send(self.screen.settings_call(self.waiting.pop(0)))
        return kinds

    def key(self, *names: str) -> list[str]:
        kinds: list[str] = []
        for name in names:
            kinds += self.send(state_module.Key(name))
        return kinds

    def click(self, x: int, y: int, button: int = 0, pressed: bool = True) -> list[str]:
        return self.send(state_module.Mouse(button, x, y, pressed))

    def tick(self, seconds: float = 0.0) -> list[str]:
        self.clock.now += seconds
        return self.send(state_module.Tick())

    def show(self, model: Optional[dict[str, Any]] = None, width: Optional[int] = None,
             height: Optional[int] = None) -> None:
        """The model and screen size the next keys see, as a new snapshot and a drawn frame give them."""
        if model is not None:
            self.state = dataclasses.replace(self.state, model=model)
        frame = self.state.frame
        self.send(state_module.Drawn(state_module.Frame(width or frame.width, height or frame.height)))

    @property
    def panel(self) -> settings_model.Panel:
        overlay = self.state.overlay
        assert isinstance(overlay, settings_model.Panel), overlay
        return overlay

    def context(self) -> settings_model.Context:
        return state_module.settings_context(self.state)

    def select(self, focus: tuple[str, str]) -> None:
        self.state = dataclasses.replace(self.state, overlay=dataclasses.replace(self.panel, focus=focus,
                                                                                 reveal=True))

    def geometry(self) -> settings_model.Geometry:
        return settings_model.geometry(self.panel, self.context())

    def canvas(self, model: Optional[dict[str, Any]] = None, width: Optional[int] = None,
               height: Optional[int] = None) -> tui.Canvas:
        self.show(model, width, height)
        canvas = tui.Canvas(self.state.frame.width, self.state.frame.height)
        settings_view.draw(canvas, self.panel, self.context())
        return canvas

    def text(self, model: Optional[dict[str, Any]] = None, width: Optional[int] = None,
             height: Optional[int] = None) -> str:
        """The panel's own rows of the rendered screen, as text."""
        canvas = self.canvas(model, width, height)
        y, x, h, w = self.geometry().rect
        return "\n".join(line[x:x + w].rstrip() for line in canvas.render(False).split("\n")[y:y + h])

    def hit(self, focus: tuple[str, str]) -> tuple[int, int]:
        """Where a click selects `focus`: (x, y)."""
        rect = self.geometry().rect
        return rect.x + 4, next(y for found, y in self.geometry().hits() if found == focus)

    @property
    def focus(self) -> tuple[str, str]:
        return self.panel.focus


class PanelCase(unittest.TestCase):
    def setUp(self) -> None:
        self.real = bool(REAL)
        self.fake: Any = RealSlotfather(REAL) if REAL else FakeSlotfather()
        self.addCleanup(self.fake.close)
        patcher = mock.patch.dict(os.environ, self.fake.environ(), clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.clock = Clock()

    def tool(self, *args: str) -> Any:
        """What `slotfather slots N`, `slots memory G` and `order ...` saved, written into the fake's state."""
        if self.real:
            return self.fake.run(*args)
        state = self.fake.read()
        if args[0] == "slots":
            state["capacity"] = {"slots": int(args[1])} if len(args) == 2 else \
                {"mode": args[1], "gib_per_slot": int(args[2]), "slots": 64 // int(args[2])}
        else:
            rest = list(args[1:])
            if rest and not rest[0].startswith("--"):
                state["order"]["order"] = rest.pop(0)
            while rest:
                state["order"][rest[0][2:]] = float(rest[1])
                rest = rest[2:]
        self.fake.write(state)
        return mock.Mock(returncode=0)

    def writes(self, fake: Any = None) -> list[list[str]]:
        """The settings writes a tool was asked for."""
        return [argv for argv in (fake or self.fake).argv() if argv[:2] in (["settings", "set"], ["settings", "reset"])]

    def open(self, interval: float = 1.0, **fields: Any) -> Settings:
        found = Settings(self.clock, interval, **fields)
        self.addCleanup(found.screen.close)
        return found

    def assert_snapshot(self, name: str, text: str) -> None:
        path = SNAPSHOTS / f"settings-{name}.txt"
        if UPDATE:
            path.parent.mkdir(exist_ok=True)
            path.write_text(text + "\n")
        self.assertEqual(path.read_text(), text + "\n", f"{path.name} differs; SNAPSHOT_UPDATE=1 rewrites it")

    def saved_capacity(self) -> Optional[dict[str, Any]]:
        return self.fake.read()["capacity"]

    def saved_order(self) -> dict[str, Any]:
        return self.fake.read()["order"]

    def settle(self, settings: Settings) -> list[str]:
        return settings.tick(settings_model.SETTLE_SECONDS)


class RenderTests(PanelCase):
    def test_the_list_keeps_a_blank_row_over_and_under_it_also_scrolled(self) -> None:
        settings = self.open()
        settings.show(idle(), 90, 20)
        for presses in (0, 30):  # the top, then far down the list
            settings.key(*["down"] * presses)
            canvas = settings.canvas()
            view = settings.geometry()
            y, x, height, width = view.rect
            rows = canvas.render(False).split("\n")
            first, last = y + 2, y + 1 + view.visible
            self.assertFalse(rows[y + 1][x + 1:x + width - 1].strip(), "a blank row under the top border")
            self.assertFalse(rows[last + 1][x + 1:x + width - 1].strip(), "a blank row under the list")
            thumb = [row for row in range(y, y + height) if rows[row][x + width - 2] in "┃╻╹"]
            self.assertTrue(thumb, "the list overflows at this height")
            self.assertGreaterEqual(thumb[0], first)
            self.assertLessEqual(thumb[-1], last)
            self.assertEqual({"dim"}, {canvas.cells[row][x + width - 2][1] for row in thumb}, "muted at rest")
            lit = tui.Canvas(90, 20)
            lit.glow = {"settings"}
            settings_view.draw(lit, settings.panel, settings.context())
            self.assertEqual({"text"}, {lit.cells[row][x + width - 2][1] for row in thumb}, "the foreground scrolling")

    def test_idle(self) -> None:
        self.assert_snapshot("idle", self.open().text(idle()))

    def test_busy_while_a_shrink_drains(self) -> None:
        self.assertEqual(0, self.tool("slots", "2").returncode)
        settings = self.open()
        settings.select(SLOTS)
        self.assert_snapshot("draining", settings.text(busy(2, 4, 3)))

    def test_editing_a_field_previews_what_a_shrink_drains(self) -> None:
        self.assertEqual(0, self.tool("slots", "4").returncode)
        if not self.real:
            self.fake.update(held=4, waiting=[1])  # the preview is Slotfather's, from its queue
        settings = self.open(model=busy(4, 4, 1))
        settings.key("down", "\r", "1")
        self.assert_snapshot("editing", settings.text(busy(4, 4, 1)))
        self.assertEqual({"slots": 4}, self.saved_capacity(), "typing saves nothing")

    def test_invalid_input_is_shown_once_under_its_row_and_never_saved(self) -> None:
        self.assertEqual(0, self.tool("slots", "4").returncode)
        settings = self.open()
        settings.key("down", "\r", "0")
        error = "✗ Enter a whole number of slots, at least 1."
        self.assertEqual(1, settings.text(idle()).count(error))
        settings.key("\r")
        self.assertTrue(settings.panel.editing, "the refused value stays in the field")
        text = settings.text(idle())
        self.assertEqual(1, text.count("Enter a whole number of slots"), "once, under the row; not in the notes too")
        self.assert_snapshot("invalid", text)
        self.assertEqual({"slots": 4}, self.saved_capacity())

    def test_status_is_read_only_and_one_footer_holds_every_key_hint(self) -> None:
        settings = self.open()
        text = settings.text(busy(4, 2, 1))
        rows = text.split("\n")
        status = settings.geometry().rect.y + next(n for n, line in enumerate(rows) if "environment" in line)
        hits = settings.geometry().hits()
        self.assertTrue(all(y != status for _, y in hits), "STATUS rows are not selectable")
        self.assertNotIn(("slotfather", "env"), [focus for focus, _ in hits])
        self.assertEqual(1, text.count("esc close"))
        for button in ("[Apply]", "[Defaults]", "[Reload]", "[Cancel]", "Draft"):
            self.assertNotIn(button, text)
        self.assertIn("esc close", rows[-2], "the hint line is the panel's last row")
        hints = rows[-2].strip("│ ")
        self.assertLessEqual(abs(rows[-2].index(hints) + len(hints) / 2 - len(rows[-2]) / 2), 2, "centred")

    def test_every_rule_has_one_length_and_the_list_sits_two_cells_in(self) -> None:
        settings = self.open()
        text = settings.text(idle(), height=60)
        rows = text.split("\n")
        rules = [line for line in rows if "────" in line and not line.startswith(("╭", "╰"))]
        self.assertGreaterEqual(len(rules), 2, "between sections and over the notes")
        self.assertEqual(1, len({line.strip("│ ") for line in rules}), "every rule one length")
        self.assertEqual(1, len({line.index("─") for line in rules}), "every rule from one column")
        for line in rows[1:-1]:
            self.assertTrue(line.startswith(("│  ", "┃  ")), f"two cells of padding: {line!r}")
        self.assertTrue(any(line.startswith("│  GODFATHER") for line in rows))

    def test_status_labels_are_dim_and_the_selected_field_alone_is_highlighted(self) -> None:
        settings = self.open()
        canvas = settings.canvas(idle(), 90, 40)
        rows = ["".join(str(cell[0]) for cell in line) for line in canvas.cells]
        queue = next(n for n, line in enumerate(rows) if "queue  " in line)
        self.assertEqual("dim", canvas.cells[queue][rows[queue].index("queue")][1])
        self.assertFalse(any(cell[2] for cell in canvas.cells[queue]), "status rows have no selection")
        selected = [n for n, line in enumerate(canvas.cells) if any(cell[2] for cell in line)]
        self.assertEqual(1, len(selected))
        self.assertIn("▶ Capacity ", rows[selected[0]])
        value = rows[selected[0]].index("Automatic")
        self.assertEqual("accent", canvas.cells[selected[0]][value][1], "editable values stand out from status")

    def test_the_selected_field_has_a_one_line_description(self) -> None:
        settings = self.open()
        for focus, line in ((MODE, "Automatic: the lower of the CPU and memory"),
                            (SAFETY, "Multiplier on expected runtimes"), (INTERVAL, "this dashboard only"),
                            (THEME, "this dashboard only"), (("slotfather", "order.idle_cpu"), "💤")):
            settings.select(focus)
            self.assertIn(line, settings.text(idle()))

    def test_the_environment_tightened_order_shows_beside_the_saved_one(self) -> None:
        with mock.patch.dict(os.environ, {"SLOTFATHER_QUEUE": "window"}):
            text = self.open().text(idle(), height=44)
        self.assertRegex(text, r"Queue order +Backfill +effective here: Arrival")

    def test_durations_read_as_hours_minutes_and_seconds(self) -> None:
        text = self.open().text(idle(), height=60)
        for label, value in (("Unknown job runtime", "1h"), ("Interactive head start", "30m"),
                             ("Normal promoted after", "1h"), ("Idle for", "5m"), ("Ask to yield after", "2m"),
                             ("Overtaking stops after", "never"), ("Refresh every", "1s")):
            self.assertRegex(text, rf"{re.escape(label)} +{re.escape(value)}\b")
        self.assertNotRegex(text, r"\d+ s\b", "no raw seconds")

    def test_a_short_terminal_scrolls_the_panel_and_an_outside_click_closes_it(self) -> None:
        settings = self.open()
        text = settings.text(idle(), width=40, height=18)
        self.assertIn("SETTINGS", text)
        self.assertRegex(text, r"[┃╻╹]│", "a scrollbar, no row counts")
        self.assertNotRegex(text, r"↓ \d+ more|↑ \d+ above")
        self.assertEqual(["settings"], [settings.panel.kind])
        self.assertEqual(MODE, settings.focus)
        for _ in range(12):
            settings.click(10, 5, 65)
        self.assertIn("Ask to yield", settings.text(idle(), width=40, height=18))
        self.assertIn("esc close", settings.text(idle(), width=40, height=18).split("\n")[-2],
                      "a narrow footer keeps the way out")
        settings.click(0, 0, 65)
        self.assertIsNotNone(settings.state.overlay, "the wheel outside does nothing")
        self.assertIn("repaint", settings.click(0, 0))
        self.assertIsNone(settings.state.overlay, "a click outside closes the panel")
        self.assertEqual([], self.writes(), "viewing settings saves nothing")


class KeyTests(PanelCase):
    def test_an_arrow_change_shows_at_once_and_saves_after_the_keys_settle(self) -> None:
        settings = self.open()
        settings.key("right")
        self.assertIsNone(self.saved_capacity(), "nothing is saved while the keys may still move")
        text = settings.text(idle())
        self.assertRegex(text, r"Capacity +‹ Fixed › +pending")
        self.assertIn("→ 4 slots", text)
        self.assertIn("… Capacity Fixed · saves in 1 s · Enter now · Esc cancels", text)
        settings.tick(0.5)
        self.assertEqual({"slots": 4}, {"slots": 4})
        self.assertIsNone(self.saved_capacity(), "not yet")
        self.assertIn("load-model", self.settle(settings))
        self.assertEqual({"slots": 4}, self.saved_capacity(), "fixed keeps the current count")
        flash = [line for line in settings.text(idle()).split("\n") if "✓" in line]
        self.assertEqual(1, len(flash))
        self.assertRegex(flash[0], r"│  ✓ Saved · capacity 4 slots +│$", "one short line")

    def test_stepping_through_memory_never_saves_it_or_grants_by_it(self) -> None:
        settings = self.open()
        for _ in range(3):  # Fixed, Memory, CPU + memory
            settings.tick(0.4)
            settings.key("right")
            self.assertEqual([], self.writes(), "each step restarts the settle")
        self.settle(settings)
        self.assertEqual([["settings", "set", "capacity.mode", "lower", "--revision"]],
                         [argv[:5] for argv in self.writes()], "one save, of the value the keys settled on")
        self.assertEqual({"slots": 4, "mode": "lower", "gib_per_slot": 4}, self.saved_capacity())

    def test_enter_saves_a_pending_change_now_and_escape_cancels_one(self) -> None:
        settings = self.open()
        settings.select(SAFETY)
        settings.key("right")
        self.assertIn("load-model", settings.key("\r"))
        self.assertEqual(3.5, self.saved_order()["safety"])
        settings.key("right")
        settings.key("esc")
        self.assertIsNotNone(settings.state.overlay, "the first Esc cancels the pending change")
        self.settle(settings)
        self.assertEqual(3.5, self.saved_order()["safety"])
        self.assertIn("Change cancelled", settings.text(idle()))
        settings.key("esc")
        self.assertIsNone(settings.state.overlay)

    def test_one_cursor_through_every_section_with_labels_that_read_alone(self) -> None:
        settings = self.open()
        self.assertEqual([INTERVAL, THEME, MODE] + [("slotfather", "order." + name) for name in (
            "order", "safety", "fallback", "overrun", "limit", "interactive", "batch", "aging_batch",
            "aging_normal", "warn_wait", "reservation_timeout", "memory_share", "memory_default_mb",
            "idle_cpu", "idle_after", "yield_after")],
            [row.focus for row in settings_model.selectable(settings.panel, settings.context())])
        text = settings.text(idle(), height=60)
        for label, value in (("Capacity", "Automatic"), ("Queue order", "Backfill"), ("Backfill margin", "3×"),
                             ("Overrun estimate", "0.5×"), ("Batch held back", "30m"),
                             ("Batch promoted after", "30m"), ("Warn after waiting", "30m"),
                             ("Reservation timeout", "30m"), ("Idle when below", "0.1 cores"),
                             ("Colour theme", "Auto")):
            self.assertRegex(text, rf"{re.escape(label)} +(‹ )?{re.escape(value)}")
        self.assertNotRegex(text, r"\((s|cores)\)", "no units in the labels")
        self.assertLess(text.index("GODFATHER"), text.index("SLOTFATHER"), "this dashboard's section first")

    def test_moving_on_saves_a_pending_change(self) -> None:
        settings = self.open()
        settings.select(SAFETY)
        settings.key("right")
        self.assertIn("load-model", settings.key("down"))
        self.assertEqual((("slotfather", "order.fallback"), 3.5), (settings.focus, self.saved_order()["safety"]))

    def test_enter_edits_a_number_and_enter_saves_it(self) -> None:
        self.assertEqual(0, self.tool("slots", "4").returncode)
        settings = self.open()
        settings.key("down", "\r")
        self.assertTrue(settings.panel.editing)
        settings.key("6")
        self.assertIn("[6▏]", settings.text(idle()))
        self.assertIn("load-model", settings.key("\r"))
        self.assertFalse(settings.panel.editing)
        self.assertEqual({"slots": 6}, self.saved_capacity())

    def test_typed_capacity_has_an_upper_bound_and_a_large_grow_says_what_starts(self) -> None:
        self.assertEqual(0, self.tool("slots", "4").returncode)
        model = {**busy(4, 4, 3), "waiters": waiters(2, 6, 1)}
        if not self.real:
            self.fake.update(held=4, waiting=[2, 6, 1])  # the preview is Slotfather's, from its queue
        settings = self.open(model=model)
        settings.key("down", "\r", "4", "9")
        self.assertIn("✗ At most 48 slots (4 per core).", settings.text(model))
        settings.key("\r")
        self.assertEqual({"slots": 4}, self.saved_capacity())
        settings.key("\x15", "1", "2")
        self.assertIn("→ 12 slots · will start 2 waiting", settings.text(model))

    def test_typing_a_digit_on_a_number_starts_editing_with_it(self) -> None:
        settings = self.open()
        settings.select(SAFETY)
        settings.key("5")
        self.assertEqual((True, "5"), (settings.panel.editing, settings.panel.buffer))
        self.assertIn("load-model", settings.key("\r"))
        self.assertEqual(5.0, self.saved_order()["safety"])

    def test_escape_leaves_edit_mode_without_saving_and_then_closes(self) -> None:
        self.assertEqual(0, self.tool("slots", "4").returncode)
        settings = self.open()
        settings.key("down", "\r", "8", "esc")
        self.assertFalse(settings.panel.editing)
        self.assertEqual({"slots": 4}, self.saved_capacity())
        self.assertIn("Edit cancelled", settings.text(idle()))
        settings.key("esc")
        self.assertIsNone(settings.state.overlay)

    def test_keys_while_editing_stay_in_the_field(self) -> None:
        settings = self.open()
        settings.select(INTERVAL)
        settings.key("\r")
        for name in ("q", "s", "D", "down"):
            self.assertEqual([], settings.key(name))
        self.assertEqual((True, "1"), (settings.panel.editing, settings.panel.buffer))
        settings.key("\x7f")
        self.assertEqual("", settings.panel.buffer)
        settings.key("\r")
        self.assertTrue(settings.panel.editing, "an empty field is refused")

    def test_the_interval_steps_through_a_tenth_and_whole_seconds(self) -> None:
        settings = self.open(0.1)
        settings.select(INTERVAL)
        seen: list[float] = []
        for name in ("right", "right", "right", "left", "left", "left"):
            settings.key(name, "\r")
            seen.append(settings.state.interval)
        self.assertEqual([1.0, 2.0, 3.0, 2.0, 1.0, 0.1], seen)
        self.assertEqual([], self.writes(), "a local setting never goes to Slotfather")

    def test_reset_restores_the_selected_field_alone(self) -> None:
        self.assertEqual(0, self.tool("slots", "6").returncode)
        self.assertEqual(0, self.tool("order", "window", "--safety", "4").returncode)
        settings = self.open()
        settings.select(SAFETY)
        self.assertIn("load-model", settings.key("d"))
        self.assertEqual("window", self.saved_order()["order"])
        self.assertIn(self.saved_order().get("safety"), (None, 3.0),
                      "settings reset order.safety: back to Slotfather's default, dropped or written")
        self.assertRegex(settings.text(idle()), r"Backfill margin +3×")
        self.assertEqual({"slots": 6}, self.saved_capacity())
        self.assertIn(["settings", "reset", "order.safety", "--revision"], [argv[:4] for argv in self.writes()],
                      "a reset carries the row's revision")
        settings.select(SLOTS)
        self.assertIn("load-model", settings.key("d"))
        self.assertIsNone(self.saved_capacity(), "a capacity row resets to automatic capacity")
        self.assertEqual(MODE, settings.focus, "Slots is gone: its section's first row")

    def test_a_field_that_disappears_takes_its_focus_and_keys_with_it(self) -> None:
        self.assertEqual(0, self.tool("slots", "6").returncode)
        settings = self.open()
        settings.select(SLOTS)
        settings.key("d", "5")  # one batch: Slots is gone after the reset
        self.assertEqual(MODE, settings.focus)
        self.assertFalse(settings.panel.editing, "a choice takes no typed value")
        self.assertIsNone(self.saved_capacity())

    def test_a_memory_mode_keeps_the_cursor_on_its_amount(self) -> None:
        self.assertEqual(0, self.tool("slots", "6").returncode)
        settings = self.open()
        settings.select(SLOTS)
        self.assertEqual(0, self.tool("slots", "memory", "8").returncode)  # another client
        settings.tick(settings_model.READ_EVERY["slotfather"])
        self.assertEqual(("slotfather", "capacity.gib_per_slot"), settings.focus, "the amount that took its place")

    def test_reset_all_asks_twice_and_resets_the_selected_section_only(self) -> None:
        self.assertEqual(0, self.tool("slots", "6").returncode)
        self.assertEqual(0, self.tool("order", "window").returncode)
        settings = self.open(5.0)
        settings.key("D")
        self.assertIn("Press D again to reset every Slotfather setting", settings.text(idle()))
        settings.key("down")
        settings.key("D")
        self.assertNotIn("reset", [argv[1] for argv in self.writes()], "another key in between withdraws it")
        settings.tick(settings_model.FLASH_SECONDS + 0.1)
        settings.key("D")
        self.assertIsNotNone(self.saved_capacity(), "the question expires with its flash")
        settings.tick(1)
        self.assertIn("load-model", settings.key("D"))
        self.assertEqual((None, {}), (self.saved_capacity(), self.saved_order()))
        self.assertEqual(5.0, settings.state.interval, "this dashboard's section is left as it was")
        self.assertIn(["settings", "reset", "--all", "--json"], self.writes())
        settings.select(INTERVAL)
        settings.key("D", "D")
        self.assertEqual(1.0, settings.state.interval, "D twice in this dashboard's section resets its own")
        self.assertEqual(1, sum(argv[2] == "--all" for argv in self.writes()), "and no tool's")

    def test_the_refresh_interval_stays_local(self) -> None:
        settings = self.open()
        settings.select(INTERVAL)
        settings.key("\r", "6", "1")
        self.assertIn("Enter seconds from 0.1 to 60", settings.text(idle()))
        settings.key("\r")
        self.assertTrue(settings.panel.editing)
        kinds = settings.key("\x15", "5", "\r")
        self.assertEqual(5.0, settings.state.interval)
        self.assertIn("remember", kinds)
        self.assertEqual([], self.writes(), "a local setting never goes to Slotfather")

    def test_a_click_selects_a_field_a_second_click_edits_and_another_cancels_the_edit(self) -> None:
        settings = self.open()
        settings.show(idle(), 90, 44)
        settings.click(*settings.hit(INTERVAL))
        self.assertEqual((INTERVAL, False), (settings.focus, settings.panel.editing))
        settings.click(*settings.hit(INTERVAL))
        self.assertTrue(settings.panel.editing)
        settings.key("7")
        settings.click(*settings.hit(SAFETY))
        self.assertEqual((SAFETY, False, 1.0), (settings.focus, settings.panel.editing, settings.state.interval))
        self.assertIn("Edit cancelled · nothing was saved", settings.text(idle()))

    def test_below_the_minimum_size_only_the_ways_out_work(self) -> None:
        settings = self.open()
        self.assertIn("Enlarge", settings.text(idle(), width=28, height=10))
        for name in ("right", "d", "D", "D", "\r", "5", "\r"):
            self.assertEqual([], settings.key(name))
        self.assertEqual((False, None), (settings.panel.editing, settings.panel.pending))
        self.assertEqual([], self.writes())
        self.assertEqual(["quit"], settings.key("q"))
        settings.key("esc")
        self.assertIsNone(settings.state.overlay)

    def test_changes_from_another_client_show_while_open_and_are_not_overwritten(self) -> None:
        settings = self.open()
        self.assertEqual(0, self.tool("order", "window").returncode)
        settings.tick(settings_model.READ_EVERY["slotfather"])
        self.assertRegex(settings.text(idle()), r"Queue order +Arrival")
        settings.select(SAFETY)
        settings.key("right", "\r")
        self.assertEqual(("window", 3.5), (self.saved_order()["order"], self.saved_order()["safety"]))

    def test_a_capacity_saved_elsewhere_while_typing_is_not_overwritten(self) -> None:
        self.assertEqual(0, self.tool("slots", "4").returncode)
        settings = self.open()
        settings.key("down", "\r", "3")
        self.assertEqual(0, self.tool("slots", "6").returncode)
        self.assertNotIn("load-model", settings.key("\r"))
        self.assertEqual({"slots": 6}, self.saved_capacity(), "the other save stands")
        text = settings.text(idle())
        self.assertEqual(1, text.count("✗ Capacity changed elsewhere: now 6 slots · nothing was saved"))
        self.assertRegex(text, r"Slots +6")
        settings.key("\r", "3")
        self.assertIn("load-model", settings.key("\r"), "once it has read the new value it saves")
        self.assertEqual({"slots": 3}, self.saved_capacity())

    def test_two_dashboards_do_not_overwrite_each_others_capacity(self) -> None:
        self.assertEqual(0, self.tool("slots", "4").returncode)
        first, second = self.open(), self.open()
        for settings in (first, second):
            settings.select(SLOTS)
        first.key("right")
        self.assertIn("load-model", first.key("\r"))
        second.key("left")
        self.assertNotIn("load-model", second.key("\r"))
        self.assertEqual({"slots": 5}, self.saved_capacity())
        self.assertIn("Capacity changed elsewhere: now 5 slots", second.text(idle()))

    def test_a_pending_change_is_never_saved_over_a_change_made_elsewhere(self) -> None:
        # The audit's probe: a refresh during the settle took the other client's bytes and mode as its own, and
        # "5 slots" was saved as 5 GiB per slot over the other client's change.
        self.assertEqual(0, self.tool("slots", "4").returncode)
        settings = self.open()
        settings.select(SLOTS)
        settings.tick(0.5)
        settings.key("right")  # 5 slots, pending
        self.assertEqual(0, self.tool("slots", "memory", "8").returncode)  # another client
        self.assertNotIn("load-model", settings.tick(settings_model.READ_EVERY["slotfather"]))
        self.assertNotIn("load-model", self.settle(settings), "nothing is saved")
        saved = self.saved_capacity()
        assert saved is not None
        self.assertEqual(("memory", 8), (saved["mode"], saved["gib_per_slot"]), "the other client's change stands")
        self.assertEqual(1, settings.text(idle()).count("changed elsewhere"), "and the panel says so, once")

    def test_a_refused_save_on_moving_on_stays_visible(self) -> None:
        self.assertEqual(0, self.tool("slots", "4").returncode)
        settings = self.open()
        settings.select(SLOTS)
        settings.key("right")
        self.assertEqual(0, self.tool("slots", "6").returncode)  # elsewhere, not read yet
        self.assertNotIn("load-model", settings.key("down"))
        self.assertEqual({"slots": 6}, self.saved_capacity())
        self.assertIn("changed elsewhere", settings.text(idle()), "the refusal is not wiped by the move")

    def test_a_refused_change_says_why_and_keeps_the_value(self) -> None:
        if self.real:
            self.skipTest("the real Slotfather reads this machine's memory")
        settings = self.open()
        settings.key("right", "\r")  # Fixed, 4 slots
        self.fake.update(memory_gib=None)  # Slotfather cannot tell the machine's memory
        settings.tick(settings_model.READ_EVERY["slotfather"])
        settings.key("right")  # Memory needs the machine's memory
        self.assertIn("memory is unknown", settings.text(idle()))
        self.assertNotIn("load-model", settings.key("\r"))
        self.assertIn("memory is unknown", settings.text(idle()))
        self.assertEqual({"slots": 4}, self.saved_capacity())

    def test_status_shows_the_capacity_the_queue_read_under_its_lock(self) -> None:
        settings = self.open()
        basis: dict[str, Any] = dict(settings_model.basis_of(settings.panel.sections[0].reply))
        model: dict[str, Any] = {**idle(9), "basis": {**basis, "slots": 9, "mode": "fixed", "saved": True}}
        self.assertRegex(settings.text(model), r"capacity +9 slots · saved, fixed count")


class ContractTests(PanelCase):
    """What Settings asks each tool, beyond what the flows above show."""

    def test_a_pending_capacity_change_carries_the_revision_it_was_chosen_under(self) -> None:
        settings = self.open()
        revision = settings_model.row_of(settings.panel, settings.context(), MODE).revision  # type: ignore[union-attr]
        settings.key("right", "\r")
        self.assertEqual([["settings", "set", "capacity.mode", "fixed", "--revision", revision, "--json"]],
                         self.writes())

    def test_an_order_knob_carries_its_rows_revision(self) -> None:
        settings = self.open()
        settings.select(SAFETY)
        revision = settings_model.row_of(settings.panel, settings.context(), SAFETY).revision  # type: ignore
        settings.key("right", "\r")
        self.assertEqual([["settings", "set", "order.safety", "3.5", "--revision", revision, "--json"]],
                         self.writes())

    def test_a_read_only_queue_shows_its_rows_with_why_and_saves_nothing(self) -> None:
        if self.real:
            self.skipTest("another user's queue needs another user")
        self.fake.update(writable=False)
        settings = self.open()
        text = settings.text(idle(), height=60)
        self.assertIn("SLOTFATHER · read-only here", text)
        self.assertIn("belongs to uid 502", text)
        self.assertRegex(text, r"Backfill margin +3× +read-only")
        self.assertNotIn("slotfather", {focus[0] for focus, _ in settings.geometry().hits()}, "never editable")
        self.assertEqual(INTERVAL, settings.focus, "the cursor goes to the rows that can change")
        settings.key("right", "d", "D", "D")
        self.assertEqual([], self.writes())

    def test_a_queue_that_turned_read_only_refuses_with_its_reason_under_the_row(self) -> None:
        if self.real:
            self.skipTest("another user's queue needs another user")
        settings = self.open()
        settings.select(SAFETY)
        self.fake.update(writable=False)  # after the read: the set is refused (exit 4)
        self.assertNotIn("load-model", settings.key("right", "\r"))
        text = settings.text(idle(), height=60)
        self.assertEqual(1, text.count("belongs to uid 502"), "said once")
        self.assertEqual({}, self.saved_order())

    def test_without_a_settings_contract_its_section_says_why(self) -> None:
        state = self.fake.read()
        state["capabilities"] = {"v": 1, "tool": "slotfather", "version": "0.1.0", "dashboard": {
            "v": 1, "tag": "slot", "snapshot": {"argv": ["status", "--json", "--full"], "versions": [2], "poll": 1}}}
        self.fake.write(state)
        settings = self.open()
        self.assertEqual([INTERVAL, THEME], [row.focus for row in settings_model.selectable(settings.panel,
                                                                                            settings.context())])
        text = settings.text(idle())
        self.assertIn("slotfather 0.1.0: no settings contract; update slotfather", text)
        self.assertRegex(text, r"Refresh every +1s")
        self.assertNotIn("Capacity", text)
        self.assertEqual([], [argv for argv in self.fake.argv() if argv[:1] == ["settings"]], "never asked")

    def test_an_absent_tool_has_no_section(self) -> None:
        text = self.open().text(idle(), height=60)
        self.assertNotIn("LANEFATHER", text)
        self.assertNotIn("JUNKFATHER", text)

    def test_a_read_older_than_a_save_is_dropped(self) -> None:
        settings = self.open()
        found = settings.panel.sections[0]
        older = state_module.SettingsRead("slotfather", found.generation - 1, {"v": 1, "sections": []})
        settings.send(older)
        self.assertEqual(found.reply, settings.panel.sections[0].reply, "a reply nobody needs is not taken")
        self.assertIsNone(settings.panel.flash)

    def test_a_failed_read_keeps_the_rows_and_says_why(self) -> None:
        settings = self.open()
        found = settings.panel.sections[0]
        settings.send(state_module.SettingsRead("slotfather", found.generation, None,
                                                "slotfather settings took longer than 5 s"))
        text = settings.text(idle())
        self.assertIn("slotfather settings took longer than 5 s", text)
        self.assertRegex(text, r"Capacity +‹ Automatic ›")

    def test_a_stale_snapshot_says_why_in_status(self) -> None:
        settings = self.open()
        settings.state = dataclasses.replace(settings.state, refresh=state_module.Refresh(
            "stale", "slotfather status --json --full took longer than 5 s"))
        self.assertRegex(settings.text(idle()), r"slotfather +slotfather status --json --full took longer")

    def test_reads_come_every_second_for_slotfather_and_never_while_typing(self) -> None:
        settings = self.open()
        reads = lambda: sum(argv == ["settings", "--json"] for argv in self.fake.argv())  # noqa: E731
        before = reads()
        settings.tick(0.5)
        self.assertEqual(before, reads())
        settings.tick(0.5)
        self.assertEqual(before + 1, reads())
        settings.select(SAFETY)
        settings.key("\r")
        settings.tick(5)
        self.assertEqual(before + 1, reads(), "not under a value being typed")


class DeferredTests(PanelCase):
    """fathers#87 review: what happens while a tool has not answered yet, as on the live worker."""

    def test_a_second_change_made_before_the_first_save_answers_is_not_a_conflict(self) -> None:
        settings = self.open()
        settings.select(SAFETY)
        settings.deferred = True
        settings.key("right")
        settings.answer(len(settings.waiting))  # its check
        settings.key("\r")  # saves 3.5: the set is under way, not answered
        self.assertEqual(["set"], [request.kind for request in settings.waiting])
        settings.select(("slotfather", "order.fallback"))
        settings.key("right")
        settings.answer(len(settings.waiting))  # the first set and the second change's check
        settings.key("\r")
        while settings.waiting:
            settings.answer()
        self.assertEqual((3.5, 3660.0), (self.saved_order()["safety"], self.saved_order()["fallback"]),
                         "both saved")
        self.assertNotIn("changed", settings.text(idle()), "the user's own changes never conflict")
        revisions = [argv[argv.index("--revision") + 1] for argv in self.writes()]
        self.assertEqual(2, len(set(revisions)), "the second went with the revision the first save gave")

    def test_a_change_made_while_a_save_is_under_way_waits_for_its_answer(self) -> None:
        settings = self.open()
        settings.select(SAFETY)
        settings.deferred = True
        settings.key("right")
        settings.answer(len(settings.waiting))
        settings.key("\r", "right")  # the set of 3.5 under way, then 4 and its check
        self.assertEqual(["set", "check"], [request.kind for request in settings.waiting])
        settings.send(settings.screen.settings_call(settings.waiting.pop(1)))  # the check answers first
        settings.key("\r")
        self.assertEqual(["set"], [request.kind for request in settings.waiting], "one set at a time per tool")
        while settings.waiting:
            settings.answer()
        self.assertEqual(4.0, self.saved_order()["safety"])
        self.assertEqual(2, len(self.writes()))

    def test_a_value_whose_check_refused_or_has_not_answered_is_never_sent(self) -> None:
        if self.real:
            self.skipTest("the fake's refusal")
        settings = self.open()
        settings.key("right")  # Capacity Fixed
        settings.key("\r")
        self.fake.update(memory_gib=None)  # Memory needs the machine's memory: its check refuses
        settings.tick(settings_model.READ_EVERY["slotfather"])
        settings.deferred = True
        settings.key("right")  # Memory, its check not answered
        settings.tick(settings_model.SETTLE_SECONDS)
        self.assertNotIn("set", [request.kind for request in settings.waiting], "no save before its check")
        while settings.waiting:
            settings.answer()
        settings.tick(settings_model.SETTLE_SECONDS)
        settings.key("\r")
        self.assertEqual([], [argv for argv in self.writes() if "memory" in argv], "a refused value is never sent")
        settings.key("down")  # moving on drops it, its reason kept under its row
        self.assertIsNone(settings.panel.pending)
        self.assertIn("memory is unknown", settings.text(idle()))
        self.assertEqual({"slots": 4}, self.saved_capacity())

    def test_enter_on_a_typed_value_waits_for_its_check_then_saves(self) -> None:
        settings = self.open()
        settings.select(SAFETY)
        settings.deferred = True
        settings.key("5")
        settings.key("\r")
        self.assertTrue(settings.panel.editing, "not saved before its check says yes")
        self.assertEqual(["check"], [request.kind for request in settings.waiting])
        while settings.waiting:
            settings.answer()
        self.assertFalse(settings.panel.editing)
        self.assertEqual(5.0, self.saved_order()["safety"])

    def test_typing_in_one_section_does_not_stop_the_others_refreshing(self) -> None:
        if self.real:
            self.skipTest("the fake tools sit beside the fake Slotfather")
        lane = FakeTool("lanefather", self.fake.bin)
        settings = self.open()
        settings.select(SAFETY)
        settings.key("\r")
        def reads(fake: Any) -> int:
            return sum(argv == ["settings", "--json"] for argv in cast(list[list[str]], fake.argv()))

        before = (reads(self.fake), reads(lane))
        settings.tick(3)
        self.assertEqual(before[0], reads(self.fake), "not the section being typed in")
        self.assertEqual(before[1] + 1, reads(lane), "the others go on")

    def test_d_twice_resets_only_the_section_it_was_asked_in(self) -> None:
        if self.real:
            self.skipTest("the fake tools sit beside the fake Slotfather")
        junk = FakeTool("junkfather", self.fake.bin, values={"reap.grace": 30})
        self.tool("order", "--safety", "4")
        settings = self.open()
        settings.show(idle(), 90, 70)
        settings.select(SAFETY)
        settings.key("D")
        settings.click(*settings.hit(("junkfather", "reap.grace")))  # another section, by the mouse
        settings.key("D")
        self.assertEqual([], self.writes(junk) + self.writes(), "the first D asked about Slotfather")
        self.assertIn("Press D again to reset every Junkfather setting", settings.text(idle(), height=70))
        settings.key("D")
        self.assertEqual([["settings", "reset", "--all", "--json"]], self.writes(junk))
        self.assertEqual(4.0, self.saved_order()["safety"])


class SectionTests(PanelCase):
    """Lanefather's and Junkfather's sections (fathers#87), against the fakes of tests/fake_tool.py."""

    def setUp(self) -> None:
        super().setUp()
        if self.real:
            self.skipTest("the fake tools sit beside the fake Slotfather")
        self.lane = FakeTool("lanefather", self.fake.bin, environment={"LANEFATHER_PRIORITY_CHECK": "batch"})
        self.junk = FakeTool("junkfather", self.fake.bin)

    def test_three_sections_in_order_each_with_its_status_and_settings(self) -> None:
        settings = self.open()
        text = settings.text(idle(), width=100, height=70)
        self.assert_snapshot("sections", text)
        order = [text.index(title) for title in ("GODFATHER", "SLOTFATHER", "LANEFATHER", "JUNKFATHER")]
        self.assertEqual(sorted(order), order)
        self.assertRegex(text, r"Rerun waits +55m")
        self.assertRegex(text, r"Default TTL +2h")
        self.assertRegex(text, r"check priority +Batch +env LANEFATHER_PRIORITY_CHECK")
        self.assertRegex(text, r"State directory +/fake/state/junkfather +read-only")
        self.assertRegex(text, r"settings +/fake/config/lanefather/settings.json")
        focus = [row.focus for row in settings_model.selectable(settings.panel, settings.context())]
        self.assertNotIn(("lanefather", "slots.priority.check"), focus, "read-only rows are not selectable")
        self.assertNotIn(("junkfather", "env.JUNKFATHER_STATE"), focus)
        self.assertIn(("junkfather", "reap.grace"), focus, "one cursor reaches every section")

    def test_a_lanefather_setting_changes_and_reads_back(self) -> None:
        settings = self.open()
        settings.select(("lanefather", "rerun.wait"))
        settings.key("right")
        text = settings.text(idle(), height=60)
        self.assertRegex(text, r"Rerun waits +1h +pending")
        self.assertIn("rerun.wait would be 3600", text, "the tool's preview under the row")
        self.assertIn("load-model", self.settle(settings))
        self.assertEqual({"rerun.wait": 3600}, self.lane.read()["values"])
        revision = self.writes(self.lane)[0][self.writes(self.lane)[0].index("--revision") + 1]
        self.assertEqual(["settings", "set", "rerun.wait", "3600", "--revision", revision, "--json"],
                         self.writes(self.lane)[0])
        self.assertIn("Saved · Rerun waits 1h", settings.text(idle(), height=60))
        settings.select(("lanefather", "review.start"))
        settings.key("\r", "\r")  # a choice: the next value, then Enter saves it
        self.assertEqual("manual", self.lane.read()["values"]["review.start"])
        self.assertEqual([], self.writes(), "Slotfather is not asked")

    def test_a_conflict_shows_the_current_value_and_saves_nothing(self) -> None:
        settings = self.open()
        settings.select(("junkfather", "reap.grace"))
        self.junk.update(values={"reap.grace": 30})  # another client, not read yet
        settings.key("right")
        self.assertNotIn("load-model", settings.key("\r"))
        self.assertEqual({"reap.grace": 30}, self.junk.read()["values"])
        text = settings.text(idle(), height=70)
        self.assertEqual(1, text.count("✗ Changed by another client: now 30s · nothing was saved"))
        self.assertRegex(text, r"Grace before SIGKILL +30s")

    def test_an_invalid_value_is_refused_under_its_row(self) -> None:
        settings = self.open()
        settings.select(("junkfather", "reap.grace"))
        settings.key("\r", "\x15", "9", "0", "0")
        text = settings.text(idle(), height=70)
        self.assertEqual(1, text.count("must be from 1 to 600 s"))
        self.assertNotIn("load-model", settings.key("\r"))
        self.assertEqual({}, self.junk.read()["values"])

    def test_read_only_junkfather_refuses_with_exit_5_and_says_why(self) -> None:
        settings = self.open()
        settings.select(("junkfather", "own.default_ttl"))
        self.junk.update(writable=False)  # after the read
        self.assertNotIn("load-model", settings.key("right", "\r"))
        text = settings.text(idle(), height=70)
        self.assertEqual(1, text.count("/fake: is not writable"), "said once, the rows read-only after the reread")
        self.assertRegex(text, r"Default TTL +2h +read-only")
        self.assertEqual({}, self.junk.read()["values"])

    def test_d_twice_resets_the_selected_section_and_leaves_the_others(self) -> None:
        self.tool("order", "--safety", "4")
        self.lane.update(values={"rerun.wait": 600})
        self.junk.update(values={"reap.grace": 30})
        settings = self.open()
        settings.select(("junkfather", "reap.grace"))
        settings.key("D")
        self.assertIn("Press D again to reset every Junkfather setting", settings.text(idle(), height=70))
        self.assertIn("load-model", settings.key("D"))
        self.assertEqual({}, self.junk.read()["values"])
        self.assertEqual({"rerun.wait": 600}, self.lane.read()["values"])
        self.assertEqual(4.0, self.saved_order()["safety"])
        self.assertEqual([["settings", "reset", "--all", "--json"]], self.writes(self.junk))
        self.assertEqual([], self.writes(self.lane) + self.writes())

    def test_d_resets_one_row_with_its_revision(self) -> None:
        self.lane.update(values={"rerun.wait": 600})
        settings = self.open()
        settings.select(("lanefather", "rerun.wait"))
        self.assertIn("load-model", settings.key("d"))
        self.assertEqual({}, self.lane.read()["values"])
        self.assertEqual(["settings", "reset", "rerun.wait", "--revision"], self.writes(self.lane)[0][:4])

    def test_the_others_are_read_every_two_seconds(self) -> None:
        settings = self.open()
        reads = lambda: sum(argv == ["settings", "--json"] for argv in self.lane.argv())  # noqa: E731
        before = reads()
        settings.tick(1)
        self.assertEqual(before, reads())
        self.lane.update(values={"review.start": "manual"})
        settings.tick(1)
        self.assertEqual(before + 1, reads())
        self.assertRegex(settings.text(idle(), height=60), r"Review start +Manual")


class ModelTests(unittest.TestCase):
    """settings_model alone: formatting, schema rows, and the transitions the reducer runs."""

    def section(self, *rows: dict[str, Any], tool: str = "lanefather", **reply: Any) -> settings_model.Section:
        return settings_model.Section(tool, reply={"v": 1, "tool": tool, "writable": True, "status": [],
                                                   "sections": [{"id": "x", "settings": list(rows)}], **reply})

    def test_durations(self) -> None:
        for seconds, text in ((3600, "1h"), (1800, "30m"), (5400, "1h30m"), (45, "45s"), (0, "0s"), (0.1, "0.1s"),
                              (90000, "1d1h"), (3601, "1h1s"), (3300, "55m")):
            self.assertEqual(text, span(seconds, compact=True), seconds)

    def test_an_unknown_type_is_shown_read_only(self) -> None:
        found = self.section({"key": "a.colour", "label": "Accent", "type": "colour", "value": "#ff0000",
                              "writable": True, "revision": "r"},
                             {"key": "a.wait", "label": "Wait", "type": "duration", "value": 90, "writable": True,
                              "revision": "r", "step": 30, "min": 0})
        context = settings_model.Context(width=90, height=40)
        panel = settings_model.opened([found], context, 0.0)
        self.assertEqual([INTERVAL, THEME, ("lanefather", "a.wait")],
                         [row.focus for row in settings_model.selectable(panel, context)])
        text = " ".join(part[1] for line in settings_model.lines(panel, context) for part in line.parts)
        self.assertIn("#ff0000", text)
        self.assertIn("read-only here: needs a newer godfather", text)
        self.assertIn("1m30s", text)

    def test_a_pending_change_under_an_older_revision_is_dropped_when_the_rows_change(self) -> None:
        row = {"key": "a.wait", "label": "Wait", "type": "duration", "value": 90, "writable": True,
               "revision": "r1", "step": 30, "min": 0}
        context = settings_model.Context(width=90, height=40)
        panel = settings_model.opened([self.section(row)], context, 0.0)
        panel, context, _ = settings_model.step(panel, context, 1, 0.0)
        self.assertEqual(120.0, panel.pending.value if panel.pending else None)
        newer = self.section(dict(row, value=60, revision="r2")).reply
        assert newer is not None
        panel = settings_model.read_done(panel, context, "lanefather", 0, newer, None, 0.5)
        self.assertIsNone(panel.pending)
        self.assertEqual("Changed by another client: now 1m · your change was not saved",
                         panel.refusal.text if panel.refusal else None)
        panel, _, requests = settings_model.tick(panel, context, 5.0)
        self.assertNotIn("set", [request.kind for request in requests], "nothing is saved")

    def test_a_save_bumps_the_generation_so_an_older_read_is_dropped(self) -> None:
        row = {"key": "a.wait", "label": "Wait", "type": "duration", "value": 90, "writable": True,
               "revision": "r1", "step": 30, "min": 0}
        context = settings_model.Context(width=90, height=40)
        panel = settings_model.opened([self.section(row)], context, 0.0)
        panel, context, _ = settings_model.step(panel, context, 1, 0.0)
        panel = settings_model.replace(panel, checked={("lanefather", "a.wait", "120", "r1"): {"ok": True}})
        panel, context, requests = settings_model.commit_pending(panel, context, 0.1)
        self.assertEqual([("set", "a.wait", "120", "r1")],
                         [(request.kind, request.key, request.value, request.revision) for request in requests])
        self.assertEqual(1, panel.sections[0].generation)
        self.assertRegex(" ".join(part[1] for line in settings_model.lines(panel, context) for part in line.parts),
                         r"2m +saving…")
        panel, more = settings_model.due_reads(panel, 10.0)
        self.assertEqual([], more, "no read while a save of that tool is under way")

    def test_no_thread_or_lock_lives_in_the_state(self) -> None:
        source = Path(settings_model.__file__).read_text() + Path(state_module.__file__).read_text()
        self.assertNotIn("threading", source)
        self.assertNotIn("Lock(", source)
        self.assertTrue(dataclasses.fields(settings_model.Panel))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            settings_model.Panel().focus = ("x", "y")  # type: ignore[misc]

    def test_settings_keys_are_in_the_mode_table(self) -> None:
        from godfather import modes

        for mode in ("settings", "settings-edit", "settings-small"):
            self.assertIn(mode, modes.SETTINGS_TABLE)
        for key in ("left", "right", "d", "D", "\r", "esc", "page_down", "5"):
            self.assertIn(key, modes.SETTINGS)
        source = Path(settings_view.__file__).read_text()
        self.assertNotIn("def key", source, "the view handles no key")


class CompatibilityTests(unittest.TestCase):
    """Provider compatibility matrix (fathers#181), separate from the sibling checkout's exact row-order parity in
    test_contract.py:

    - Sibling checkouts (../slotfather, ../lanefather, ../junkfather): exact schema and row-order parity with
      tests/fake_slotfather.py and tests/fake_tool.py.
    - Any external provider advertising capabilities v1 with `dashboard.settings.versions` containing 1:
      - Older v1 providers that omit later additive rows (such as `order.reservation_timeout`, `host.min_disk_gb`, or
        `own.background_ttl`) render only the rows the provider supplies, from the provider's values, without
        synthesizing omitted rows from copied defaults.
      - Extended v1 providers that add new supported rows (`choice`, `boolean`, `integer`, `number`, `duration`,
        conditional `when` rows) or reorder sections/rows render and edit generically using the provider's keys and
        revisions, while keeping unknown future types read-only.
      - Providers returning malformed required v1 fields (`sections`, `settings`, or a row's `key`, `type`, `value`,
        or `choices`) fail visibly in the section rather than silently dropping rows or crashing.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="godfather-settings-compat-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.record = self.root / "writes.jsonl"
        self.clock = Clock()
        patcher = mock.patch.dict(os.environ, {"GODFATHER_FIXTURES": str(self.root),
                                               "GODFATHER_FIXTURES_RECORD": str(self.record),
                                               "HOME": str(self.root),
                                               "XDG_CONFIG_HOME": str(self.root / "config")}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_provider(self, tool: str, reply: Any, replies: Optional[dict[str, Any]] = None) -> None:
        folder = self.root / tool
        folder.mkdir(parents=True, exist_ok=True)
        capabilities = {"v": 1, "tool": tool, "version": "0.1.5", "dashboard": {
            "v": 1, "tag": tool[:4], "snapshot": {"argv": ["status", "--json", "--full"], "versions": [2], "poll": 1},
            "settings": {"versions": [1]}}}
        table: dict[str, Any] = {"capabilities --json": {"code": 0, "json": capabilities},
                                 "settings --json": {"code": 0, "json": reply}}
        if replies:
            table.update(replies)
        (folder / "replies.json").write_text(json.dumps(table))

    def recorded_argvs(self) -> list[list[str]]:
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text().splitlines()]

    def open_settings(self, width: int = 96, height: int = 48) -> Settings:
        found = Settings(self.clock, width=width, height=height)
        self.addCleanup(found.screen.close)
        return found

    def test_older_provider_omitting_optional_additive_rows_renders_without_copied_defaults(self) -> None:
        import fake_slotfather
        import fake_tool

        slot_doc = fake_slotfather.settings({"cores": 12, "memory_gib": 64, "capacity": None,
                                             "order": {"safety": 4.5, "warn_wait": 900.0, "idle_cpu": 0.25},
                                             "environment": [], "writable": True})
        for section in slot_doc["sections"]:
            section["settings"] = [
                row for row in section["settings"]
                if row["key"] not in {"order.reservation_timeout", "order.memory_share", "order.memory_default_mb"}
            ]
            for row in section["settings"]:
                if row["key"].startswith("order."):
                    row["revision"] = "rev-older-order"
        lane_doc = fake_tool.document({"tool": "lanefather", "values": {"rerun.wait": 1200},
                                       "environment": {}, "writable": True})
        lane_doc["sections"] = [section for section in lane_doc["sections"] if section["id"] != "host"]
        junk_doc = fake_tool.document({"tool": "junkfather", "values": {"own.default_ttl": 5400},
                                       "environment": {}, "writable": True})
        for section in junk_doc["sections"]:
            section["settings"] = [row for row in section["settings"] if row["key"] != "own.background_ttl"]

        updated_slot = copy.deepcopy(slot_doc)
        for section in updated_slot["sections"]:
            for row in section["settings"]:
                if row["key"] == "order.idle_cpu":
                    row["value"] = 0.3
                if row["key"].startswith("order."):
                    row["revision"] = "rev-older-order-2"
        self.write_provider("slotfather", slot_doc, {
            "settings check order.idle_cpu 0.3 --json": {"code": 0, "json": {"ok": True, "error": None, "preview": None}},
            "settings set order.idle_cpu 0.3 --revision rev-older-order --json": {"code": 0, "json": updated_slot},
        })
        self.write_provider("lanefather", lane_doc)
        self.write_provider("junkfather", junk_doc)

        settings = self.open_settings(width=96, height=70)
        text = settings.text(idle(), width=96, height=70)
        for omitted in ("Reservation timeout", "Free disk floor", "Background command TTL"):
            self.assertNotIn(omitted, text, f"omitted additive row {omitted!r} must not be synthesized")
        focus_keys = [row.focus for row in settings_model.selectable(settings.panel, settings.context())]
        self.assertNotIn(("slotfather", "order.reservation_timeout"), focus_keys)
        self.assertNotIn(("lanefather", "host.min_disk_gb"), focus_keys)
        self.assertNotIn(("junkfather", "own.background_ttl"), focus_keys)
        self.assertRegex(text, r"Backfill margin +4\.5×")
        self.assertRegex(text, r"Warn after waiting +15m")
        self.assertRegex(text, r"Idle when below +0\.25 cores")
        self.assertRegex(text, r"Rerun waits +20m")
        self.assertRegex(text, r"Default TTL +1h30m")

        # Moving down from warn_wait steps directly to idle_cpu and edits it using the provider's key and revision.
        settings.select(("slotfather", "order.warn_wait"))
        settings.key("down")
        self.assertEqual(("slotfather", "order.idle_cpu"), settings.focus)
        settings.key("right", "\r")
        self.assertIn(["slotfather", "settings", "set", "order.idle_cpu", "0.3", "--revision", "rev-older-order",
                       "--json"], self.recorded_argvs())

    def test_extended_v1_provider_with_added_reordered_and_unsupported_rows(self) -> None:
        extended: dict[str, Any] = {
            "v": 1, "tool": "slotfather", "writable": True, "reason": None,
            "basis": {"saved": False, "cores": 12, "cpu_slots": 4, "memory_gib": 64.0, "gib_per_slot": None,
                      "memory_slots": 16, "slots": 4, "mode": "automatic"},
            "status": [{"label": "queue", "value": "/compat/state"}],
            "sections": [
                {"id": "order", "title": "Queue order", "settings": [
                    {"key": "order.lease_grace", "label": "Lease grace window", "type": "duration",
                     "value": 45, "default": 30, "min": 0, "max": 300, "step": 45, "unit": "s",
                     "zero": "immediate", "writable": True, "source": "saved", "revision": "rev-ext-1",
                     "description": "Extra grace before an idle lease expires.",
                     "warning": "Applies to future grants only."},
                    {"key": "order.strict_fairness", "label": "Strict fairness", "type": "boolean",
                     "value": True, "default": False, "writable": True, "source": "saved", "revision": "rev-ext-1",
                     "description": "Enforce strict class fairness across projects."},
                    {"key": "order.fairness_burst", "label": "Fairness burst", "type": "integer",
                     "value": 2, "default": 1, "min": 1, "max": 8, "step": 1, "unit": "jobs",
                     "when": {"order.strict_fairness": [True]}, "writable": True, "source": "saved",
                     "revision": "rev-ext-1", "description": "Consecutive same-project grants allowed."},
                    {"key": "order.schedule_curve", "label": "Schedule curve", "type": "histogram",
                     "value": "p90", "default": "p90", "writable": True, "source": "default",
                     "revision": "rev-ext-1", "description": "Future curve type."},
                    {"key": "order.order", "label": "Queue order", "type": "choice",
                     "value": "window", "default": "backfill",
                     "choices": [{"value": "backfill", "label": "Backfill"}, {"value": "window", "label": "Arrival"}],
                     "writable": True, "source": "saved", "revision": "rev-ext-1",
                     "description": "Queue admission discipline."},
                ]},
                {"id": "capacity", "title": "Capacity", "settings": [
                    {"key": "capacity.mode", "label": "Capacity", "type": "choice",
                     "value": "automatic", "default": "automatic",
                     "choices": [{"value": "automatic", "label": "Automatic"}, {"value": "fixed", "label": "Fixed"}],
                     "writable": True, "source": "default", "revision": "rev-cap-1",
                     "description": "Shared slot budget."},
                ]},
            ],
        }
        after_grace = copy.deepcopy(extended)
        after_grace["sections"][0]["settings"][0]["value"] = 90
        after_grace["sections"][0]["settings"][0]["revision"] = "rev-ext-2"
        self.write_provider("slotfather", extended, {
            "settings check order.lease_grace 0 --json": {
                "code": 0, "json": {"ok": True, "error": None, "preview": "grace disabled"}},
            "settings check order.lease_grace 90 --json": {
                "code": 0, "json": {"ok": True, "error": None, "preview": "lease_grace would be 90s"}},
            "settings set order.lease_grace 90 --revision rev-ext-1 --json": {"code": 0, "json": after_grace},
            "settings reset order.lease_grace --revision rev-ext-2 --json": {"code": 0, "json": extended},
        })

        settings = self.open_settings(width=96, height=48)
        focus_keys = [row.focus for row in settings_model.selectable(settings.panel, settings.context())]
        self.assertEqual([
            INTERVAL, THEME,
            ("slotfather", "order.lease_grace"),
            ("slotfather", "order.strict_fairness"),
            ("slotfather", "order.fairness_burst"),
            ("slotfather", "order.order"),
            ("slotfather", "capacity.mode"),
        ], focus_keys, "provider row order and additive rows are preserved; unsupported type is excluded")
        self.assertEqual(("slotfather", "order.lease_grace"), settings.focus, "first provider row gets initial focus")

        text = settings.text(idle(), width=96, height=48)
        self.assertLess(text.index("Lease grace window"), text.index("Capacity"), "reordered sections render in order")
        self.assertRegex(text, r"Lease grace window +45s")
        self.assertIn("Applies to future grants only.", text)
        self.assertIn("Extra grace before an idle lease expires.", text)
        self.assertRegex(text, r"Strict fairness +On")
        self.assertRegex(text, r"Fairness burst +2 jobs")
        self.assertRegex(text, r"Schedule curve +p90 +read-only here: needs a newer godfather")
        self.assertRegex(text, r"Queue order +Arrival")

        # Stepping the additive duration row to 0 uses the provider's `zero` word; stepping up and saving uses its key.
        settings.key("left")
        self.assertRegex(settings.text(idle(), width=96, height=48), r"Lease grace window +immediate +pending")
        settings.key("esc", "right")
        self.assertIn("lease_grace would be 90s", settings.text(idle(), width=96, height=48))
        settings.key("\r")
        self.assertRegex(settings.text(idle(), width=96, height=48), r"Lease grace window +1m30s")
        settings.key("d")
        self.assertIn(["slotfather", "settings", "set", "order.lease_grace", "90", "--revision", "rev-ext-1", "--json"],
                      self.recorded_argvs())
        self.assertIn(["slotfather", "settings", "reset", "order.lease_grace", "--revision", "rev-ext-2", "--json"],
                      self.recorded_argvs())

        # Toggling the additive boolean row hides its conditional `when` follower.
        settings.select(("slotfather", "order.strict_fairness"))
        settings.key("right")
        after_toggle = [row.focus for row in settings_model.selectable(settings.panel, settings.context())]
        self.assertNotIn(("slotfather", "order.fairness_burst"), after_toggle)

    def test_malformed_required_fields_fail_visibly(self) -> None:
        from godfather.adapters.settings import SettingsClient, SettingsError

        valid_row = {"key": "order.safety", "label": "Backfill margin", "type": "number", "value": 3.0,
                     "writable": True, "revision": "r1"}
        cases: list[tuple[str, dict[str, Any], str]] = [
            ("unknown version", {"v": 2, "sections": []}, "settings version 2; needs a newer godfather"),
            ("missing sections", {"v": 1, "tool": "slotfather"}, "sections is missing"),
            ("malformed section", {"v": 1, "sections": [{"id": "order"}]}, "malformed section"),
            ("missing key", {"v": 1, "sections": [{"id": "order", "settings": [{"type": "number", "value": 1}]}]},
             "has no key"),
            ("missing type", {"v": 1, "sections": [{"id": "order", "settings": [{"key": "order.safety", "value": 1}]}]},
             "order.safety has no type"),
            ("missing value",
             {"v": 1, "sections": [{"id": "order", "settings": [{"key": "order.safety", "type": "number"}]}]},
             "order.safety has no value"),
            ("choice without choices",
             {"v": 1, "sections": [{"id": "order", "settings": [
                 {"key": "order.order", "type": "choice", "value": "backfill", "choices": []}]}]},
             "order.order has malformed choices"),
            ("non-numeric duration value",
             {"v": 1, "sections": [{"id": "order", "settings": [
                 {"key": "order.warn_wait", "type": "duration", "value": "thirty"}]}]},
             "order.warn_wait has a non-numeric value"),
            ("non-boolean boolean value",
             {"v": 1, "sections": [{"id": "order", "settings": [
                 {"key": "order.strict", "type": "boolean", "value": "yes"}]}]},
             "order.strict has a non-boolean value"),
            ("malformed status",
             {"v": 1, "status": [{"label": "queue"}], "sections": [{"id": "order", "settings": [valid_row]}]},
             "malformed status"),
        ]
        for name, document, expected in cases:
            with self.subTest(name=name):
                self.write_provider("slotfather", document)
                with self.assertRaisesRegex(SettingsError, expected):
                    SettingsClient(tool="slotfather").read()
                settings = self.open_settings()
                text = settings.text(idle(), width=96, height=40)
                self.assertIn(expected, text)
                self.assertEqual([INTERVAL, THEME],
                                 [row.focus for row in settings_model.selectable(settings.panel, settings.context())])


if __name__ == "__main__":
    unittest.main()
