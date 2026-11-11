"""Dashboard hotkeys on any keyboard layout (#13).

A hotkey is the key at a US-QWERTY position, whatever layout types there: `s` opens Settings also where the key types
`ы`. Where the terminal speaks the kitty keyboard protocol, the dashboard asks it to report every key as an escape
code with its base-layout key (KITTY_ON), and a key carries that base key (Key.base). Elsewhere a table maps the
common non-Latin layouts (Russian ЙЦУКЕН, Ukrainian) to the US position. Only hotkeys are translated: what a person
types into a Settings field stays as typed."""
from __future__ import annotations

import re

# Progressive enhancement: disambiguate (1), report alternate keys (4), report all keys as escape codes (8). Pushed
# on the terminal's stack on entry and popped on the way out; a terminal without the protocol ignores both.
KITTY_FLAGS = 1 | 4 | 8
KITTY_ON = f"\x1b[>{KITTY_FLAGS}u".encode()
KITTY_OFF = b"\x1b[<u"
# CSI key-code[:shifted[:base]] [; modifiers[:event]] [; text] u
KITTY_KEY = re.compile(r"\x1b\[(\d+)(?::(\d*))?(?::(\d+))?(?:;(\d*)(?::\d+)?)?(?:;[\d:]*)?u")

_US_SHIFT = dict(zip("`1234567890-=[]\\;',./", "~!@#$%^&*()_+{}|:\"<>?"))
_RU_LOWER, _RU_UPPER = "ёйцукенгшщзхъфывапролджэячсмитьбю.", "ЁЙЦУКЕНГШЩЗХЪФЫВАПРОЛДЖЭЯЧСМИТЬБЮ,"
_US_LOWER, _US_UPPER = "`qwertyuiop[]asdfghjkl;'zxcvbnm,./", "~QWERTYUIOP{}ASDFGHJKL:\"ZXCVBNM<>?"
LAYOUT = {**dict(zip(_RU_LOWER, _US_LOWER)), **dict(zip(_RU_UPPER, _US_UPPER)),
          # Ukrainian: the same, with і, ї, є, ґ where Russian has ы, ъ, э and ё
          "і": "s", "І": "S", "ї": "]", "Ї": "}", "є": "'", "Є": '"', "ґ": "`", "Ґ": "~"}
# Only ',' (Russian shift+/, where `?` opens help) is ASCII among the layout's characters; '.' (Russian /) stays
# '.', since a US keyboard types it into Settings.
del LAYOUT["."]


# The protocol's keypad keys (num lock on: digits and operators) as the ordinary keys they stand for.
KEYPAD = {**{57399 + n: str(n) for n in range(10)}, 57409: ".", 57410: "/", 57411: "*", 57412: "-", 57413: "+",
          57414: "\r", 57415: "=", 57416: ",", 57417: "left", 57418: "right", 57419: "up", 57420: "down",
          57421: "page_up", 57422: "page_down", 57423: "home", 57424: "end", 57426: "\x7f"}


class Key(str):
    """A key as typed, with the US-QWERTY key at its position when the terminal said which (`base`)."""
    base: str | None = None

    def __new__(cls, text: str, base: str | None = None) -> "Key":
        key = super().__new__(cls, text)
        key.base = base
        return key


def hotkey(key: str) -> str:
    """The hotkey a key means: its base-layout key where the terminal reported one, else its US-QWERTY position on a
    known layout, else the key itself."""
    base = getattr(key, "base", None)
    if base:
        return base
    return LAYOUT.get(key, key) if len(key) == 1 else key


def kitty(match: re.Match[str]) -> str | None:
    """One kitty-protocol key event as the dashboard's key: text, a control character, or a named key; None for a
    key the dashboard does not use (a lone modifier, a keypad or media key)."""
    code = int(match[1])
    shifted = int(match[2]) if match[2] else None
    base = int(match[3]) if match[3] else None
    modifiers = max(0, int(match[4] or 1) - 1)
    shift, ctrl = bool(modifiers & 1), bool(modifiers & 4)
    special = {13: "\r", 9: "back_tab" if shift else "\t", 127: "\x7f", 8: "\x7f", 27: "esc"}
    if code in special:
        return special[code]
    if code in KEYPAD:
        return KEYPAD[code]
    if code >= 57344 or code < 32:  # the protocol's other functional keys (private use area) and C0 codes
        return None
    us = chr(base) if base is not None else chr(code) if chr(code).isascii() else LAYOUT.get(chr(code))
    if ctrl:
        return chr(ord(us.lower()) & 0x1F) if us and us.isalpha() else None
    text = chr(shifted) if shift and shifted else chr(code).upper() if shift and chr(code).isalpha() else chr(code)
    if us is None:
        return Key(text)
    if shift:
        us = us.upper() if us.isalpha() else _US_SHIFT.get(us, us)
    return Key(text, us)
