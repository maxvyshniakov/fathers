"""The overview through the `godfather` command, moved from Slotfather's DashboardTests: --once frames of queues the
fake Slotfather's snapshot describes, the options, and a click in the live view. What Slotfather decides (admission,
grants, the order of the queue) stays in Slotfather's tests; these only draw what its snapshot says."""
from __future__ import annotations

import re
import subprocess
import sys
import unittest

from helpers import ROOT
from terminal import LiveCase, environ, item


class OnceTests(LiveCase):
    def snapshot(self, width: int = 140, height: int = 40) -> str:
        result = subprocess.run([sys.executable, str(ROOT / "godfather"), "--once", "--width", str(width),
                                 "--height", str(height)], capture_output=True, text=True, env=environ(self.fake))
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout

    def test_an_all_lease_shows_all_above_eight_slots_and_its_cells_when_they_fit(self) -> None:
        self.queue(holders=[item("whole", 700, count=10, all=True)], slots=10)
        self.assertRegex(self.snapshot(), r"whole +all +[■░]")
        self.queue(holders=[item("fits", 701, count=4, all=True)], slots=4)
        self.assertRegex(self.snapshot(), r"fits +▪▪▪▪ +[■░]")

    def test_table_headers_share_the_border_and_empty_messages_start_under_them(self) -> None:
        lines = self.snapshot(width=220, height=50).splitlines()
        for title, message in (("QUEUE", "No requests"), ("EVENTS", "No grants")):
            row = next(i for i, line in enumerate(lines) if title in line)
            self.assertFalse(lines[row + 1][1:lines[row + 1].index("│", 1)].strip(), "a blank padding row first")
            self.assertEqual(lines[row].index(title), lines[row + 2].index(message), "then the message")
            self.assertIn("┍" if title == "QUEUE" else "╭", lines[row], "QUEUE holds the focus (fathers#139)")
            if title != "EVENTS":
                self.assertIn("pid", lines[row], "column names belong on the top border")

    def test_small_terminal_fallback_preserves_its_footer(self) -> None:
        for height in (5, 7):
            with self.subTest(height=height):
                text = self.snapshot(width=40, height=height)
                self.assertIn("Godfather", text.splitlines()[-1])
                self.assertIn("live", text.splitlines()[-1])

    def test_an_idle_queue_reads_open_with_every_slot_free(self) -> None:
        text = self.snapshot()
        self.assertIn("SLOTS", text.splitlines()[0])
        self.assertIn("free", text)
        self.assertIn("Godfather", text.splitlines()[-1])
        self.assertNotIn("Godfather", "\n".join(text.splitlines()[:-1]))
        self.assertNotIn("/fake/state", text)
        self.assertIn("No requests · all 4 slots free", text)
        self.assertNotIn("\x1b[", text, "a snapshot to a pipe is plain text")

    def test_a_blocked_head_shows_its_cells_and_place_and_the_selected_holder_on_the_frame(self) -> None:
        self.queue(holders=[item("holder", 800, count=2)],
                   waiters=[item("gate", 801, kind="waiting", count=4, all=True),
                            item("small", 802, kind="waiting", count=1)])
        state = self.fake.read()
        state["snapshot"]["dashboard"]["admission"] = 0
        self.fake.write(state)
        text = self.snapshot()
        self.assertIn("no admission", text)
        self.assertIn("━ QUEUE ", text)
        self.assertIn("┃  ▶️    holder", text, "the selected row on the frame, its state (#15), then the label")
        self.assertEqual(1, text.count("▶"), "only the selected row has a cursor")
        self.assertRegex(text, r"gate +▪▪▪▪ +waiting #1 ")
        self.assertRegex(text, r"small +▪ +waiting #2 ")

    def test_every_terminal_size_draws_without_error(self) -> None:
        self.queue(holders=[item("a-rather-long-label-for-a-holder", 900)])
        for width, height in ((20, 10), (49, 13), (50, 14), (80, 24), (109, 30), (110, 30), (200, 60)):
            text = self.snapshot(width, height)
            self.assertEqual(height, len(text.splitlines()), f"{width}x{height}")
            self.assertTrue(all(len(line) <= width for line in text.splitlines()), f"{width}x{height}")

    def test_control_bytes_in_labels_never_reach_the_terminal(self) -> None:
        self.queue(holders=[item("evil\x1b[2Jlabel", 901, cmd="rm\x07 -rf")])
        text = self.snapshot()
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\x07", text)
        self.assertIn("evil [2Jlabel", text)


class OptionTests(LiveCase):
    def run_godfather(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(ROOT / "godfather"), *args], capture_output=True, text=True,
                              env=environ(self.fake), stdin=subprocess.DEVNULL)

    def test_options_are_checked(self) -> None:
        for args in (["--interval", "0"], ["--width", "4"], ["--height", "x"], ["--bogus"], ["--page", "lanes"]):
            result = self.run_godfather(*args)
            self.assertEqual(2, result.returncode, args)
            self.assertTrue(result.stderr.startswith("godfather: "), (args, result.stderr))

    def test_without_a_terminal_it_prints_usage_and_exits_2(self) -> None:
        result = self.run_godfather()
        self.assertEqual(2, result.returncode)
        self.assertIn("usage: godfather", result.stderr)
        self.assertIn("--once prints one frame", result.stderr)


class ClickTests(LiveCase):
    def test_clicking_a_queue_row_updates_selected_in_the_live_terminal(self) -> None:
        holder = item("click-owner", 950, count=4, all=True)
        target = item("click-target", 951, kind="waiting")
        self.queue(holders=[holder], waiters=[target])
        screen = self.start(rows=40, columns=160,
                            argv=[sys.executable, str(ROOT / "godfather"), "--interval", "0.3"])
        screen.until(lambda text: re.search(r"label\s+click-owner", text) and re.search(r"status\s+running", text),
                     what="the holder's details")
        mark = screen.mark()
        screen.click(10, 10)  # QUEUE starts at row 6: holder at 8, the gap at 9, the waiter at 10 (1-based)
        screen.until(lambda text: re.search(r"label\s+click-target", text) and re.search(r"status\s+waiting", text),
                     what="the waiter selected", since=mark)
        granted = dict(target, kind="held", granted=target["since"], cpu=10.0, rss=1024, procs=1,
                       tree=[[951, 10.0, 1024, "python3"]])
        self.queue(holders=[granted])  # the next snapshot: the waiter got its slots, the holder ended
        mark = screen.mark()
        screen.until(lambda text: re.search(r"label\s+click-target", text) and re.search(r"status\s+running", text)
                     and re.search(r"▶️\s+click-target", text), what="the same request selected, now running",
                     since=mark)
        self.assertEqual(0, screen.quit())
        self.assertIn(b"\x1b[?1006l\x1b[?1000l", screen.raw, "mouse reporting is given back")


if __name__ == "__main__":
    unittest.main()
