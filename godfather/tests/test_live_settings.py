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
        screen.receive("SLOTFATHER", since=mark)
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



class SectionFlowTests(LiveCase):
    """fathers#87 review: reset, `D D` and Lanefather's and Junkfather's sections in the live dashboard, each write
    recorded with its revision."""

    def setUp(self) -> None:
        super().setUp()
        from helpers import FakeTool

        self.lane = FakeTool("lanefather", self.fake.bin, values={"rerun.wait": 600})
        self.junk = FakeTool("junkfather", self.fake.bin, values={"reap.grace": 30})

    @staticmethod
    def tool_writes(fake: object) -> list[list[str]]:
        return [argv for argv in fake.argv() if argv[:2] in (["settings", "set"], ["settings", "reset"])]  # type: ignore[attr-defined]

    def test_every_section_saves_resets_and_d_twice_with_its_revision(self) -> None:
        screen = self.start(rows=60, columns=120)
        screen.receive(" QUEUE ")
        mark = screen.mark()
        screen.write(b"s")
        screen.receive("LANEFATHER", "JUNKFATHER", "Rerun waits", since=mark)
        # Capacity is selected: Slotfather's 13 order rows, Lanefather's Review start, then its Rerun waits
        mark = screen.mark()
        screen.write(b"j" * 15)
        screen.write(b"\x1b[C")  # Rerun waits 10m -> 15m, saved once the keys settle
        screen.receive("Saved · Rerun waits 15m", since=mark)
        self.assertEqual({"rerun.wait": 900}, self.lane.read()["values"])
        mark = screen.mark()
        screen.write(b"d")  # reset the row
        screen.receive("Saved · Rerun waits 55m", since=mark)
        self.assertEqual({}, self.lane.read()["values"])
        mark = screen.mark()
        screen.write(b"G")  # Junkfather's last row: Grace before SIGKILL
        screen.write(b"DD")
        screen.receive("every Junkfather setting at its default", since=mark)
        self.assertEqual({}, self.junk.read()["values"])
        lane, junk = self.tool_writes(self.lane), self.tool_writes(self.junk)
        self.assertEqual(["settings", "set", "rerun.wait", "900", "--revision"], lane[0][:5])
        self.assertEqual(["settings", "reset", "rerun.wait", "--revision"], lane[1][:4])
        self.assertTrue(all(argv[argv.index("--revision") + 1].startswith("sha256:") for argv in lane))
        self.assertEqual([["settings", "reset", "--all", "--json"]], junk)
        self.assertEqual([], self.writes(), "Slotfather is not asked")
        self.assertEqual(0, screen.quit())

if __name__ == "__main__":
    unittest.main()
