"""The dashboard footer: the name on the left, the hints centred, the status on the right; no pause, refresh or
select-text keys."""
from __future__ import annotations

import helpers  # noqa: F401 - src/ on sys.path
import view as _view

import datetime
import os
import unittest
from typing import Any

def judged(memory: dict[str, Any]) -> dict[str, Any]:
    """Memory facts with Slotfather's judgement of them, as its snapshot carries it (docs/contracts/slotfather.md):
    low above pressure level 1, swap low under 512 MB free."""
    level, free = memory.get("level"), memory.get("swap_free")
    return dict(memory, low=None if level is None else level > 1,
                swap_low=bool(memory.get("swap_total")) and free is not None and free < 512)



HINTS = ["↑↓ select", "tab panel", "s settings", "? help", "q quit"]
DROPS = ["? help", "s settings", "tab panel", "↑↓ select"]  # the order a narrow footer drops them in


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

    def test_the_hints_are_centred_in_order_between_the_name_and_the_status(self) -> None:
        for width in (120, 80, 60):
            with self.subTest(width=width):
                line = self.footer(width)
                self.assertTrue(line.startswith(" Godfather"), line)
                self.assertRegex(line.rstrip(), r"updated \d\d:\d\d:\d\d  ● live$")
                status = line.index("updated")
                shown = [hint for hint in HINTS if hint in line]
                self.assertIn("q quit", shown, "quit is the last to go")
                self.assertEqual(set(HINTS) - set(shown), set(DROPS[:len(HINTS) - len(shown)]), "help goes first")
                hints = "   ".join(shown)
                self.assertIn(hints, line, "navigation, panels, settings, help, quit last")
                start = line.index(hints)
                self.assertLess(start + len(hints), status - 1, "never over the status")
                if width == 120:
                    self.assertEqual(HINTS, shown)
                    self.assertLessEqual(abs(start + len(hints) / 2 - width / 2), 1, "centred on the footer")

    def test_the_version_follows_the_name_and_goes_before_any_hint(self) -> None:
        version = self.tool.running_version()
        self.assertTrue(version, "a commit or semver")
        self.assertIn(f" Godfather {version} ", self.footer(120))
        self.assertIn(f" Godfather {version} ↻ ", self.footer(120, installed="another release"),
                      "installed after this dashboard started: it runs older code until it restarts")
        self.assertNotIn("↻", self.footer(120, installed=self.tool.running_source()))
        narrow = self.footer(80)
        self.assertNotIn(version, narrow, "the version goes first")
        self.assertIn("↑↓ select", narrow)

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
