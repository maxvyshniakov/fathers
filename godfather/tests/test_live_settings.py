"""Settings in the live dashboard (#7), moved from Slotfather's TerminalTests: s opens it, an arrow change is shown
at once and saved through `slotfather settings set` once the keys settle, a typed value saves on Enter, Esc
cancels an edit and then closes the panel."""
from __future__ import annotations

import unittest

from terminal import LiveCase


class TerminalTests(LiveCase):
    def test_s_opens_settings_a_change_applies_at_once_and_escape_closes(self) -> None:
        screen = self.start(rows=40, columns=100)
        screen.receive(" QUEUE ")
        mark = screen.mark()
        screen.write(b"s")
        screen.receive("STATUS", since=mark)
        mark = screen.mark()
        screen.write(b"\x1b[C")  # Capacity mode: Automatic -> Fixed, shown at once
        screen.receive("saves in 1 s", since=mark)
        screen.receive("Saved", since=mark)  # once the keys have settled
        self.assertEqual({"slots": 4}, self.fake.read()["capacity"], "fixed keeps the current count")
        mark = screen.mark()
        screen.write(b"\x1b[B\r6\r")  # Slots: type 6, Enter saves
        screen.receive("capacity 6 slots", since=mark)
        self.assertEqual({"slots": 6}, self.fake.read()["capacity"])
        mark = screen.mark()
        screen.write(b"\r9\x1b")  # a second edit, cancelled
        screen.receive("Edit cancelled", since=mark)
        self.assertEqual({"slots": 6}, self.fake.read()["capacity"])
        mark = screen.mark()
        screen.write(b"\x1b")
        screen.receive(" QUEUE ", since=mark)
        self.assertEqual(0, screen.quit())
        sets = [argv for argv in self.writes() if argv[1] == "set"]
        self.assertEqual([["settings", "set", "capacity.mode", "fixed"], ["settings", "set", "capacity.slots", "6"]],
                         [argv[:4] for argv in sets], "two saves, each through Slotfather")
        self.assertTrue(all("--revision" in argv for argv in sets), "a capacity change carries its revision")


if __name__ == "__main__":
    unittest.main()
