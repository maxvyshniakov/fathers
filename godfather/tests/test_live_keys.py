"""Hotkeys on any keyboard layout (#13) in the live dashboard, moved from Slotfather: the kitty keyboard protocol is
asked for and given back, a Russian layout's keys work by their position, Ctrl-C under the protocol quits, and
Settings translates hotkeys while typed values stay as typed."""
from __future__ import annotations

import os
import select
import unittest
from unittest import mock

from terminal import LiveCase

from godfather import keys as sf_keys
from godfather import tui


class SettingsKeyTests(LiveCase):
    def test_hotkeys_translate_and_typed_values_stay_as_typed(self) -> None:
        """Keys reach Settings through the mode table, which translates them once (modes.press())."""
        from test_settings import SAFETY, INTERVAL, Clock, Settings

        with mock.patch.dict(os.environ, self.fake.environ()):
            settings = Settings(Clock())
            settings.select(SAFETY)
            self.assertNotIn("load-model", settings.key("д"), "д is l: the next value, pending")
            assert settings.panel.pending is not None
            self.assertEqual(3.5, settings.panel.pending.value)
            self.assertIn("load-model", settings.key("\r"), "Enter saves it")
            self.assertEqual(3.5, self.fake.read()["order"]["safety"])
            settings.select(INTERVAL)
            settings.key("\r")
            self.assertTrue(settings.panel.editing)
            settings.key("ю", "5")
            self.assertEqual("5", settings.panel.buffer, "ю types nothing into a number, never the . at its position")
            settings.key("esc")
            settings.select(SAFETY)
            self.assertIn("load-model", settings.key("в"), "в is d: reset the selected setting")
            settings.key("ы")
            self.assertIsNone(settings.state.overlay, "ы is s: close")


class TerminalTests(LiveCase):
    def test_the_live_dashboard_asks_for_base_keys_takes_russian_hotkeys_and_restores_the_mode(self) -> None:
        screen = self.start()
        screen.receive("QUEUE")
        self.assertIn(sf_keys.KITTY_ON, screen.raw, "the kitty protocol is asked for")
        mark = screen.mark()
        screen.write("ы")
        screen.receive("SLOTFATHER", since=mark)
        screen.write(b"\x1b")
        self.assertEqual(0, screen.quit(b"\x1b[1081::113u"), "q by its base key (kitty: й at the q position) quits")
        self.assertIn(sf_keys.KITTY_OFF, screen.raw, "the terminal's mode is restored")

    def test_ctrl_c_under_the_protocol_quits_from_the_dashboard_and_from_settings(self) -> None:
        for opening in (b"", "ы".encode()):
            with self.subTest(opening=opening):
                screen = self.start()
                screen.receive("QUEUE")
                if opening:
                    mark = screen.mark()
                    screen.write(opening)
                    screen.receive("SLOTFATHER", since=mark)
                self.assertEqual(0, screen.quit(b"\x1b[99;5u"), f"Ctrl-C quits ({opening!r})")
                self.assertIn(sf_keys.KITTY_OFF, screen.raw)

    def test_the_mode_is_restored_when_the_dashboard_fails(self) -> None:
        import pty
        master, slave = pty.openpty()
        read, write = os.pipe()  # the screen: a pipe, so that restoring the terminal never waits for a reader
        try:
            with self.assertRaises(RuntimeError):
                with tui.full_screen(slave, write):
                    raise RuntimeError("boom")
            out = b""
            while select.select([read], [], [], 0.1)[0]:
                out += os.read(read, 65536)
            self.assertLess(out.index(sf_keys.KITTY_ON), out.index(sf_keys.KITTY_OFF))
        finally:
            for fd in (master, slave, read, write):
                os.close(fd)


if __name__ == "__main__":
    unittest.main()
