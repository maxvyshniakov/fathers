"""The dashboard footer: the name on the left, the hints centred, the status on the right; no pause, refresh or
select-text keys."""
from __future__ import annotations

import helpers  # noqa: F401 - src/ on sys.path
import view as _view

import datetime
import os
import re
import unittest
from typing import Any

def judged(memory: dict[str, Any]) -> dict[str, Any]:
    """Memory facts with Slotfather's judgement of them, as its snapshot carries it (docs/contracts/slotfather.md):
    low above pressure level 1, swap low under 512 MB free."""
    level, free = memory.get("level"), memory.get("swap_free")
    return dict(memory, low=None if level is None else level > 1,
                swap_low=bool(memory.get("swap_total")) and free is not None and free < 512)



HINTS = ["↑↓ select", "tab panel", "s settings", "? help", "q quit"]
DROPS = ["tab panel", "s settings", "↑↓ select", "? help"]  # the order a narrow footer drops them in
LABEL = "Godfather  what's new?"


def model() -> dict[str, Any]:
    now = datetime.datetime.now(datetime.timezone.utc)
    return dict(holders=[], waiters=[], slots=4, cores=12, saved=False, averages=(1.0, 1.0, 1.0),
                advice=dict(unleased_load=0, jobs=4, capacity=4), admission=4, memory_block=None,
                memory=judged(dict(level=1, used=512, total=1024, swap_total=0, swap_free=None)), history=[], events=[],
                updated=now, order="backfill", start=None, limit=2700, used=0, free=4)


class FooterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tool: Any = _view.fresh()

    def footer(self, width: int, **extra: Any) -> str:
        canvas = self.tool.Canvas(width, 30)
        self.tool.dashboard_draw(canvas, dict(model(), **extra))
        return canvas.render(False).splitlines()[-1].ljust(width)

    def styled(self, width: int, **draw: Any) -> list[Any]:
        canvas = self.tool.Canvas(width, 30)
        self.tool.dashboard_draw(canvas, model(), **draw)
        return canvas.cells[-1]

    def test_the_hints_are_centred_between_the_left_and_right_content_at_every_width(self) -> None:
        for width in (156, 120, 100, 76, 60, 44):
            with self.subTest(width=width):
                line = self.footer(width)
                self.assertTrue(line.startswith(" " + LABEL), line)
                self.assertRegex(line.rstrip(), r"(updated )?(\d\d:\d\d:\d\d  )?● live$")
                shown = [hint for hint in HINTS if hint in line]
                self.assertEqual(set(HINTS) - set(shown), set(DROPS[:len(HINTS) - len(shown)]),
                                 "tab, settings and the arrows go before help")
                if not shown:
                    continue
                hints = "   ".join(shown)
                self.assertIn(hints, line, "navigation, panels, settings, help, quit last")
                start = line.index(hints)
                before = start - (1 + len(LABEL))
                right = re.search(r"(updated )?(\d\d:\d\d:\d\d  )?● live", line)
                assert right
                after = right.start() - (start + len(hints))
                self.assertGreaterEqual(min(before, after), 2, "never against the label or the status")
                self.assertLessEqual(abs(before - after), 1, f"centred between them: {before} and {after}")
                if width >= 120:
                    self.assertEqual(HINTS, shown)

    def test_help_stays_at_medium_widths(self) -> None:
        self.assertIn("↑↓ select   s settings   ? help   q quit", self.footer(100))
        self.assertIn("↑↓ select   ? help   q quit", self.footer(76))
        self.assertIn("updated", self.footer(100))

    def test_whats_new_is_the_text_colour_while_anything_is_unread_else_muted_never_the_accent(self) -> None:
        from godfather import news as _news
        news = _news.News((), (_news.Item("slotfather", "0.2.0", "2026-11-12", "Added", "x"),))
        name, after = len("Godfather"), len("  what's new?")
        for read, style in (({}, "text"), ({"slotfather": "0.1.0"}, "text"), ({"slotfather": "0.2.0"}, "dim")):
            with self.subTest(read=read):
                cells = self.styled(120, news=news, read=read)
                self.assertEqual({"title"}, {cell[1] for cell in cells[1:1 + name]}, "the name, as before fathers#102")
                self.assertEqual({style}, {cell[1] for cell in cells[1 + name:1 + name + after]})
        cells = self.styled(120)
        self.assertEqual("dim", cells[1 + name + 2][1], "nothing loaded yet: muted")
        line = "".join(cell[0] for cell in cells)
        self.assertEqual("dim", cells[line.index("? help") + 2][1], "muted like the key hints")

    def test_no_counter_and_the_restart_mark_after_it(self) -> None:
        self.assertNotRegex(self.footer(120)[:30], r"\d")
        self.assertIn(f" {LABEL} ↻ ", self.footer(120, installed="another release"),
                      "installed after this dashboard started: it runs older code until it restarts")
        self.assertNotIn("↻", self.footer(120, installed=self.tool.running_source()))

    def test_a_scrollable_panel_has_the_arrows_scroll(self) -> None:
        canvas = self.tool.Canvas(120, 30)
        canvas.focus = "details"
        self.tool.dashboard_draw(canvas, model())
        self.assertIn("↑↓ scroll   tab panel", canvas.render(False).splitlines()[-1])

    def test_the_removed_keys_and_states_are_gone(self) -> None:
        line = self.footer(120)
        for gone in ("pause", "refresh", "select text", "text selection", "paused"):
            self.assertNotIn(gone, line)
        keys = dict(self.tool.HELP)["KEYS"]
        terms = [entry[0] for entry in keys if entry[0]]
        for gone in ("p", "r", "m", "space"):
            self.assertNotIn(gone, terms)
        self.assertIn("shift+drag", terms, "text is selected with Shift+drag")


class HintClickTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tool: Any = _view.fresh()

    def targets(self, canvas: Any, row: int, hints: list[str]) -> dict[str, list[str | None]]:
        """For each hint text on the row: what a click on each of its cells presses."""
        line = "".join(cell[0] for cell in canvas.cells[row])
        found: dict[str, list[str | None]] = {}
        for hint in hints:
            start = line.index(hint)
            found[hint] = [canvas.hint_at(x, row) for x in range(start, start + len(hint))]
        return found

    def test_every_footer_hint_presses_its_key_and_the_gaps_nothing(self) -> None:
        canvas = self.tool.Canvas(140, 30)
        self.tool.dashboard_draw(canvas, model())
        row = canvas.height - 1
        found = self.targets(canvas, row, HINTS)
        self.assertEqual(["up", "down"], found["↑↓ select"][:2], "each arrow its own key")
        for hint, key in (("tab panel", "\t"), ("s settings", "s"), ("? help", "?"), ("q quit", "q")):
            self.assertEqual({key}, set(found[hint]), f"the whole of {hint}, key and label")
        line = "".join(cell[0] for cell in canvas.cells[row])
        gap = line.index("tab panel") - 2
        self.assertIsNone(canvas.hint_at(gap, row), "between hints: nothing")

    def test_overlay_footers_are_click_targets_too(self) -> None:
        for which, hints in ((self.tool.Help(), {"esc close": "esc", "q quit": "q"}),
                             (self.tool.Changelog(), {"esc close": "esc"})):
            canvas = self.tool.Canvas(100, 30)
            self.tool.dashboard_draw(canvas, model(), overlay=which)
            info = canvas.help_view
            row = info.y + info.height - 2
            found = self.targets(canvas, row, ["↑↓ scroll", *hints])
            self.assertEqual(["up", "down"], found["↑↓ scroll"][:2])
            for hint, key in hints.items():
                self.assertEqual({key}, set(found[hint]), f"{which}: {hint}")

    def test_the_settings_footer_is_a_click_target_too(self) -> None:
        from unittest import mock

        from helpers import FakeSlotfather
        from godfather.views import settings as dashboard_settings
        fake = FakeSlotfather()
        self.addCleanup(fake.close)
        with mock.patch.dict(os.environ, fake.environ()):
            dialog = dashboard_settings.Settings(1.0)
            canvas = self.tool.Canvas(100, 44)
            dialog.draw(canvas, None)
        _, y, _, height = dialog.bounds
        found = self.targets(canvas, y + height - 2, ["↑↓ select", "←→ change", "enter edit", "d reset", "esc close"])
        self.assertEqual(["up", "down"], found["↑↓ select"][:2])
        self.assertEqual(["left", "right"], found["←→ change"][:2])
        for hint, key in (("enter edit", "\r"), ("d reset", "d"), ("esc close", "esc")):
            self.assertEqual({key}, set(found[hint]), hint)






if __name__ == "__main__":
    unittest.main()
