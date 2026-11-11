"""The help modal explains queue decisions and keeps input inside its own viewport."""
from __future__ import annotations

import datetime
import fcntl
import importlib
import os
import pty
import re
import select
import signal
import struct
import subprocess
import tempfile
import termios
import time
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
view = importlib.import_module("slotfather.dashboard")


class HelpTests(unittest.TestCase):
    def test_sections_in_order_terms_in_a_column_and_hanging_descriptions(self) -> None:
        self.assertEqual(["KEYS", "STATES", "SLOTS", "QUEUE", "DETAILS", "EVENTS", "MACHINE", "SETTINGS"],
                         [title for title, _ in view.HELP])
        lines = view.help_lines(60)
        texts = ["".join(text for text, _ in parts) for parts in lines]
        self.assertEqual([index for index, parts in enumerate(lines) if parts and parts[0][1] == "title"],
                         [0] + [index + 1 for index, parts in enumerate(lines) if not parts],
                         "one blank line between sections, none inside them")
        for title, entries in view.HELP:
            start = texts.index(title)
            terms = [entry for entry in entries if entry[0]]
            column = view.HELP_INDENT + max(view.text_width(entry[0]) for entry in terms) + view.HELP_GAP
            for term, description in ((entry[0], entry[1]) for entry in terms):
                parts = next(lines[i] for i in range(start, len(lines)) if len(lines[i]) == 4 and lines[i][1][0] == term)
                self.assertEqual("bold", parts[1][1], f"{title}: {term} stands out")
                self.assertEqual(column, view.text_width("".join(text for text, _ in parts[:3])),
                                 f"{title}: {term}'s description starts in the section's column, emoji too")
                self.assertTrue(description.startswith(parts[3][0]))
        cpu = next(i for i, text in enumerate(view.help_lines(40)) if "".join(t for t, _ in text).startswith("  cpu"))
        follow = "".join(text for text, _ in view.help_lines(40)[cpu + 1])
        width = view.HELP_INDENT + len("slots") + view.HELP_GAP
        self.assertEqual(" " * width, follow[:width], "a wrapped description hangs under its column")
        self.assertNotEqual(" ", follow[width])
        for parts in lines:
            for text, style in parts:
                if text.strip() and style == "bold":
                    self.assertTrue(text.strip())

    def test_every_hotkey_is_in_keys(self) -> None:
        terms = " ".join(entry[0] for entry in dict(view.HELP)["KEYS"])
        for key in ("↑ ↓", "pgup pgdn", "home end", "tab", "s", "?", "esc", "q", "shift+drag"):
            self.assertIn(key, terms)

    def test_narrow_help_wraps_and_reaches_its_end_with_interior_overflow_hints(self) -> None:
        canvas = view.Canvas(36, 14)
        canvas.scroll = {"requests": 4, "processes": 2}
        view.draw_help(canvas)
        text = canvas.render(False)
        self.assertRegex(text, r"↓ \d+ more")
        self.assertNotIn("…", text, "help paragraphs wrap instead of truncating")
        for parts in view.help_lines(30):
            self.assertLessEqual(view.text_width("".join(text for text, _ in parts)), 30)
        self.assertEqual("scroll", view.help_key(canvas, "end", canvas.scroll))
        view.draw_help(canvas)
        at_end = canvas.render(False)
        self.assertIn("↑", at_end)
        self.assertNotRegex(at_end, r"↓ \d+ more")
        self.assertIn("Built-in.", at_end)
        self.assertIn("esc close", at_end)
        self.assertEqual(4, canvas.scroll["requests"])
        self.assertEqual(2, canvas.scroll["processes"])
        info = canvas.help_view
        self.assertEqual(info.total - info.visible, info.offset)
        self.assertNotIn("above", at_end.splitlines()[info.y])
        self.assertNotIn("close", at_end.splitlines()[info.y + info.height - 1])

    def test_help_remains_available_when_terminal_is_narrow_or_queue_is_unavailable(self) -> None:
        model: dict[str, object] = {"updated": datetime.datetime.now(datetime.timezone.utc),
                                   "slots": 6, "used": 0, "holders": [], "waiters": []}
        for contents in (None, model):
            with self.subTest(unavailable=contents is None):
                canvas = view.Canvas(36, 14)
                view.dashboard_draw(canvas, contents, help_open=True)
                text = canvas.render(False)
                self.assertIn("HELP", text)
                self.assertRegex(text, r"↓ \d+ more")
                self.assertIn("Slotfather", text.splitlines()[-1])

    def test_modal_keyboard_and_wheel_do_not_change_underlying_tables_or_selection(self) -> None:
        canvas = view.Canvas(80, 20)
        scroll = {"requests": 4, "processes": 2}
        canvas.scroll = scroll
        canvas.tables["requests"] = dict(x=0, y=0, width=80, height=20, top=2,
                                      visible=10, count=20, offset=4, base=0, row_items=None)
        view.draw_help(canvas)
        self.assertEqual("scroll", view.help_key(canvas, "down", scroll))
        self.assertEqual(1, scroll["help"])
        self.assertEqual("scroll", view.help_key(canvas, "page_down", scroll))
        self.assertEqual(canvas.help_view.visible, scroll["help"])
        self.assertEqual("scroll", view.help_key(canvas, "page_up", scroll))
        self.assertEqual(1, scroll["help"])
        self.assertEqual("scroll", view.help_key(canvas, "home", scroll))
        self.assertEqual(0, scroll["help"])
        self.assertEqual(7, view.mouse_action(canvas, ("mouse", 65, 10, 5, True), scroll, 7))
        self.assertEqual(3, scroll["help"])
        self.assertEqual(7, view.mouse_action(canvas, ("mouse", 0, 10, 5, True), scroll, 7))
        self.assertEqual(7, view.mouse_action(canvas, ("mouse", 65, 0, 5, True), scroll, 7))
        self.assertEqual(3, scroll["help"], "outside-modal input is ignored")
        for key in ("p", "m", "r", "unrecognized"):
            self.assertEqual("ignored", view.help_key(canvas, key, scroll))
        for key in ("?", "esc"):
            self.assertEqual("close", view.help_key(canvas, key, scroll))
        self.assertEqual("quit", view.help_key(canvas, "q", scroll))
        self.assertEqual(4, scroll["requests"])
        self.assertEqual(2, scroll["processes"])

    def test_page_keys_are_distinct_from_home_and_end(self) -> None:
        read, write = os.pipe()
        try:
            os.write(write, b"\x1b[5~\x1b[6~\x1b[H\x1b[F")
            self.assertEqual(["page_up", "page_down", "home", "end"], view.read_keys(read))
        finally:
            os.close(read)
            os.close(write)

    def test_live_terminal_scrolls_help_and_closes_it_without_writing_the_queue(self) -> None:
        with tempfile.TemporaryDirectory(prefix="slotfather-help-") as directory:
            root = Path(directory)
            memory = root / "memory"
            memory.write_text("8000 1\n")
            state = root / "state"
            env = {name: value for name, value in os.environ.items() if not name.startswith("SLOTFATHER")}
            env.update(SLOTFATHER_DIR=str(state), SLOTFATHER_LEGACY_DIRS="", SLOTFATHER_CORES="12",
                       SLOTFATHER_MEMORY_GIB="32", SLOTFATHER_LOADAVG="0", SLOTFATHER_MEMORY_FILE=str(memory))
            master, slave = pty.openpty()
            process: subprocess.Popen[bytes] | None = None
            try:
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 18, 80, 0, 0))
                process = subprocess.Popen([str(ROOT / "slotfather"), "dashboard", "--interval", "60"],
                                           stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True)
                os.close(slave)
                slave = -1
                screen = bytearray()

                def receive(fragment: str, *also: str) -> str:
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        if select.select([master], [], [], 0.1)[0]:
                            try:
                                screen.extend(os.read(master, 65536))
                            except OSError:
                                break
                        plain = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", screen.decode("utf-8", "replace"))
                        if all(text in plain for text in (fragment, *also)):
                            return plain
                    self.fail(f"terminal did not show {fragment!r}: {screen.decode('utf-8', 'replace')!r}")

                receive("─ QUEUE ")
                before = {path.relative_to(state): path.read_bytes() for path in state.rglob("*") if path.is_file()}
                screen.clear()
                os.write(master, b"?")
                receive("HELP")
                screen.clear()
                os.write(master, b"\x1b[F")
                self.assertIn("esc close", receive("Built-in.", "esc close"))
                screen.clear()
                os.write(master, b"\x1b[H")
                receive("next panel")
                screen.clear()
                os.write(master, b"\x1b")
                receive("─ QUEUE ")
                screen.clear()
                os.write(master, b"?\x1b[F")
                receive("Built-in.")
                os.write(master, b"q")
                deadline = time.monotonic() + 5
                while process.poll() is None and time.monotonic() < deadline:
                    if select.select([master], [], [], 0.1)[0]:
                        try:
                            os.read(master, 65536)  # keep consuming the final frame while waiting for exit
                        except OSError:
                            break
                self.assertEqual(0, process.wait(timeout=1), "q keeps its quit-dashboard meaning inside help")
                after = {path.relative_to(state): path.read_bytes() for path in state.rglob("*") if path.is_file()}
                self.assertEqual(before, after)
            finally:
                if process is not None and process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=3)
                os.close(master)
                if slave >= 0:
                    os.close(slave)


if __name__ == "__main__":
    unittest.main()
