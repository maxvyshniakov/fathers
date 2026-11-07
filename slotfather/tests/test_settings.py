"""Settings changes observed through the dialog controller and the public CLI."""
from __future__ import annotations

import importlib
import fcntl
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
from unittest import mock

from test_slotfather import SlotCase


class SettingsTests(SlotCase):
    def test_automatic_preview_matches_reset_after_leaving_a_saved_cgroup(self) -> None:
        root = Path(__file__).resolve().parents[1]
        self.env.pop("SLOTFATHER_MEMORY_GIB")
        environment = {name: value for name, value in self.env.items() if name != "SLOTFATHER_MEMORY_GIB"}
        environment.update(SLOTFATHER_CORES="16", SLOTFATHER_PROC=str(root / "tests/fixtures/proc/calm"),
                           SLOTFATHER_CGROUP_ROOT=str(root / "tests/fixtures/cgroup/calm"))
        saved = self.tool("slots", "memory", "5", **{**environment, "SLOTFATHER_CGROUP": "/ci.slice/ci-runners.slice"})
        self.assertEqual(0, saved.returncode, saved.stderr)
        with mock.patch.dict(os.environ, environment, clear=True):
            settings = importlib.import_module("slotfather.dashboard_settings")
            dialog = settings.Settings(1)
            dialog.values["mode"] = "automatic"
            self.assertEqual("Draft capacity: 3 slots", dialog.preview())
            dialog.apply()
        self.assertIn("3 slots (automatic", self.tool("slots", **environment).stdout)

    def test_escape_while_editing_discards_the_entire_dialog(self) -> None:
        with mock.patch.dict(os.environ, self.env, clear=True):
            settings = importlib.import_module("slotfather.dashboard_settings")
            dialog = settings.Settings(1)
            dialog.key("right")
            dialog.key("down")
            dialog.key("\r")
            dialog.key("8")
            self.assertEqual("close", dialog.key("esc"))
        self.assertFalse(self.state.exists())

    def test_advanced_shows_saved_and_environment_tightened_order(self) -> None:
        with mock.patch.dict(os.environ, {**self.env, "SLOTFATHER_QUEUE": "window"}, clear=True):
            settings = importlib.import_module("slotfather.dashboard_settings")
            view = importlib.import_module("slotfather.dashboard")
            dialog = settings.Settings(1)
            dialog.advanced = True
            canvas = view.Canvas(90, 40)
            dialog.draw(canvas, None)
            self.assertIn("Queue order:  Backfill · effective Window", canvas.render(False))

    def test_defaults_remain_a_draft_and_reset_the_shared_policy_on_apply(self) -> None:
        self.assertEqual(0, self.tool("slots", "6").returncode)
        self.assertEqual(0, self.tool("order", "window").returncode)
        with mock.patch.dict(os.environ, self.env, clear=True):
            settings = importlib.import_module("slotfather.dashboard_settings")
            dialog = settings.Settings(5.0)
            dialog.focus = "defaults"
            dialog.activate()
            self.assertTrue((self.state / "capacity.json").exists())
            self.assertTrue((self.state / "order.json").exists())
            self.assertEqual(1, dialog.apply())
            self.assertFalse((self.state / "capacity.json").exists())
            self.assertFalse((self.state / "order.json").exists())

    def test_failed_policy_write_restores_the_previous_capacity(self) -> None:
        self.assertEqual(0, self.tool("slots", "6").returncode)
        before = (self.state / "capacity.json").read_bytes()
        replace = Path.replace

        def fail_order(path: Path, target: str | os.PathLike[str]) -> Path:
            if Path(target).name == "order.json":
                raise OSError("fixture write failure")
            return replace(path, Path(target))

        with mock.patch.dict(os.environ, self.env, clear=True):
            settings = importlib.import_module("slotfather.dashboard_settings")
            errors = importlib.import_module("slotfather.config")
            dialog = settings.Settings(1.0)
            dialog.values.update(amount="8", order="window")
            with mock.patch.object(Path, "replace", fail_order), self.assertRaisesRegex(errors.SlotError, "cannot save"):
                dialog.apply()
        self.assertEqual(before, (self.state / "capacity.json").read_bytes())
        self.assertFalse((self.state / "order.json").exists())

    def test_narrow_advanced_modal_scrolls_and_ignores_outside_clicks(self) -> None:
        with mock.patch.dict(os.environ, self.env, clear=True):
            settings = importlib.import_module("slotfather.dashboard_settings")
            view = importlib.import_module("slotfather.dashboard")
            dialog = settings.Settings(1.0)
            dialog.advanced = True
            canvas = view.Canvas(36, 18)
            dialog.draw(canvas, None)
            self.assertIn("SETTINGS", canvas.render(False))
            self.assertIn("↓", canvas.render(False))
            dialog.mouse(("mouse", 0, 0, 0, True))
            self.assertEqual("mode", dialog.focus)
            for _ in range(12):
                dialog.mouse(("mouse", 65, 10, 5, True))
            dialog.draw(canvas, None)
            at_end = canvas.render(False)
            self.assertIn("↑", at_end)
            self.assertIn("tighten", at_end)
            self.assertNotRegex(at_end, r"↓ \d+ more")
            self.assertEqual("close", dialog.key("esc"))
        self.assertFalse(self.state.exists(), "viewing advanced settings creates no queue")

    def test_busy_capacity_apply_changes_neither_order_nor_capacity(self) -> None:
        holder = self.request("busy", 1)
        self.started("busy")
        with mock.patch.dict(os.environ, self.env, clear=True):
            settings = importlib.import_module("slotfather.dashboard_settings")
            errors = importlib.import_module("slotfather.config")
            dialog = settings.Settings(1.0)
            dialog.values.update(mode="fixed", amount="6", order="window", interval="3")
            with self.assertRaisesRegex(errors.SlotError, "queue is busy"):
                dialog.apply()
            self.assertFalse((self.state / "capacity.json").exists())
            self.assertFalse((self.state / "order.json").exists())
            dialog.values = {**dialog.original, "interval": "3"}
            self.assertEqual(3, dialog.apply(), "local refresh can change while shared capacity is locked")
        self.release("busy")
        self.finish(holder)

    def test_shared_changes_require_reload_after_another_cli_saves(self) -> None:
        with mock.patch.dict(os.environ, self.env, clear=True):
            settings = importlib.import_module("slotfather.dashboard_settings")
            errors = importlib.import_module("slotfather.config")
            dialog = settings.Settings(1.0)
            dialog.values.update(mode="fixed", amount="6")
            self.assertEqual(0, self.tool("order", "window").returncode)
            with self.assertRaisesRegex(errors.SlotError, "another process"):
                dialog.apply()
            self.assertFalse((self.state / "capacity.json").exists())
            dialog.reload()
            dialog.values.update(mode="fixed", amount="6")
            dialog.apply()
        self.assertIn("6 slots (saved)", self.tool("slots").stdout)
        self.assertIn("window", self.tool("order").stdout)

    def test_interval_only_and_opening_leave_missing_state_missing(self) -> None:
        with mock.patch.dict(os.environ, self.env, clear=True):
            settings = importlib.import_module("slotfather.dashboard_settings")
            errors = importlib.import_module("slotfather.config")
            dialog = settings.Settings(1.0)
            for value in ("0", "nan", "61", "typo"):
                dialog.values["interval"] = value
                with self.assertRaisesRegex(errors.SlotError, "Refresh interval"):
                    dialog.apply()
            dialog.values["interval"] = "5"
            self.assertEqual(5, dialog.apply())
            self.assertFalse(self.state.exists())

    def test_apply_capacity_uses_the_shared_cli_capacity(self) -> None:
        with mock.patch.dict(os.environ, self.env, clear=True):
            settings = importlib.import_module("slotfather.dashboard_settings")
            dialog = settings.Settings(1.0)
            dialog.values["mode"] = "fixed"
            dialog.values["amount"] = "6"
            self.assertEqual(1.0, dialog.apply())
        result = self.tool("slots")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("6 slots (saved)", result.stdout)

    def test_modal_keyboard_edits_a_draft_then_applies(self) -> None:
        with mock.patch.dict(os.environ, self.env, clear=True):
            settings = importlib.import_module("slotfather.dashboard_settings")
            view = importlib.import_module("slotfather.dashboard")
            dialog = settings.Settings(1.0)
            canvas = view.Canvas(90, 28)
            dialog.draw(canvas, None)
            self.assertIn("SETTINGS", canvas.render(False))
            dialog.key("right")
            dialog.key("down")
            dialog.key("\r")
            dialog.key("6")
            dialog.key("\r")
            self.assertFalse((self.state / "capacity.json").exists(), "edits remain a draft")
            self.assertEqual("applied", dialog.key("\x13"))
        self.assertIn("6 slots (saved)", self.tool("slots").stdout)

    def test_s_opens_settings_and_escape_cancels_in_a_real_terminal(self) -> None:
        master, slave = pty.openpty()
        process: subprocess.Popen[bytes] | None = None
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 28, 90, 0, 0))
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
            before = {p.relative_to(self.state): p.read_bytes() for p in self.state.rglob("*") if p.is_file()}
            os.write(master, b"s")
            receive("SETTINGS")
            os.write(master, b"\x1b[C\x1b[B\r9\r\x1b")
            receive("REQUESTS")
            after = {p.relative_to(self.state): p.read_bytes() for p in self.state.rglob("*") if p.is_file()}
            self.assertEqual(before, after, "closing a draft must leave shared state unchanged")
            os.write(master, b"s")
            receive("SETTINGS")
            os.write(master, b"\x1b[C\x1b[B\r6\r\x13")
            receive("REQUESTS")
            self.assertIn("6 slots (saved)", self.tool("slots").stdout)
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
