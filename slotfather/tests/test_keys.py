"""Dashboard hotkeys on any keyboard layout (#13)."""
from __future__ import annotations

import sys as _path_sys
from pathlib import Path as _Path
_path_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "src"))

import fcntl
import importlib
import os
import pty
import re
import select
import signal
import struct
import subprocess
import termios
import time
import unittest
from typing import Any
from unittest import mock

from slotfather import keys as sf_keys

from test_slotfather import ROOT, SlotCase

RUSSIAN = {"й": "q", "Й": "Q", "ы": "s", ",": "?", "з": "p", "к": "r", "ь": "m", "в": "d", "В": "D", "о": "j",
           "л": "k", "п": "g", "П": "G", "р": "h", "д": "l"}


def keys_of(data: bytes) -> list[Any]:
    tool = importlib.import_module("slotfather.dashboard")
    read, write = os.pipe()
    try:
        os.write(write, data)
        return tool.read_keys(read)
    finally:
        os.close(read)
        os.close(write)


class LayoutTests(unittest.TestCase):
    def test_every_hotkey_through_the_russian_and_ukrainian_layouts(self) -> None:
        for typed, meant in RUSSIAN.items():
            self.assertEqual(meant, sf_keys.hotkey(typed), typed)
        self.assertEqual("s", sf_keys.hotkey("і"), "Ukrainian і sits where Russian ы does")
        for latin in "qspmrdDjkgG?hl":
            self.assertEqual(latin, sf_keys.hotkey(latin), "Latin keys stay as they are")
        self.assertEqual("up", sf_keys.hotkey("up"))

    def test_a_kitty_key_event_carries_its_base_layout_key(self) -> None:
        found = keys_of(b"\x1b[1099::115u\x1b[1074:1042:100;2u\x1b[63u\x1b[13u\x1b[9;2u\x1b[27u\x1b[115;5u"
                        b"\x1b[49u\x1b[57441;2u\x1b[A")
        self.assertEqual(["ы", "В", "?", "\r", "back_tab", "esc", "\x13", "1", "up"], found,
                         "a lone modifier key (57441) is dropped")
        self.assertEqual(["s", "D", "?"], [sf_keys.hotkey(key) for key in found[:3]])
        self.assertEqual("1", sf_keys.hotkey(found[7]))

    def test_keypad_keys_are_the_ordinary_keys_and_ctrl_c_interrupts(self) -> None:
        self.assertEqual(["1", "0", ".", "-", "+", "\r", "up", "page_down"],
                         keys_of(b"\x1b[57400u\x1b[57399u\x1b[57409u\x1b[57412u\x1b[57413u\x1b[57414u\x1b[57419u"
                                 b"\x1b[57422u"))
        with self.assertRaises(KeyboardInterrupt):
            keys_of(b"\x1b[99;5u")
        with self.assertRaises(KeyboardInterrupt):
            keys_of(b"\x1b[1089:1057:99;5u")  # Ctrl with the Russian с, at the c position

    def test_a_kitty_sequence_split_across_reads_is_completed(self) -> None:
        tool = importlib.import_module("slotfather.dashboard")
        read, write = os.pipe()
        try:
            os.write(write, b"\x1b[1099::1")
            self.assertEqual([], tool.read_keys(read))
            os.write(write, b"15u")
            self.assertEqual(["s"], [sf_keys.hotkey(key) for key in tool.read_keys(read)])
        finally:
            os.close(read)
            os.close(write)


class SettingsKeyTests(SlotCase):
    def test_hotkeys_translate_and_typed_values_stay_as_typed(self) -> None:
        with mock.patch.dict(os.environ, self.env, clear=True):
            settings = importlib.import_module("slotfather.dashboard_settings")
            dialog = settings.Settings(1.0)
            dialog.focus = "safety"
            self.assertEqual("handled", dialog.key("д"), "д is l: the next value, pending")
            self.assertEqual(3.5, dialog.pending.value)
            self.assertEqual("changed", dialog.key("\r"), "Enter saves it")
            dialog.focus = "interval"
            self.assertEqual("handled", dialog.key("\r"))
            for key in ("ю", "5"):
                dialog.key(key)
            self.assertEqual("5", dialog.buffer, "ю types nothing into a number, never the . at its position")
            dialog.key("esc")
            self.assertEqual("changed", dialog.key("в"), "в is d: reset the selected setting")
            self.assertEqual("close", dialog.key("ы"), "ы is s: close")


class TerminalTests(SlotCase):
    def test_the_live_dashboard_asks_for_base_keys_takes_russian_hotkeys_and_restores_the_mode(self) -> None:
        master, slave = pty.openpty()
        process: subprocess.Popen[bytes] | None = None
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
            process = subprocess.Popen([str(ROOT / "slotfather"), "dashboard", "--interval", "60"],
                                       stdin=slave, stdout=slave, stderr=slave, env=self.env, start_new_session=True)
            os.close(slave)
            slave = -1
            screen = bytearray()

            def receive(fragment: bytes) -> None:
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    if select.select([master], [], [], 0.1)[0]:
                        try:
                            screen.extend(os.read(master, 65536))
                        except OSError:
                            break
                    if fragment in re.sub(rb"\x1b\[[0-9;?]*[ -/]*[@-~]", b"", bytes(screen)) or fragment in screen:
                        return
                self.fail(f"terminal did not show {fragment!r}: {bytes(screen)!r}")

            receive("QUEUE".encode())
            self.assertIn(sf_keys.KITTY_ON, screen, "the kitty protocol is asked for")
            os.write(master, "ы".encode())
            receive(b"STATUS")
            os.write(master, b"\x1b")
            os.write(master, b"\x1b[1081::113u")  # kitty: й, at the q position
            deadline = time.monotonic() + 8
            while process.poll() is None and time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    try:
                        screen.extend(os.read(master, 65536))
                    except OSError:
                        break
            self.assertEqual(0, process.wait(timeout=1), "q by its base key quits")
            self.assertIn(sf_keys.KITTY_OFF, screen, "the terminal's mode is restored")
        finally:
            if process is not None and process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=3)
            os.close(master)
            if slave >= 0:
                os.close(slave)

    def test_ctrl_c_under_the_protocol_quits_from_the_dashboard_and_from_settings(self) -> None:
        for opening in (b"", "ы".encode()):
            master, slave = pty.openpty()
            process: subprocess.Popen[bytes] | None = None
            try:
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
                process = subprocess.Popen([str(ROOT / "slotfather"), "dashboard", "--interval", "60"],
                                           stdin=slave, stdout=slave, stderr=slave, env=self.env,
                                           start_new_session=True)
                os.close(slave)
                slave = -1
                screen = b""
                deadline = time.monotonic() + 8
                while b"QUEUE" not in screen and time.monotonic() < deadline:
                    if select.select([master], [], [], 0.1)[0]:
                        screen += os.read(master, 65536)
                if opening:
                    os.write(master, opening)
                    while b"STATUS" not in screen and time.monotonic() < deadline:
                        if select.select([master], [], [], 0.1)[0]:
                            screen += os.read(master, 65536)
                os.write(master, b"\x1b[99;5u")  # Ctrl-C as the protocol reports it
                deadline = time.monotonic() + 8
                while process.poll() is None and time.monotonic() < deadline:
                    if select.select([master], [], [], 0.1)[0]:
                        try:
                            screen += os.read(master, 65536)
                        except OSError:
                            break
                self.assertEqual(0, process.wait(timeout=1), f"Ctrl-C quits ({opening!r})")
                self.assertIn(sf_keys.KITTY_OFF, screen)
            finally:
                if process is not None and process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=3)
                os.close(master)
                if slave >= 0:
                    os.close(slave)

    def test_the_mode_is_restored_when_the_dashboard_fails(self) -> None:
        tool = importlib.import_module("slotfather.dashboard")
        master, slave = pty.openpty()
        read, write = os.pipe()  # the screen: a pipe, so that restoring the terminal never waits for a reader
        try:
            with self.assertRaises(RuntimeError):
                with tool.full_screen(slave, write):
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
