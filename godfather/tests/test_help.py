"""The help modal explains queue decisions and keeps input inside its own viewport."""
from __future__ import annotations

import datetime
import os
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
import helpers  # noqa: F401 - src/ on sys.path
import view as _view
view = _view.current()


class HelpTests(unittest.TestCase):
    def test_sections_in_order_terms_in_a_column_and_hanging_descriptions(self) -> None:
        self.assertEqual(["KEYS", "STATES", "PRIORITY", "SLOTS", "QUEUE", "DETAILS", "EVENTS", "MACHINE", "SETTINGS"],
                         [title for title, _ in view.HELP])
        lines = view.help_lines(60)
        texts = ["".join(text for text, _ in parts) for parts in lines]
        titles = [index for index, parts in enumerate(lines) if parts and parts[0][1] == "title"]
        self.assertEqual(0, titles[0], "the first title right under the top padding row")
        blanks = {index for index, parts in enumerate(lines) if not parts}
        expected = {title + 1 for title in titles} | {title - n for title in titles[1:] for n in (1, 2)}
        self.assertEqual(expected, blanks, "two blank lines before a title but the first, one after, none elsewhere")
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

    def test_the_text_starts_under_the_titles_first_letter(self) -> None:
        for draw, title in ((view.draw_help, "HELP"), (view.draw_changelog, "CHANGELOG")):
            with self.subTest(title=title):
                canvas = view.Canvas(100, 40)
                draw(canvas)
                info = canvas.help_view
                lines = canvas.render(False).splitlines()
                title_x = lines[info.y].index(f" {title} ") + 1
                inside = [line[info.x + 1:info.x + info.width - 1] for line in lines[info.y + 1:info.y + info.height - 2]]
                first = next(text for text in inside if text.strip())
                self.assertEqual(title_x, info.x + 1 + len(first) - len(first.lstrip()), first)
                self.assertEqual(title_x, info.x + 3)

    def test_changelog_versions_have_the_help_rhythm(self) -> None:
        releases = view.parse_changelog(view.changelog_text())
        lines = view.changelog_lines(70, releases, "0.1.0")
        titles = [index for index, parts in enumerate(lines) if parts and parts[0][1] == "title"]
        self.assertEqual(0, titles[0])
        blanks = {index for index, parts in enumerate(lines) if not parts}
        self.assertEqual({title + 1 for title in titles} | {title - n for title in titles[1:] for n in (1, 2)}, blanks)

    def test_every_hotkey_is_in_keys(self) -> None:
        terms = " ".join(entry[0] for entry in dict(view.HELP)["KEYS"])
        for key in ("↑ ↓", "pgup pgdn", "home end", "tab", "s", "?", "esc", "q", "shift+drag"):
            self.assertIn(key, terms)

    def test_narrow_help_wraps_and_reaches_its_end_with_interior_overflow_hints(self) -> None:
        canvas = view.Canvas(36, 14)
        canvas.scroll = {"requests": 4, "processes": 2}
        view.draw_help(canvas)
        text = canvas.render(False)
        lines = text.splitlines()
        info = canvas.help_view
        self.assertEqual(" ", lines[info.y + 1][info.x + info.width - 2], "a blank padding row over the lines")
        self.assertIn(lines[info.y + 2][info.x + info.width - 2], "┃╻╹", "the thumb at the top: more below")
        self.assertNotRegex(text, r"↓ \d+ more|↑ \d+ above", "no row counts")
        self.assertNotIn("…", text, "help paragraphs wrap instead of truncating")
        for parts in view.help_lines(30):
            self.assertLessEqual(view.text_width("".join(text for text, _ in parts)), 30)
        self.assertEqual("scroll", view.help_key(canvas, "end", canvas.scroll))
        view.draw_help(canvas)
        at_end = canvas.render(False)
        info = canvas.help_view
        self.assertIn(at_end.splitlines()[info.y + 1 + info.visible][info.x + info.width - 2], "┃╻╹",
                      "the thumb at the bottom of the lines")
        self.assertFalse(at_end.splitlines()[info.y + 2 + info.visible][info.x + 1:info.x + info.width - 1].strip(),
                         "a blank padding row under them, also at the end")
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
                self.assertRegex(text, r"[┃╻╹]│", "the scrollbar shows there is more")
                self.assertIn("Godfather", text.splitlines()[-1])

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
        from helpers import FakeSlotfather
        from terminal import Terminal, environ
        fake = FakeSlotfather()
        self.addCleanup(fake.close)
        screen = Terminal(self, environ(fake), rows=18, columns=80)
        screen.receive(" QUEUE ")
        mark = screen.mark()
        screen.write(b"?")
        screen.receive("HELP", since=mark)
        mark = screen.mark()
        screen.write(b"\x1b[F")
        self.assertIn("esc close", screen.receive("Built-in.", "esc close", since=mark))
        mark = screen.mark()
        screen.write(b"\x1b[H")
        screen.receive("next panel", since=mark)
        mark = screen.mark()
        screen.write(b"\x1b")
        screen.receive(" QUEUE ", since=mark)
        mark = screen.mark()
        screen.write(b"?\x1b[F")
        screen.receive("Built-in.", since=mark)
        self.assertEqual(0, screen.quit(), "q keeps its quit-dashboard meaning inside help")
        self.assertEqual([], [argv for argv in fake.argv() if argv[:2] in (["settings", "set"], ["settings", "reset"])],
                         "help writes nothing to Slotfather")


if __name__ == "__main__":
    unittest.main()
