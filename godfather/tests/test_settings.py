"""The Settings panel (#7), moved from Slotfather: rendered as text and driven by keys and the mouse, against a fake
`slotfather settings` (tests/fake_slotfather.py) that keeps Slotfather's capacity rules and messages.

Snapshots live in tests/snapshots/ and are Slotfather's own, unchanged; `SNAPSHOT_UPDATE=1` rewrites them after a
deliberate layout change."""
from __future__ import annotations

import os
import re
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from helpers import FakeSlotfather

from godfather import tui
from godfather.views import settings as settings_view

SNAPSHOTS = Path(__file__).resolve().parent / "snapshots"
UPDATE = bool(os.environ.get("SNAPSHOT_UPDATE"))
LOCATION = "~/.local/state/slotfather"


def idle(slots: int = 4) -> dict[str, Any]:
    return {"slots": slots, "free": slots, "used": 0, "waiters": [], "holders": []}


def busy(slots: int, held: int, waiting: int) -> dict[str, Any]:
    return {"slots": slots, "free": max(0, slots - held), "used": held, "waiters": [{}] * waiting, "holders": []}


class PanelCase(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeSlotfather()
        self.addCleanup(self.fake.close)
        patcher = mock.patch.dict(os.environ, self.fake.environ(), clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.settings = settings_view
        self.view = tui
        location = mock.patch.object(self.settings.Settings, "location", return_value=LOCATION)
        location.start()
        self.addCleanup(location.stop)

    def tool(self, *args: str) -> Any:
        """What `slotfather slots N`, `slots memory G` and `order ...` saved, written into the fake's state."""
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

    def writes(self) -> list[list[str]]:
        """The settings writes Slotfather was asked for."""
        return [argv for argv in self.fake.argv() if argv[:2] in (["settings", "set"], ["settings", "reset"])]

    def dialog(self, interval: float = 1.0) -> Any:
        return self.settings.Settings(interval)

    def panel(self, dialog: Any, model: dict[str, Any] | None, width: int = 90, height: int = 40) -> str:
        """The panel's own rows of the rendered screen, as text."""
        canvas = self.view.Canvas(width, height)
        dialog.draw(canvas, model)
        x, y, w, h = dialog.bounds
        return "\n".join(line[x:x + w].rstrip() for line in canvas.render(False).split("\n")[y:y + h])

    def assert_snapshot(self, name: str, text: str) -> None:
        path = SNAPSHOTS / f"settings-{name}.txt"
        if UPDATE:
            path.parent.mkdir(exist_ok=True)
            path.write_text(text + "\n")
        self.assertEqual(path.read_text(), text + "\n", f"{path.name} differs; SNAPSHOT_UPDATE=1 rewrites it")

    def saved_capacity(self) -> dict[str, Any] | None:
        return self.fake.read()["capacity"]

    def saved_order(self) -> dict[str, Any]:
        return self.fake.read()["order"]


class RenderTests(PanelCase):
    def test_the_list_keeps_a_blank_row_over_and_under_it_also_scrolled(self) -> None:
        dialog = self.dialog()
        for presses in (0, 30):  # the top, then far down the list
            for _ in range(presses):
                dialog.key("down")
            canvas = self.view.Canvas(90, 20)
            dialog.draw(canvas, idle())
            x, y, width, height = dialog.bounds
            rows = canvas.render(False).split("\n")
            first, last = y + 2, y + 1 + dialog.visible
            self.assertFalse(rows[y + 1][x + 1:x + width - 1].strip(), "a blank row under the top border")
            self.assertFalse(rows[last + 1][x + 1:x + width - 1].strip(), "a blank row under the list")
            thumb = [row for row in range(y, y + height) if rows[row][x + width - 2] in "┃╻╹"]
            self.assertTrue(thumb, "the list overflows at this height")
            self.assertGreaterEqual(thumb[0], first)
            self.assertLessEqual(thumb[-1], last)
            self.assertEqual({"dim"}, {canvas.cells[row][x + width - 2][1] for row in thumb}, "muted at rest")
            lit = self.view.Canvas(90, 20)
            lit.glow = {"settings"}
            dialog.draw(lit, idle())
            self.assertEqual({"text"}, {lit.cells[row][x + width - 2][1] for row in thumb}, "the foreground scrolling")

    def test_idle(self) -> None:
        self.assert_snapshot("idle", self.panel(self.dialog(), idle()))

    def test_busy_while_a_shrink_drains(self) -> None:
        self.assertEqual(0, self.tool("slots", "2").returncode)
        dialog = self.dialog()
        dialog.focus = "amount"
        self.assert_snapshot("draining", self.panel(dialog, busy(2, 4, 3)))

    def test_editing_a_field_previews_what_a_shrink_drains(self) -> None:
        self.assertEqual(0, self.tool("slots", "4").returncode)
        dialog = self.dialog()
        self.panel(dialog, busy(4, 4, 1))
        for key in ("down", "\r", "1"):
            dialog.key(key)
        self.assert_snapshot("editing", self.panel(dialog, busy(4, 4, 1)))
        self.assertEqual({"slots": 4}, self.saved_capacity(), "typing saves nothing")

    def test_invalid_input_is_shown_inline_and_never_saved(self) -> None:
        self.assertEqual(0, self.tool("slots", "4").returncode)
        dialog = self.dialog()
        for key in ("down", "\r", "0"):
            dialog.key(key)
        self.assertIn("✗ Enter a whole number of slots, at least 1.", self.panel(dialog, idle()))
        self.assertEqual("handled", dialog.key("\r"))
        self.assertTrue(dialog.editing, "the refused value stays in the field")
        self.assert_snapshot("invalid", self.panel(dialog, idle()))
        self.assertEqual({"slots": 4}, self.saved_capacity())

    def test_status_is_read_only_and_one_footer_holds_every_key_hint(self) -> None:
        dialog = self.dialog()
        text = self.panel(dialog, busy(4, 2, 1))
        rows = text.split("\n")
        heading = dialog.bounds[1] + next(n for n, line in enumerate(rows) if "─" * 20 in line and n)
        self.assertTrue(dialog.hits and all(key and row > heading for key, _, row, _, _ in dialog.hits),
                        "only the settings below the status are selectable")
        self.assertEqual(1, text.count("esc close"))
        for heading in ("SHARED CAPACITY", "QUEUE POLICY", "THIS DASHBOARD"):
            self.assertNotIn(heading, text, "one flat list")
        for button in ("[Apply]", "[Defaults]", "[Reload]", "[Cancel]", "Draft"):
            self.assertNotIn(button, text)
        self.assertIn("esc close", rows[-2], "the hint line is the panel's last row")
        hints = rows[-2].strip("│ ")
        self.assertLessEqual(abs(rows[-2].index(hints) + len(hints) / 2 - len(rows[-2]) / 2), 2, "centred")

    def test_status_labels_are_dim_and_the_selected_field_alone_is_highlighted(self) -> None:
        dialog = self.dialog()
        canvas = self.view.Canvas(90, 40)
        dialog.draw(canvas, idle())
        rows = ["".join(cell[0] for cell in line) for line in canvas.cells]
        queue = next(n for n, line in enumerate(rows) if "queue  " in line)
        self.assertEqual("dim", canvas.cells[queue][rows[queue].index("queue")][1])
        self.assertFalse(any(cell[2] for cell in canvas.cells[queue]), "status rows have no selection")
        selected = [n for n, line in enumerate(canvas.cells) if any(cell[2] for cell in line)]
        self.assertEqual(1, len(selected))
        self.assertIn("▶ Capacity ", rows[selected[0]])
        value = rows[selected[0]].index("Automatic")
        self.assertEqual("accent", canvas.cells[selected[0]][value][1], "editable values stand out from status")

    def test_the_selected_field_has_a_one_line_description(self) -> None:
        dialog = self.dialog()
        for key, line in (("mode", "Automatic: the lower of the CPU and memory budgets"),
                          ("safety", "Multiplier on expected runtimes"), ("interval", "this dashboard only"),
                          ("theme", "this dashboard only"), ("idle_cpu", "💤")):
            dialog.focus = key
            self.assertIn(line, self.panel(dialog, idle()))

    def test_the_environment_tightened_order_shows_beside_the_saved_one(self) -> None:
        with mock.patch.dict(os.environ, {"SLOTFATHER_QUEUE": "window"}):
            text = self.panel(self.dialog(), idle(), height=44)
        self.assertRegex(text, r"Queue order +Backfill +effective here: Arrival")

    def test_a_short_terminal_scrolls_the_panel_and_an_outside_click_closes_it(self) -> None:
        dialog = self.dialog()
        text = self.panel(dialog, idle(), width=40, height=18)
        self.assertIn("SETTINGS", text)
        self.assertRegex(text, r"[┃╻╹]│", "a scrollbar, no row counts")
        self.assertNotRegex(text, r"↓ \d+ more|↑ \d+ above")
        self.assertEqual("close", dialog.mouse(("mouse", 0, 0, 0, True)), "a click outside closes the panel")
        self.assertEqual("handled", dialog.mouse(("mouse", 65, 0, 0, True)), "the wheel outside does nothing")
        self.assertEqual("mode", dialog.focus)
        for _ in range(12):
            dialog.mouse(("mouse", 65, 10, 5, True))
        text = self.panel(dialog, idle(), width=40, height=18)
        self.assertIn("Refresh every", text)
        self.assertIn("esc close", text.split("\n")[-2], "a narrow footer keeps the way out")
        self.assertEqual("close", dialog.key("esc"))
        self.assertEqual([], self.writes(), "viewing settings saves nothing")


class Clock:
    """A settable monotonic clock for the panel's settle, flash and reset-all timing."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class KeyTests(PanelCase):
    def setUp(self) -> None:
        super().setUp()
        self.clock = Clock()

    def dialog(self, interval: float = 1.0) -> Any:
        return self.settings.Settings(interval, clock=self.clock)

    def settle(self, dialog: Any) -> bool:
        self.clock.now += self.settings.SETTLE_SECONDS
        return dialog.tick()

    def test_an_arrow_change_shows_at_once_and_saves_after_the_keys_settle(self) -> None:
        dialog = self.dialog()
        self.assertEqual("handled", dialog.key("right"))
        self.assertIsNone(self.saved_capacity(), "nothing is saved while the keys may still move")
        text = self.panel(dialog, idle())
        self.assertRegex(text, r"Capacity +‹ Fixed › +pending")
        self.assertIn("→ 4 slots", text)
        self.assertIn("… Capacity Fixed · saves in 1 s · Enter now · Esc cancels", text)
        self.clock.now += 0.5
        self.assertFalse(dialog.tick())
        self.assertTrue(self.settle(dialog))
        self.assertEqual({"slots": 4}, self.saved_capacity(), "fixed keeps the current count")
        flash = [line for line in self.panel(dialog, idle()).split("\n") if "✓" in line]
        self.assertEqual(1, len(flash))
        self.assertRegex(flash[0], r"│ ✓ Saved · capacity 4 slots +│$", "one short line")

    def test_stepping_through_memory_never_saves_it_or_grants_by_it(self) -> None:
        dialog = self.dialog()
        for key in ("right", "right", "right"):  # Fixed, Memory, CPU + memory
            self.clock.now += 0.4
            dialog.key(key)
            self.assertFalse(dialog.tick(), "each step restarts the settle")
        self.assertTrue(self.settle(dialog))
        self.assertEqual([["settings", "set", "capacity.mode", "lower", "--revision"]],
                         [argv[:5] for argv in self.writes()], "one save, of the value the keys settled on")
        self.assertEqual({"slots": 4, "mode": "lower", "gib_per_slot": 4}, self.saved_capacity())

    def test_enter_saves_a_pending_change_now_and_escape_cancels_one(self) -> None:
        dialog = self.dialog()
        dialog.focus = "safety"
        dialog.key("right")
        self.assertEqual("changed", dialog.key("\r"))
        self.assertEqual(3.5, self.saved_order()["safety"])
        dialog.key("right")
        self.assertEqual("handled", dialog.key("esc"), "the first Esc cancels the pending change")
        self.assertFalse(self.settle(dialog))
        self.assertEqual(3.5, self.saved_order()["safety"])
        self.assertIn("Change cancelled", self.panel(dialog, idle()))
        self.assertEqual("close", dialog.key("esc"))

    def test_one_flat_list_with_labels_that_read_alone_and_units_by_the_values(self) -> None:
        dialog = self.dialog()
        self.assertEqual(["mode", "order", "safety", "fallback", "overrun", "limit", "interactive", "batch",
                          "aging_batch", "aging_normal", "warn_wait", "idle_cpu", "idle_after", "yield_after",
                          "interval", "theme"], dialog.fields())
        text = self.panel(dialog, idle(), height=60)
        for label, value in (("Capacity", "Automatic"), ("Queue order", "Backfill"), ("Backfill margin", "3×"),
                             ("Unknown job runtime", "3600 s"), ("Overrun estimate", "0.5×"),
                             ("Overtaking stops after", "never"), ("Interactive head start", "1800 s"),
                             ("Batch held back", "1800 s"), ("Batch promoted after", "1800 s"),
                             ("Normal promoted after", "3600 s"), ("Warn after waiting", "1800 s"),
                             ("Idle when below", "0.1 cores"),
                             ("Idle for", "300 s"), ("Ask to yield after", "120 s"), ("Refresh every", "1 s"),
                             ("Colour theme", "Auto")):
            self.assertRegex(text, rf"{re.escape(label)} +(‹ )?{re.escape(value)}")
        self.assertNotRegex(text, r"\((s|cores)\)", "no units in the labels")

    def test_moving_on_saves_a_pending_change(self) -> None:
        dialog = self.dialog()
        dialog.focus = "safety"
        dialog.key("right")
        self.assertEqual("changed", dialog.key("down"))
        self.assertEqual(("fallback", 3.5), (dialog.focus, self.saved_order()["safety"]))

    def test_enter_edits_a_number_and_enter_saves_it(self) -> None:
        self.assertEqual(0, self.tool("slots", "4").returncode)
        dialog = self.dialog()
        self.assertEqual("handled", dialog.key("down"))
        self.assertEqual("handled", dialog.key("\r"))
        self.assertTrue(dialog.editing)
        dialog.key("6")
        self.assertIn("[6▏]", self.panel(dialog, idle()))
        self.assertEqual("changed", dialog.key("\r"))
        self.assertFalse(dialog.editing)
        self.assertEqual({"slots": 6}, self.saved_capacity())

    def test_typed_capacity_has_an_upper_bound_and_a_large_grow_says_what_starts(self) -> None:
        self.assertEqual(0, self.tool("slots", "4").returncode)
        dialog = self.dialog()
        self.panel(dialog, {**busy(4, 4, 3), "waiters": [{"count": 2}, {"count": 6}, {"count": 1}]})
        for key in ("down", "\r", "4", "9"):
            dialog.key(key)
        self.assertIn("✗ At most 48 slots (4 per core).", self.panel(dialog, busy(4, 4, 3)))
        self.assertEqual("handled", dialog.key("\r"))
        self.assertEqual({"slots": 4}, self.saved_capacity())
        for key in ("\x15", "1", "2"):
            dialog.key(key)
        model = {**busy(4, 4, 3), "waiters": [{"count": 2}, {"count": 6}, {"count": 1}]}
        self.assertIn("→ 12 slots · will start 2 waiting", self.panel(dialog, model))

    def test_typing_a_digit_on_a_number_starts_editing_with_it(self) -> None:
        dialog = self.dialog()
        dialog.focus = "safety"
        dialog.key("5")
        self.assertEqual((True, "5"), (dialog.editing, dialog.buffer))
        self.assertEqual("changed", dialog.key("\r"))
        self.assertEqual(5.0, self.saved_order()["safety"])

    def test_escape_leaves_edit_mode_without_saving_and_then_closes(self) -> None:
        self.assertEqual(0, self.tool("slots", "4").returncode)
        dialog = self.dialog()
        for key in ("down", "\r", "8"):
            dialog.key(key)
        self.assertEqual("handled", dialog.key("esc"))
        self.assertFalse(dialog.editing)
        self.assertEqual({"slots": 4}, self.saved_capacity())
        self.assertIn("Edit cancelled", self.panel(dialog, idle()))
        self.assertEqual("close", dialog.key("esc"))

    def test_keys_while_editing_stay_in_the_field(self) -> None:
        dialog = self.dialog()
        dialog.focus = "interval"
        dialog.key("\r")
        for key in ("q", "s", "D", "down"):
            self.assertEqual("handled", dialog.key(key))
        self.assertEqual((True, "1"), (dialog.editing, dialog.buffer))
        dialog.key("\x7f")
        self.assertEqual("", dialog.buffer)
        self.assertEqual("handled", dialog.key("\r"), "an empty field is refused")
        self.assertTrue(dialog.editing)

    def test_the_interval_steps_through_a_tenth_and_whole_seconds(self) -> None:
        dialog = self.dialog(0.1)
        dialog.focus = "interval"
        seen: list[float] = []
        for key in ("right", "right", "right", "left", "left", "left"):
            dialog.key(key)
            dialog.key("\r")
            seen.append(dialog.interval)
        self.assertEqual([1.0, 2.0, 3.0, 2.0, 1.0, 0.1], seen)
        self.assertEqual([], self.writes(), "a local setting never goes to Slotfather")

    def test_reset_restores_the_selected_field_alone(self) -> None:
        self.assertEqual(0, self.tool("slots", "6").returncode)
        self.assertEqual(0, self.tool("order", "window", "--safety", "4").returncode)
        dialog = self.dialog()
        dialog.focus = "safety"
        self.assertEqual("changed", dialog.key("d"))
        self.assertEqual(("window", None), (self.saved_order()["order"], self.saved_order().get("safety")),
                         "settings reset order.safety: back to Slotfather's default")
        self.assertRegex(self.panel(dialog, idle()), r"Backfill margin +3×")
        self.assertEqual({"slots": 6}, self.saved_capacity())
        dialog.focus = "amount"
        self.assertEqual("changed", dialog.key("d"))
        self.assertIsNone(self.saved_capacity(), "a capacity field resets to automatic capacity")
        self.assertNotIn("amount", dialog.fields())

    def test_a_field_that_disappears_takes_its_focus_and_keys_with_it(self) -> None:
        self.assertEqual(0, self.tool("slots", "6").returncode)
        dialog = self.dialog()
        dialog.focus = "amount"
        for key in ("d", "5"):  # one batch: Total slots is gone after the reset
            dialog.key(key)
        self.assertEqual("mode", dialog.focus)
        self.assertFalse(dialog.editing)
        self.assertIsNone(self.saved_capacity())
        self.assertEqual((None, "This setting takes no typed value."), dialog.check("amount", "5"))
        self.panel(dialog, idle())

    def test_reset_all_asks_twice_within_the_flash_and_then_restores_every_default(self) -> None:
        self.assertEqual(0, self.tool("slots", "6").returncode)
        self.assertEqual(0, self.tool("order", "window").returncode)
        dialog = self.dialog(5.0)
        self.assertEqual("handled", dialog.key("D"))
        self.assertIn("Press D again", self.panel(dialog, idle()))
        dialog.key("down")
        self.assertEqual("handled", dialog.key("D"), "another key in between withdraws the question")
        self.clock.now += self.settings.FLASH_SECONDS + 0.1
        self.assertEqual("handled", dialog.key("D"), "the question expires with its flash")
        self.assertIsNotNone(self.saved_capacity())
        self.clock.now += 1
        self.assertEqual("changed", dialog.key("D"))
        self.assertEqual((None, {}, 1.0), (self.saved_capacity(), self.saved_order(), dialog.interval))

    def test_the_refresh_interval_stays_local(self) -> None:
        dialog = self.dialog()
        dialog.focus = "interval"
        for key in ("\r", "6", "1"):
            dialog.key(key)
        self.assertIn("Enter seconds from 0.1 to 60", self.panel(dialog, idle()))
        self.assertEqual("handled", dialog.key("\r"))
        for key in ("\x15", "5", "\r"):
            dialog.key(key)
        self.assertEqual(5.0, dialog.interval)
        self.assertEqual([], self.writes(), "a local setting never goes to Slotfather")

    def test_a_click_selects_a_field_a_second_click_edits_and_another_cancels_the_edit(self) -> None:
        dialog = self.dialog()
        self.panel(dialog, idle())
        _, left, top, _, _ = next(hit for hit in dialog.hits if hit[0] == "interval")
        dialog.mouse(("mouse", 0, left + 3, top, True))
        self.assertEqual(("interval", False), (dialog.focus, dialog.editing))
        dialog.mouse(("mouse", 0, left + 3, top, True))
        self.assertTrue(dialog.editing)
        dialog.key("7")
        self.panel(dialog, idle())
        _, left, top, _, _ = next(hit for hit in dialog.hits if hit[0] == "safety")
        dialog.mouse(("mouse", 0, left + 3, top, True))
        self.assertEqual(("safety", False, 1.0), (dialog.focus, dialog.editing, dialog.interval))
        self.assertIn("Edit cancelled · nothing was saved", self.panel(dialog, idle()))

    def test_below_the_minimum_size_only_the_ways_out_work(self) -> None:
        dialog = self.dialog()
        text = self.panel(dialog, idle(), width=28, height=10)
        self.assertIn("Enlarge", text)
        for key in ("right", "d", "D", "D", "\r", "5", "\r"):
            self.assertEqual("handled", dialog.key(key))
        self.assertEqual((False, None), (dialog.editing, dialog.pending))
        self.assertEqual([], self.writes())
        self.assertEqual("quit", dialog.key("q"))
        self.assertEqual("close", dialog.key("esc"))

    def test_changes_from_another_client_show_while_open_and_are_not_overwritten(self) -> None:
        dialog = self.dialog()
        self.assertEqual(0, self.tool("order", "window").returncode)
        self.clock.now += self.settings.READ_EVERY
        self.assertRegex(self.panel(dialog, idle()), r"Queue order +Arrival")
        dialog.focus = "safety"
        dialog.key("right")
        dialog.key("\r")
        self.assertEqual(("window", 3.5), (self.saved_order()["order"], self.saved_order()["safety"]))

    def test_a_capacity_saved_elsewhere_while_typing_is_not_overwritten(self) -> None:
        self.assertEqual(0, self.tool("slots", "4").returncode)
        dialog = self.dialog()
        for key in ("down", "\r", "3"):
            dialog.key(key)
        self.assertEqual(0, self.tool("slots", "6").returncode)
        self.assertEqual("handled", dialog.key("\r"))
        self.assertEqual({"slots": 6}, self.saved_capacity(), "the other save stands")
        self.assertIn("✗ Capacity changed elsewhere: now 6 slots · nothing was saved", self.panel(dialog, idle()))
        self.assertRegex(self.panel(dialog, idle()), r"Slots +6")
        dialog.key("\r")
        dialog.key("3")
        self.assertEqual("changed", dialog.key("\r"), "once it has read the new value it saves")
        self.assertEqual({"slots": 3}, self.saved_capacity())

    def test_two_dashboards_do_not_overwrite_each_others_capacity(self) -> None:
        self.assertEqual(0, self.tool("slots", "4").returncode)
        first, second = self.dialog(), self.dialog()
        for dialog in (first, second):
            dialog.focus = "amount"
        first.key("right")
        self.assertEqual("changed", first.key("\r"))
        second.key("left")
        self.assertEqual("handled", second.key("\r"))
        self.assertEqual({"slots": 5}, self.saved_capacity())
        self.assertIn("Capacity changed elsewhere: now 5 slots", self.panel(second, idle()))

    def test_a_pending_change_is_never_saved_over_a_change_made_elsewhere(self) -> None:
        # The audit's probe: a refresh during the settle took the other client's bytes and mode as its own, and
        # "5 slots" was saved as 5 GiB per slot over the other client's change.
        self.assertEqual(0, self.tool("slots", "4").returncode)
        dialog = self.dialog()
        dialog.focus = "amount"
        self.clock.now += 0.5
        dialog.key("right")  # 5 slots, pending
        self.assertEqual(0, self.tool("slots", "memory", "8").returncode)  # another client
        self.clock.now += self.settings.READ_EVERY
        self.panel(dialog, idle())  # drawing refreshes the panel
        self.assertFalse(self.settle(dialog), "nothing is saved")
        saved = self.saved_capacity()
        assert saved is not None
        self.assertEqual("memory", saved["mode"], "the other client's change stands")
        self.assertEqual(8, saved["gib_per_slot"])
        self.assertIn("changed elsewhere", self.panel(dialog, idle()), "and the panel says so")

    def test_a_refused_save_on_moving_on_stays_visible(self) -> None:
        self.assertEqual(0, self.tool("slots", "4").returncode)
        dialog = self.dialog()
        dialog.focus = "amount"
        dialog.key("right")
        self.assertEqual(0, self.tool("slots", "6").returncode)  # elsewhere, not read yet
        self.assertEqual("handled", dialog.key("down"))
        self.assertEqual({"slots": 6}, self.saved_capacity())
        self.assertIn("changed elsewhere", self.panel(dialog, idle()), "the refusal is not wiped by the move")

    def test_esc_on_a_small_panel_cancels_a_pending_change_visibly(self) -> None:
        self.assertEqual(0, self.tool("slots", "4").returncode)
        dialog = self.dialog()
        dialog.focus = "amount"
        dialog.key("right")
        self.panel(dialog, idle(), width=20, height=8)  # too small: only the ways out work
        self.assertTrue(dialog.small)
        self.assertEqual("handled", dialog.key("esc"), "the first Esc cancels")
        self.assertIsNone(dialog.pending)
        self.assertIn("cancelled", (dialog.flash or ("",))[0])
        self.assertFalse(self.settle(dialog), "and nothing is saved later")
        self.assertEqual({"slots": 4}, self.saved_capacity())
        self.assertEqual("close", dialog.key("esc"))

    def test_a_refused_change_says_why_and_keeps_the_value(self) -> None:
        dialog = self.dialog()
        dialog.key("right")
        dialog.key("\r")  # Fixed, 4 slots
        self.fake.update(memory_gib=None)  # Slotfather cannot tell the machine's memory
        dialog.read_at = -1e9
        dialog.key("right")  # Memory needs the machine's memory
        self.assertIn("memory is unknown", self.panel(dialog, idle()))
        self.assertEqual("handled", dialog.key("\r"))
        self.assertIn("memory is unknown", self.panel(dialog, idle()))
        self.assertEqual({"slots": 4}, self.saved_capacity())

    def test_status_shows_the_capacity_the_queue_read_under_its_lock(self) -> None:
        dialog = self.dialog()
        model = {**idle(9), "basis": {**dialog.basis, "slots": 9, "mode": "fixed", "saved": True}}
        self.assertRegex(self.panel(dialog, model), r"capacity +9 slots · saved, fixed count")


class ContractTests(PanelCase):
    """What the panel asks Slotfather, beyond what the flows above show."""

    def setUp(self) -> None:
        super().setUp()
        self.clock = Clock()

    def test_a_pending_capacity_change_carries_the_revision_it_was_chosen_under(self) -> None:
        dialog = KeyTests.dialog(self)  # type: ignore[arg-type]
        revision = dialog.capacity_bytes
        dialog.key("right")
        dialog.key("\r")
        self.assertEqual([["settings", "set", "capacity.mode", "fixed", "--revision", revision, "--json"]],
                         self.writes())

    def test_an_order_knob_is_merged_without_a_revision(self) -> None:
        dialog = KeyTests.dialog(self)  # type: ignore[arg-type]
        dialog.focus = "safety"
        dialog.key("right")
        dialog.key("\r")
        self.assertEqual([["settings", "set", "order.safety", "3.5", "--json"]], self.writes())

    def test_a_read_only_queue_says_so_and_saves_nothing(self) -> None:
        self.fake.update(writable=False)
        dialog = KeyTests.dialog(self)  # type: ignore[arg-type]
        dialog.focus = "safety"
        dialog.key("right")
        self.assertEqual("handled", dialog.key("\r"))
        self.assertIn("✗ this queue belongs to another user; settings are read-only", self.panel(dialog, idle()))
        self.assertEqual({}, self.saved_order())

    def test_without_a_settings_contract_only_this_dashboards_rows_show(self) -> None:
        dialog = self.settings.Settings(1.0, local_only=True, problem=lambda: "slotfather 0.1.0: no settings")
        self.assertEqual(["interval", "theme"], dialog.fields())
        text = self.panel(dialog, idle())
        self.assertIn("slotfather 0.1.0: no settings", text)
        self.assertRegex(text, r"Refresh every +1 s")
        self.assertNotIn("Capacity", text)
        self.assertEqual([], self.fake.argv(), "Slotfather is never asked")

    def test_a_stale_snapshot_says_why_in_status(self) -> None:
        dialog = self.settings.Settings(1.0, problem=lambda: "slotfather status --json --full took longer than 5 s")
        self.assertRegex(self.panel(dialog, idle()), r"slotfather +slotfather status --json --full took longer")



if __name__ == "__main__":
    unittest.main()
