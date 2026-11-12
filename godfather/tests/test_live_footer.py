"""The footer in the live dashboard, moved from Slotfather: removed keys do nothing, and a click on a hint does what
its key does, in the dashboard, help and Settings."""
from __future__ import annotations

import re
import unittest

from terminal import LiveCase


class RemovedKeyTests(LiveCase):
    def test_p_r_and_m_do_nothing_in_the_live_view(self) -> None:
        screen = self.start()
        screen.receive("QUEUE")
        before = screen.mark()
        for key in (b"p", b" ", b"r", b"m", b"M", b"R"):
            screen.write(key)
        for _ in range(30):  # 1.5 s
            screen.read()
        after = re.sub(rb"\x1b\[[0-9;?]*[ -/]*[@-~]", b"", bytes(screen.raw[before:]))
        self.assertNotIn(b"paused", after)
        self.assertNotIn(b"text selection", after)
        self.assertNotIn(b"\x1b[?1006l", screen.raw[before:], "mouse reporting stays on")
        self.assertNotIn(b"\x1b[2J", screen.raw[before:], "no refresh clears the screen")
        self.assertEqual(0, screen.quit())


class HintClickLiveTests(LiveCase):
    def test_clicks_on_footer_hints_act_like_their_keys(self) -> None:
        screen = self.start(rows=30, columns=140)

        def click(text: str, row: int) -> None:
            screen.click(screen.rows()[row].index(text) + 1, row)

        # The hints are centred between the left and right content: wait for the right side's last update
        screen.until(lambda _: "updated" in screen.rows().get(30, ""), what="the footer's last update")
        mark = screen.mark()
        click("? help", 30)
        screen.receive("esc close", since=mark)
        help_row = next(r for r, line in screen.rows().items() if "esc close" in line)
        mark = screen.mark()
        click("esc close", help_row)
        screen.receive("QUEUE", since=mark)
        mark = screen.mark()
        click("tab panel", 30)
        screen.receive("↑↓ scroll", since=mark)
        mark = screen.mark()
        click("s settings", 30)
        screen.receive("enter edit", since=mark)
        screen.until(lambda _: any("esc close" in line and "enter" in line for line in screen.rows().values()),
                     what="the Settings footer")  # the rest of it can come in a later PTY read
        settings_row = next(r for r, line in screen.rows().items() if "esc close" in line and "enter" in line)
        mark = screen.mark()
        click("esc close", settings_row)
        screen.receive("QUEUE", since=mark)
        click("q quit", 30)
        self.assertEqual(0, screen.exit_code(), "a click on q quit quits")
        self.assertEqual([], self.writes(), "no click saved anything")


if __name__ == "__main__":
    unittest.main()
