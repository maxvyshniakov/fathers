"""The dashboard's Settings panel (#7): rendered as text, driven by keys and the mouse, and against a live queue.

Snapshots live in tests/snapshots/; `SNAPSHOT_UPDATE=1` rewrites them after a deliberate layout change."""
from __future__ import annotations

import importlib
import fcntl
import json
import os
import pty
import re
import select
import signal
import struct
import subprocess
import termios
import time
from pathlib import Path
from typing import Any
from unittest import mock

from test_slotfather import SlotCase

SNAPSHOTS = Path(__file__).resolve().parent / "snapshots"
UPDATE = bool(os.environ.get("SNAPSHOT_UPDATE"))
LOCATION = "~/.local/state/slotfather"


def idle(slots: int = 4) -> dict[str, Any]:
    return {"slots": slots, "free": slots, "used": 0, "waiters": [], "holders": []}


def busy(slots: int, held: int, waiting: int) -> dict[str, Any]:
    return {"slots": slots, "free": max(0, slots - held), "used": held, "waiters": [{}] * waiting, "holders": []}


class PanelCase(SlotCase):
    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.dict(os.environ, self.env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.settings = importlib.import_module("slotfather.dashboard_settings")
        self.view = importlib.import_module("slotfather.dashboard")
        location = mock.patch.object(self.settings.Settings, "location", return_value=LOCATION)
        location.start()
        self.addCleanup(location.stop)

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
        path = self.state / "capacity.json"
        return json.loads(path.read_text()) if path.exists() else None

    def saved_order(self) -> dict[str, Any]:
        path = self.state / "order.json"
        return json.loads(path.read_text()) if path.exists() else {}


class RenderTests(PanelCase):
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
        heading = dialog.bounds[1] + next(n for n, line in enumerate(rows) if "SHARED CAPACITY" in line)
        self.assertTrue(dialog.hits and all(key and row > heading for key, _, row, _, _ in dialog.hits),
                        "only the settings below the status are selectable")
        self.assertEqual(1, text.count("Esc close"))
        for button in ("[Apply]", "[Defaults]", "[Reload]", "[Cancel]", "Draft"):
            self.assertNotIn(button, text)
        self.assertIn("Esc close", rows[-2], "the hint line is the panel's last row")

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
        self.assertIn("▶ Capacity mode", rows[selected[0]])
        value = rows[selected[0]].index("Automatic")
        self.assertEqual("accent", canvas.cells[selected[0]][value][1], "editable values stand out from status")

    def test_the_selected_field_has_a_one_line_description(self) -> None:
        dialog = self.dialog()
        for key, line in (("mode", "Automatic: the lower of the CPU and memory budgets"),
                          ("safety", "Multiplier on expected runtimes"), ("interval", "Not shared")):
            dialog.focus = key
            self.assertIn(line, self.panel(dialog, idle()))

    def test_the_environment_tightened_order_shows_beside_the_saved_one(self) -> None:
        with mock.patch.dict(os.environ, {"SLOTFATHER_QUEUE": "window"}):
            text = self.panel(self.dialog(), idle(), height=44)
        self.assertRegex(text, r"Queue order +Backfill +effective here: Window")

    def test_a_short_terminal_scrolls_the_panel_and_ignores_outside_clicks(self) -> None:
        dialog = self.dialog()
        text = self.panel(dialog, idle(), width=40, height=18)
        self.assertIn("SETTINGS", text)
        self.assertRegex(text, r"↓ \d+ more")
        dialog.mouse(("mouse", 0, 0, 0, True))
        self.assertEqual("mode", dialog.focus)
        for _ in range(12):
            dialog.mouse(("mouse", 65, 10, 5, True))
        text = self.panel(dialog, idle(), width=40, height=18)
        self.assertRegex(text, r"↑ \d+ above")
        self.assertIn("Refresh inter… 1", text)
        self.assertIn("Esc close", text.split("\n")[-2], "a narrow footer keeps the way out")
        self.assertNotRegex(text, r"↓ \d+ more")
        self.assertEqual("close", dialog.key("esc"))
        self.assertFalse(self.state.exists(), "viewing settings creates no queue")


class KeyTests(PanelCase):
    def test_changing_a_choice_saves_it_at_once_and_flashes_what_it_did(self) -> None:
        dialog = self.dialog()
        self.assertEqual("changed", dialog.key("right"))
        self.assertEqual({"slots": 4}, self.saved_capacity(), "fixed keeps the current count")
        flash = [line for line in self.panel(dialog, idle()).split("\n") if "✓" in line]
        self.assertEqual(1, len(flash))
        self.assertRegex(flash[0], r"│ ✓ Saved · capacity 4 slots +│$", "one short line")
        self.assertIn("4 slots (saved)", self.tool("slots").stdout)

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

    def test_arrows_step_a_number_and_save_each_step(self) -> None:
        dialog = self.dialog()
        dialog.focus = "safety"
        self.assertEqual("changed", dialog.key("right"))
        self.assertEqual(3.5, self.saved_order()["safety"])
        dialog.focus = "interval"
        self.assertEqual("changed", dialog.key("right"))
        self.assertEqual(2.0, dialog.interval)

    def test_reset_restores_the_selected_field_alone(self) -> None:
        self.assertEqual(0, self.tool("slots", "6").returncode)
        self.assertEqual(0, self.tool("order", "window", "--safety", "4").returncode)
        dialog = self.dialog()
        dialog.focus = "safety"
        self.assertEqual("changed", dialog.key("d"))
        self.assertEqual(("window", 3.0), (self.saved_order()["order"], self.saved_order()["safety"]))
        self.assertEqual({"slots": 6}, self.saved_capacity())
        dialog.focus = "amount"
        self.assertEqual("changed", dialog.key("d"))
        self.assertIsNone(self.saved_capacity(), "a capacity field resets to automatic capacity")
        self.assertNotIn("amount", dialog.fields())

    def test_reset_all_asks_twice_and_then_restores_every_default(self) -> None:
        self.assertEqual(0, self.tool("slots", "6").returncode)
        self.assertEqual(0, self.tool("order", "window").returncode)
        dialog = self.dialog(5.0)
        self.assertEqual("handled", dialog.key("D"))
        self.assertIn("Press D again", self.panel(dialog, idle()))
        self.assertIsNotNone(self.saved_capacity())
        dialog.key("down")
        self.assertEqual("handled", dialog.key("D"), "another key in between withdraws the question")
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
        self.assertFalse(self.state.exists(), "no shared file for a local setting")

    def test_a_click_selects_a_field_and_a_second_click_edits_it(self) -> None:
        dialog = self.dialog()
        self.panel(dialog, idle())
        _, left, top, _, _ = next(hit for hit in dialog.hits if hit[0] == "interval")
        dialog.mouse(("mouse", 0, left + 3, top, True))
        self.assertEqual(("interval", False), (dialog.focus, dialog.editing))
        dialog.mouse(("mouse", 0, left + 3, top, True))
        self.assertTrue(dialog.editing)

    def test_changes_from_another_client_show_while_open_and_are_not_overwritten(self) -> None:
        dialog = self.dialog()
        self.assertEqual(0, self.tool("order", "window").returncode)
        dialog.read_at = -1e9  # its next draw reads the shared settings
        self.assertRegex(self.panel(dialog, idle()), r"Queue order +Window")
        dialog.focus = "safety"
        dialog.key("right")
        self.assertEqual(("window", 3.5), (self.saved_order()["order"], self.saved_order()["safety"]))

    def test_a_refused_change_says_why_and_keeps_the_value(self) -> None:
        dialog = self.dialog()
        dialog.key("right")  # Fixed, 4 slots
        with mock.patch.dict(os.environ, {"SLOTFATHER_MEMORY_GIB": ""}), \
                mock.patch.object(self.settings.platform, "memory_gib", return_value=None):
            self.assertEqual("handled", dialog.key("right"), "Memory needs the machine's memory")
        self.assertIn("memory is unknown", self.panel(dialog, idle()))
        self.assertEqual({"slots": 4}, self.saved_capacity())


class LiveCapacityTests(PanelCase):
    """A capacity the panel saves applies at the next grant without a restart, whatever the queue holds."""

    def test_growing_the_capacity_under_load_grants_the_waiter(self) -> None:
        self.assertEqual(0, self.tool("slots", "4").returncode)
        holder = self.request("busy", 4)
        self.started("busy")
        waiter = self.request("waiter", 2)
        self.wait_for(lambda: "waiting: 2 slot(s) waiter" in self.status(), "the waiter to queue")
        dialog = self.dialog()
        dialog.focus = "amount"
        self.assertEqual("changed", dialog.key("right"))
        self.assertEqual("changed", dialog.key("right"))
        self.assertEqual({"slots": 6}, self.saved_capacity())
        self.started("waiter")
        model = self.view.dashboard_model()
        self.assertEqual((6, 6, 0, 0), (model["slots"], model["used"], model["free"], model["draining"]))
        for label in ("busy", "waiter"):
            self.release(label)
        for process in (holder, waiter):
            self.finish(process)

    def test_shrinking_below_the_held_slots_drains_and_the_panel_says_so(self) -> None:
        self.assertEqual(0, self.tool("slots", "4").returncode)
        holders = {label: self.request(label, 2) for label in ("a", "b")}
        for label in holders:
            self.started(label)
        waiter = self.request("w", 1)
        self.wait_for(lambda: "waiting: 1 slot(s) w" in self.status(), "the waiter to queue")
        dialog = self.dialog()
        before = self.view.dashboard_model()
        dialog.focus = "amount"
        for key in ("\r", "2"):
            dialog.key(key)
        self.assertIn("→ 2 slots · will drain 2: running work keeps its slots", self.panel(dialog, before))
        self.assertEqual("changed", dialog.key("\r"))
        self.assertEqual({"slots": 2}, self.saved_capacity())
        model = self.view.dashboard_model()
        self.assertEqual((2, 4, 2), (model["slots"], model["used"], model["draining"]))
        text = self.panel(dialog, model)
        self.assertIn("draining     2 slots · nothing starts until at most 2 are held", text)
        self.assertIn("draining 2: running work keeps its slots", text)
        self.assertEqual(1, text.count("running work keeps its slots"), "the drain is said in STATUS and at the field")
        self.assertRegex(text, r"│ ✓ Saved · capacity 2 slots +│\n")
        canvas = self.view.Canvas(140, 40)
        self.view.dashboard_draw(canvas, model)
        self.assertIn("2 slots (manual) · draining 2", canvas.render(False))
        self.release("a")
        self.finish(holders["a"])
        self.assertIn("waiting: 1 slot(s) w", self.status(), "held 2 of 2: nothing starts yet")
        self.release("b")
        self.finish(holders["b"])
        self.started("w")
        self.release("w")
        self.finish(waiter)


class TerminalTests(SlotCase):
    def test_s_opens_settings_a_change_applies_at_once_and_escape_closes(self) -> None:
        master, slave = pty.openpty()
        process: subprocess.Popen[bytes] | None = None
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 100, 0, 0))
            command = Path(__file__).resolve().parents[1] / "slotfather"
            process = subprocess.Popen([str(command), "dashboard", "--interval", "60"],
                                       stdin=slave, stdout=slave, stderr=slave, env=self.env, start_new_session=True)
            os.close(slave)
            slave = -1
            screen = bytearray()

            def receive(fragment: str) -> None:
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    if select.select([master], [], [], 0.1)[0]:
                        try:
                            screen.extend(os.read(master, 65536))
                        except OSError:
                            break
                    plain = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", screen.decode("utf-8", "replace"))
                    if fragment in plain:
                        screen.clear()
                        return
                self.fail(f"terminal did not show {fragment!r}: {screen.decode('utf-8', 'replace')!r}")

            receive("REQUESTS")
            os.write(master, b"s")
            receive("STATUS")
            os.write(master, b"\x1b[C")  # Capacity mode: Automatic -> Fixed, saved at once
            receive("Saved")
            self.assertIn("4 slots (saved)", self.tool("slots").stdout)
            os.write(master, b"\x1b[B\r6\r")  # Total slots: type 6, Enter saves
            receive("capacity 6 slots")
            self.assertIn("6 slots (saved)", self.tool("slots").stdout)
            os.write(master, b"\r9\x1b")  # a second edit, cancelled
            receive("Edit cancelled")
            self.assertIn("6 slots (saved)", self.tool("slots").stdout)
            os.write(master, b"\x1b")
            receive("REQUESTS")
            os.write(master, b"q")
            deadline = time.monotonic() + 5
            while process.poll() is None and time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    try:
                        os.read(master, 65536)
                    except OSError:
                        break
            self.assertEqual(0, process.wait(timeout=1))
        finally:
            if process is not None and process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=3)
            os.close(master)
            if slave >= 0:
                os.close(slave)
