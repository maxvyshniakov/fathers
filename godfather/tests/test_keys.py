"""Dashboard hotkeys on any keyboard layout (#13)."""
from __future__ import annotations

import helpers  # noqa: F401 - src/ on sys.path
import view as _view

import os
import unittest
from typing import Any

from godfather import keys as sf_keys


RUSSIAN = {"й": "q", "Й": "Q", "ы": "s", ",": "?", "з": "p", "к": "r", "ь": "m", "в": "d", "В": "D", "о": "j",
           "л": "k", "п": "g", "П": "G", "р": "h", "д": "l"}


def keys_of(data: bytes) -> list[Any]:
    tool = _view.current()
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
        tool = _view.current()
        read, write = os.pipe()
        try:
            os.write(write, b"\x1b[1099::1")
            self.assertEqual([], tool.read_keys(read))
            os.write(write, b"15u")
            self.assertEqual(["s"], [sf_keys.hotkey(key) for key in tool.read_keys(read)])
        finally:
            os.close(read)
            os.close(write)






if __name__ == "__main__":
    unittest.main()
