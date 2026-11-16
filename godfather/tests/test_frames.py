"""How the live view writes its frames (fathers#143), read off the recorded byte stream: each frame inside one
synchronized update (mode 2026, ignored where unsupported), a resize taken from SIGWINCH through the loop's wake-up,
and the whole screen written again only on resize, a theme change or Ctrl-L, never on a timer. The clock is the
test's: sixty idle seconds pass in sixty steps, no wall-clock time."""
from __future__ import annotations

import contextlib
import datetime
import os
import re
import select as selector
import signal
import tempfile
import threading
import unittest
from typing import Any
from unittest import mock

from test_state import Case  # puts src/ on sys.path

ROW = re.compile(rb"\x1b\[(\d+);1H")
SYNC_ON, SYNC_OFF = b"\x1b[?2026h", b"\x1b[?2026l"


def frames(stream: bytes) -> list[bytes]:
    """The stream's synchronized updates, in order; fails on anything written outside one."""
    found: list[bytes] = []
    rest = stream
    while rest:
        start = rest.find(SYNC_ON)
        if start != 0:
            raise AssertionError(f"written outside a synchronized update: {rest[:200]!r}")
        end = rest.find(SYNC_OFF)
        if end < 0 or SYNC_ON in rest[len(SYNC_ON):end]:
            raise AssertionError(f"an update not closed before the next: {rest[:200]!r}")
        found.append(rest[len(SYNC_ON):end])
        rest = rest[end + len(SYNC_OFF):]
    return found


def rows(frame: bytes) -> set[int]:
    return {int(match[1]) for match in ROW.finditer(frame)}


class FrameTests(Case):
    def setUp(self) -> None:
        super().setUp()
        self.clock = 1000.0
        self.moment = datetime.datetime(2026, 11, 16, 12, 0, tzinfo=datetime.timezone.utc)
        self.size = os.terminal_size((156, 43))
        self.out, path = tempfile.mkstemp()  # a file never blocks a write, unlike a PTY whose reader is the test
        os.unlink(path)
        self.addCleanup(os.close, self.out)
        tool = self.tool

        class Screen(tool.Screen):
            def notice(self) -> None:  # Slotfather found: the overview, not the list of tools looked for
                return None

        keys, self.typist = os.pipe()  # keys that never come: the loop waits on them, as on a quiet terminal
        self.addCleanup(os.close, keys)
        self.addCleanup(os.close, self.typist)
        self.live: Any = tool.Live(keys, self.out, Screen(), 1.0, "auto")
        self.addCleanup(self.live.close)
        self.live.state = self.screen()
        size = mock.patch.object(tool, "terminal_size", self.terminal_size)
        size.start()
        self.addCleanup(size.stop)
        clock = mock.patch.object(tool.time, "monotonic", self.monotonic)
        clock.start()
        self.addCleanup(clock.stop)

    def terminal_size(self, _: int) -> os.terminal_size:
        return self.size

    def monotonic(self) -> float:
        return self.clock

    def stream(self) -> bytes:
        found = bytearray()
        while chunk := os.pread(self.out, 1 << 20, len(found)):
            found += chunk
        return bytes(found)

    def idle(self, seconds: int) -> None:
        """`seconds` of nothing happening, a pass a second as the loop makes them."""
        for _ in range(seconds):
            self.clock += 1
            self.moment += datetime.timedelta(seconds=1)
            self.live.send(self.tool.Tick(self.moment))
            self.live.draw()

    def test_every_frame_is_one_synchronized_update(self) -> None:
        self.live.draw()
        self.idle(5)
        self.live.send(self.tool.Key("down"))  # a key: some rows change
        self.live.draw()
        found = frames(self.stream())
        self.assertGreaterEqual(len(found), 2)
        self.assertEqual(set(range(1, 44)), rows(found[0]), "the first frame writes every row")
        self.assertIn(b"\x1b[2J", found[0], "cleared inside the first update")

    def test_sixty_idle_seconds_never_write_the_whole_screen_again(self) -> None:
        self.live.draw()
        mark = len(self.stream())
        self.idle(60)
        later = frames(self.stream()[mark:])
        self.assertTrue(later, "the clock still ticks")
        for frame in later:
            self.assertLess(len(rows(frame)), 43, "a second's tick writes only the rows it changed")
            self.assertNotIn(b"\x1b[2J", frame)

    def test_ctrl_l_writes_every_row_again(self) -> None:
        self.live.draw()
        mark = len(self.stream())
        self.live.send(self.tool.Key("\x0c"))
        self.live.draw()
        self.assertEqual([set(range(1, 44))], [rows(frame) for frame in frames(self.stream()[mark:])])

    def test_a_resize_arrives_as_a_signal_and_clears_the_screen(self) -> None:
        self.live.draw()
        mark = len(self.stream())
        self.size = os.terminal_size((120, 40))
        self.idle(3)
        self.assertNotIn(b"\x1b[2J", self.stream()[mark:], "the size is not polled")
        with self.live.resizes():
            os.kill(os.getpid(), signal.SIGWINCH)
        self.live.draw()
        last = frames(self.stream()[mark:])[-1]
        self.assertTrue(last.startswith(b"\x1b[2J"), "cleared, then drawn, in one update")
        self.assertEqual(set(range(1, 41)), rows(last))

    def test_a_waiting_loop_wakes_on_sigwinch(self) -> None:
        """The loop blocked on keys and its wake pipe returns when SIGWINCH comes, through the handler's byte: the
        wait is bounded (10 s) and what woke it is read from select's answer, not from a clock."""
        woke: list[list[int]] = []
        select = selector.select

        def watched(read: list[int], write: list[int], error: list[int], wait: float) -> Any:
            found = select(read, write, error, wait)
            woke.append(found[0])
            return found

        sender = threading.Thread(target=os.kill, args=(os.getpid(), signal.SIGWINCH))
        with self.live.resizes(), mock.patch.object(selector, "select", watched):
            sender.start()
            self.live.read(10)
        sender.join()
        self.assertEqual([[self.live.wake_read]], woke, "woken by the handler's byte, once")
        self.assertTrue(self.live.resized)

    def test_a_full_wake_pipe_never_blocks_the_handler(self) -> None:
        self.assertFalse(os.get_blocking(self.live.wake_write))
        with contextlib.suppress(BlockingIOError):
            while True:
                os.write(self.live.wake_write, b"x" * 4096)
        self.live.on_resize()  # returns: the loop is woken already
        self.assertTrue(self.live.resized)

    def test_resizes_puts_the_previous_handler_back(self) -> None:
        before = signal.getsignal(signal.SIGWINCH)
        with self.live.resizes():
            self.assertNotEqual(before, signal.getsignal(signal.SIGWINCH))
        self.assertEqual(before, signal.getsignal(signal.SIGWINCH))


if __name__ == "__main__":
    unittest.main()
