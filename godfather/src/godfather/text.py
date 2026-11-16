"""Text as the screen measures and words it, with no terminal, canvas or clock: cell widths of glyphs, cutting to a
width, durations, times of day, sizes, a home-relative path, a label made safe to show, and which hint a click at a
cell presses. Data and layout read these without a view (fathers#141); tui draws with them."""
from __future__ import annotations

import datetime
import math
import os
import unicodedata
from typing import Any

Part = tuple[str, str]  # (text, style)


JOINER, TEXT_STYLE, EMOJI_STYLE = "\u200d", "\ufe0e", "\ufe0f"  # zero-width joiner, variation selectors 15 and 16


def dashboard_text(value: Any) -> str:
    """A label or command as plain text: any process writes queue entries, so control bytes are blanked. The
    zero-width joiner of an emoji sequence (👨‍🦳) stays."""
    return "".join(char if char.isprintable() or char == JOINER else " " for char in str(value))


def cell_width(char: str) -> int:
    if unicodedata.combining(char) or char in (JOINER, TEXT_STYLE, EMOJI_STYLE):
        return 0
    return 2 if unicodedata.east_asian_width(char) in "WF" else 1


def clusters(text: str) -> list[str]:
    """Text as the glyphs a terminal draws (#15): each character with the combining marks, variation selectors, skin
    tones and zero-width-joined characters after it, so ▶️ and 👨‍🦳 are one glyph each."""
    found: list[str] = []
    joined = False
    for char in text:
        if found and (joined or char in (JOINER, TEXT_STYLE, EMOJI_STYLE) or unicodedata.combining(char)
                      or 0x1F3FB <= ord(char) <= 0x1F3FF):
            found[-1] += char
        else:
            found.append(char)
        joined = char == JOINER
    return found


def cluster_width(cluster: str) -> int:
    """Cells a glyph takes: an emoji sequence (VS16 or joined) two, else its first character's width."""
    if len(cluster) > 1 and (EMOJI_STYLE in cluster or JOINER in cluster):
        return 2
    return cell_width(cluster[0]) if cluster else 0


def text_width(value: Any) -> int:
    return sum(cluster_width(cluster) for cluster in clusters(dashboard_text(value)))


def clip(value: Any, width: int) -> str:
    """value cut to `width` cells, ending in … when cut; a glyph is never split."""
    text = dashboard_text(value)
    if text_width(text) <= width:
        return text
    kept: list[str] = []
    used = 0
    for cluster in clusters(text):
        if used + cluster_width(cluster) > width - 1:
            break
        kept.append(cluster)
        used += cluster_width(cluster)
    return "".join(kept) + "…" if width > 0 else ""


def span(seconds: float, compact: bool = False) -> str:
    """A duration: `3m25s`, `1h05m`, `2d03h`; `compact` (Settings' durations, fathers#87) drops the parts that are
    zero and keeps a fraction of a second: `30m`, `1h30m`, `1d1h`, `0.1s`."""
    if compact:
        return compact_span(seconds)
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02}s"
    if seconds < 86400:
        return f"{seconds // 3600}h{seconds // 60 % 60:02}m"
    return f"{seconds // 86400}d{seconds // 3600 % 24:02}h"


def minutes(seconds: float) -> str:
    """A long span to the minute: 45s, 23m, 1h05m."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m" if seconds < 3600 else f"{seconds // 3600}h{seconds // 60 % 60:02}m"


def clock(moment: datetime.datetime | None, now: datetime.datetime | None = None) -> str:
    """A time of day, its weekday too when it is not on the day of `now` (the moment the screen is drawn at; None:
    the same day). It never reads the clock (fathers#138)."""
    if moment is None:
        return "--:--:--"
    local = moment.astimezone()
    same = now is None or local.date() == now.astimezone().date()
    return local.strftime("%H:%M:%S" if same else "%a %H:%M")


def megabytes(value: float | None) -> str:
    if value is None:
        return "?"
    return f"{value / 1024:.1f} GB" if value >= 1024 else f"{value:.0f} MB"


def short(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".")


def tilde(path: str) -> str:
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path == home or path.startswith(home + os.sep) else path


def parse_utc(value: Any) -> datetime.datetime | None:
    try:
        return datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return None


def compact_span(seconds: Any) -> str:
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or not math.isfinite(seconds) \
            or seconds < 0:
        return str(seconds)
    whole = int(seconds)
    rest = float(seconds) - whole
    days, whole = divmod(whole, 86400)
    hours, whole = divmod(whole, 3600)
    mins, secs = divmod(whole, 60)
    parts = [f"{days}d" if days else "", f"{hours}h" if hours else "", f"{mins}m" if mins else "",
             f"{round(secs + rest, 3):g}s" if secs or rest else ""]
    return "".join(parts) or "0s"


def hint_at(hints: Any, x: int, y: int) -> str | None:
    """The key a click at (x, y) on a hint presses, None between hints: the one lookup a drawn canvas and the
    reducer's last frame share (fathers#141). `hints` are (row, from, to, key), the later drawn first."""
    return next((press for row, start, end, press in reversed(list(hints)) if row == y and start <= x < end), None)
