"""Settings (fathers#87) as a value: one section per present tool, read with `<tool> settings --json` and written
with `settings set` and `settings reset` (SPEC.md, Settings contract), after this dashboard's own section (Refresh
every, Colour theme). Everything here is pure: the panel is a frozen value, every transition returns the new panel
and the requests the app carries out (a read, a check, a write, a change of this dashboard's own settings), and
their answers come back as events (state.py). The keys that drive it are in the mode table (modes.py).

An arrow change shows `pending` and is saved once the keys have settled for SETTLE_SECONDS, at once on Enter, or
first when the cursor moves away; a typed value is saved on Enter. Each pending or typed value is checked with
`settings check`: its error shows in red under the row and blocks the save, its preview shows under the row. A save
passes the row's revision: a stale one is a conflict, nothing is saved and the row shows the tool's current value. A
refusal (invalid, read-only, failed) shows once, in red under its row. `d` resets the selected row, `D` twice within
FLASH_SECONDS resets the selected row's section only."""
from __future__ import annotations

import dataclasses
import math
import os
from dataclasses import dataclass, field
from typing import Any, Mapping, NamedTuple, Optional, Sequence, cast

from . import layout as _layout
from . import theme as _theme

GODFATHER = "godfather"
ORDER = ("godfather", "slotfather", "lanefather", "junkfather")  # the sections, top to bottom
SETTLE_SECONDS = 1.0  # quiet after an arrow change before it is saved
FLASH_SECONDS = 4.0  # how long a confirmation shows, and how long a first D stays armed
READ_EVERY = {"slotfather": 1.0}  # seconds between reads of a tool's settings while Settings is open
READ_OTHERS = 2.0
DEFAULT_INTERVAL = 1.0
EDITABLE = ("choice", "boolean", "integer", "number", "duration", "interval", "theme")  # what Godfather can change
NUMBERS = ("integer", "number", "duration", "interval")
CAPACITY = ("capacity.mode", "capacity.slots", "capacity.gib_per_slot")  # Slotfather's, one revision
STATUS_LABEL = 13  # columns of a status label
FIELD_LABEL = 28  # columns of a setting's label, its cursor included
LOCAL_DESCRIPTIONS = {
    "interval": "Seconds between refreshes, 0.1 to 60 · this dashboard only.",
    "theme": "Auto, Terminal (palette only) or Built-in colours · this dashboard only.",
}
TITLES = {"godfather": "GODFATHER", "slotfather": "SLOTFATHER", "lanefather": "LANEFATHER", "junkfather": "JUNKFATHER"}
NAMES = {"godfather": "Godfather", "slotfather": "Slotfather", "lanefather": "Lanefather", "junkfather": "Junkfather"}


# The value

@dataclass(frozen=True)
class Section:
    """One tool's section: its last `settings --json` reply, why it cannot be read now (`problem`, shown as one
    dimmed row), and its reads. `readable` is False for a tool that has no settings contract this Godfather knows:
    never read, its problem says why. `generation` goes up with each write, so a read started before one is
    dropped when it lands."""
    tool: str
    reply: Optional[dict[str, Any]] = None
    problem: Optional[str] = None
    readable: bool = True
    read_at: float = -math.inf
    reading: bool = False
    generation: int = 0


@dataclass(frozen=True)
class Pending:
    """A change shown but not saved yet: an arrow change, saved at `at` + SETTLE_SECONDS or at once on Enter; or,
    once sent (`sent`), a save waiting for the tool's answer. `revision` is the row's as the change began."""
    tool: str
    key: str
    value: Any
    at: float
    revision: Optional[str] = None
    sent: bool = False


@dataclass(frozen=True)
class Flash:
    """What the last change did, under the rule while it is news: (text, style) since `at`."""
    text: str
    style: str
    at: float


@dataclass(frozen=True)
class Refusal:
    """Why the tool refused a change of one row: shown once, in red, under that row."""
    tool: str
    key: str
    text: str


Focus = tuple[str, str]  # (tool, key)
CheckKey = tuple[str, str, str, str]  # (tool, key, the value as given, the row's revision)


@dataclass(frozen=True)
class Panel:
    sections: tuple[Section, ...] = ()
    focus: Focus = ("", "")
    editing: bool = False
    buffer: str = ""
    replace: bool = True
    pending: Optional[Pending] = None
    saving: tuple[Pending, ...] = ()  # saves sent, oldest first
    flash: Optional[Flash] = None
    refusal: Optional[Refusal] = None
    armed: Optional[float] = None  # when D was pressed once
    offset: int = 0
    reveal: bool = True  # the next placement scrolls the focused row into view
    checked: Mapping[CheckKey, dict[str, Any]] = field(default_factory=lambda: cast(dict[CheckKey, Any], {}))
    asked: frozenset[CheckKey] = frozenset()
    kind: str = "settings"


class Context(NamedTuple):
    """What a transition reads but does not own: this dashboard's own settings, the model drawn (held and waiting
    slots, the capacity the queue read), the refresh error, and the screen's size."""
    interval: float = DEFAULT_INTERVAL
    theme: str = _theme.DEFAULT_MODE
    model: Optional[dict[str, Any]] = None
    problem: Optional[str] = None
    width: int = 0
    height: int = 0


@dataclass(frozen=True)
class Request:
    """What the app carries out for a transition: read · check · set · reset (a tool's settings CLI), local (this
    dashboard's own settings changed), changed (a tool saved: the model reloads), close, quit."""
    kind: str
    tool: str = ""
    key: Optional[str] = None
    value: Optional[str] = None
    revision: Optional[str] = None
    generation: int = 0
    interval: float = DEFAULT_INTERVAL
    theme: str = _theme.DEFAULT_MODE


Result = tuple[Panel, Context, list[Request]]


class Row(NamedTuple):
    """One setting as shown, from its tool's schema (or this dashboard's own)."""
    tool: str
    key: str
    label: str
    type: str
    value: Any
    writable: bool
    source: str
    revision: Optional[str]
    description: str
    schema: dict[str, Any]

    @property
    def focus(self) -> Focus:
        return self.tool, self.key

    @property
    def editable(self) -> bool:
        return self.writable and self.type in EDITABLE


class Line(NamedTuple):
    """One line of the list: kind is title, problem, status, row, note or rule; `focus` the row it selects (rows
    and their notes), parts are (column, text, style)."""
    kind: str
    focus: Optional[Focus]
    parts: tuple[tuple[int, str, str], ...]


# Text

def number(value: Any) -> str:
    return f"{float(value):g}" if isinstance(value, (int, float)) and not isinstance(value, bool) else str(value)


def plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def duration(seconds: Any) -> str:
    """Seconds as 1h, 30m, 1h30m, 45s, 1d2h; a fraction of a second stays (0.1s)."""
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or not math.isfinite(seconds) \
            or seconds < 0:
        return str(seconds)
    whole = int(seconds)
    rest = float(seconds) - whole
    days, whole = divmod(whole, 86400)
    hours, whole = divmod(whole, 3600)
    minutes, secs = divmod(whole, 60)
    parts = [f"{days}d" if days else "", f"{hours}h" if hours else "", f"{minutes}m" if minutes else "",
             f"{number(round(secs + rest, 3))}s" if secs or rest else ""]
    return "".join(parts) or "0s"


def argument(value: Any) -> str:
    """A value as `settings set` takes it: 1800, 0.05, fixed."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def tilde(text: str) -> str:
    """A leading path under the home directory, tilde-shortened."""
    home = os.path.expanduser("~")
    return "~" + text[len(home):] if text == home or text.startswith(home + os.sep) else text


def value_text(row: Row, value: Any) -> str:
    """A value as shown, its unit beside it: 3×, 0.1 cores, 4 GiB, 1h30m, never."""
    kind = row.type
    if kind == "theme":
        return _theme.MODE_LABELS.get(str(value), str(value))
    if kind == "interval":
        return duration(value)
    if value is None:
        return "–"
    if kind == "choice":
        labels = {str(choice.get("value")): str(choice.get("label") or choice.get("value"))
                  for choice in choices(row)}
        return labels.get(str(value), str(value).capitalize())
    if kind == "boolean":
        return "On" if value else "Off"
    if kind in NUMBERS and isinstance(value, (int, float)) and not isinstance(value, bool):
        if not value and row.schema.get("zero"):
            return str(row.schema["zero"])
        if kind == "duration":
            return duration(value)
        unit = str(row.schema.get("unit") or "")
        if not unit or unit.lower() == row.label.lower():
            return number(value)
        return number(value) + ("" if unit == "×" else " ") + unit
    return tilde(str(value))


def choices(row: Row) -> list[dict[str, Any]]:
    if row.type == "theme":
        return [{"value": mode, "label": _theme.MODE_LABELS[mode]} for mode in _theme.MODES]
    found = row.schema.get("choices")
    return [cast(dict[str, Any], item) for item in cast(list[Any], found) if isinstance(item, dict)] \
        if isinstance(found, list) else []


# The rows

def section(panel: Panel, tool: str) -> Optional[Section]:
    return next((found for found in panel.sections if found.tool == tool), None)


def local_rows(context: Context) -> list[Row]:
    return [Row(GODFATHER, "interval", "Refresh every", "interval", context.interval, True, "saved", None,
                LOCAL_DESCRIPTIONS["interval"], {}),
            Row(GODFATHER, "theme", "Colour theme", "theme", context.theme, True, "saved", None,
                LOCAL_DESCRIPTIONS["theme"], {})]


def schema_rows(found: Section) -> list[dict[str, Any]]:
    """Every setting of a reply, in its order: one flat list (owner, 2026-11-11)."""
    reply = found.reply or {}
    rows: list[dict[str, Any]] = []
    for part in cast(list[Any], reply.get("sections") or []):
        if isinstance(part, dict):
            items = cast(list[Any], cast(dict[str, Any], part).get("settings") or [])
            rows.extend(cast(dict[str, Any], item) for item in items
                        if isinstance(item, dict) and cast(dict[str, Any], item).get("key"))
    return rows


def tool_row(tool: str, item: dict[str, Any], reply: dict[str, Any]) -> Row:
    writable = item.get("writable", True) is not False and reply.get("writable", True) is not False
    revision = item.get("revision")
    return Row(tool, str(item["key"]), str(item.get("label") or item["key"]), str(item.get("type") or "text"),
               item.get("value"), writable, str(item.get("source") or ""),
               str(revision) if revision is not None else None, str(item.get("description") or ""), item)


def shown(panel: Panel, tool: str, key: str, saved: Any) -> Any:
    """A row's value as shown: pending, else being saved, else saved."""
    for change in (panel.pending, *reversed(panel.saving)):
        if change is not None and (change.tool, change.key) == (tool, key):
            return change.value
    return saved


def rows(panel: Panel, context: Context, tool: str) -> list[Row]:
    """A section's rows as shown now: a row whose `when` names another row's values shows only while that row
    shows one of them (Slotfather's Slots in Fixed, GiB per slot in the memory modes)."""
    if tool == GODFATHER:
        return local_rows(context)
    found = section(panel, tool)
    if found is None or found.reply is None:
        return []
    items = schema_rows(found)
    values = {str(item["key"]): shown(panel, tool, str(item["key"]), item.get("value")) for item in items}
    shown_rows: list[Row] = []
    for item in items:
        when = item.get("when")
        if isinstance(when, dict) and not all(values.get(str(key)) in (allowed if isinstance(allowed, list) else [])
                                              for key, allowed in cast(dict[str, Any], when).items()):
            continue
        shown_rows.append(tool_row(tool, item, found.reply))
    return shown_rows


def tools(panel: Panel) -> list[str]:
    """The sections drawn, in ORDER: this dashboard's, then each present tool's."""
    present = {found.tool for found in panel.sections}
    return [tool for tool in ORDER if tool == GODFATHER or tool in present]


def all_rows(panel: Panel, context: Context) -> list[Row]:
    return [row for tool in tools(panel) for row in rows(panel, context, tool)]


def selectable(panel: Panel, context: Context) -> list[Row]:
    return [row for row in all_rows(panel, context) if row.editable]


def row_of(panel: Panel, context: Context, focus: Optional[Focus] = None) -> Optional[Row]:
    wanted = focus or panel.focus
    return next((row for row in all_rows(panel, context) if row.focus == wanted), None)


def focused(panel: Panel, context: Context) -> Optional[Row]:
    found = row_of(panel, context)
    return found if found is not None and found.editable else None


def saved_value(panel: Panel, context: Context, tool: str, key: str) -> Any:
    if tool == GODFATHER:
        return context.interval if key == "interval" else context.theme
    found = section(panel, tool)
    item = next((item for item in schema_rows(found) if item["key"] == key), None) if found else None
    return item.get("value") if item else None


def shown_value(panel: Panel, context: Context, row: Row) -> Any:
    return shown(panel, row.tool, row.key, saved_value(panel, context, row.tool, row.key))


# Checks and what a value would do

def check_key(row: Row, text: str) -> CheckKey:
    return row.tool, row.key, text, row.revision or ""


def check_reply(panel: Panel, row: Row, text: str) -> Optional[dict[str, Any]]:
    return panel.checked.get(check_key(row, text))


def held_and_waiting(context: Context) -> tuple[int, list[int]]:
    model = context.model or {}
    return int(model.get("used", 0)), [int(waiter.get("count", 1)) for waiter in model.get("waiters", [])]


def drain_note(slots: int, context: Context) -> str:
    drain = max(0, held_and_waiting(context)[0] - slots)
    return f"will drain {drain}: running work keeps its slots" if drain else ""


def starts(slots: int, context: Context) -> int:
    """How many waiting requests a capacity of `slots` lets start now by slot counts alone, in queue order."""
    held, waiting = held_and_waiting(context)
    free, started = slots - held, 0
    for count in waiting:
        if 0 < count <= free:
            free, started = free - count, started + 1
    return started


def capacity_effect(slots: int, context: Context) -> tuple[str, str]:
    drain, start = drain_note(slots, context), starts(slots, context)
    if drain:
        return f"→ {plural(slots, 'slot')} · {drain}", "warn"
    return f"→ {plural(slots, 'slot')}" + (f" · will start {start} waiting" if start else ""), "dim"


def answer_note(row: Row, reply: Optional[dict[str, Any]], context: Context, typed: bool) -> tuple[str, str]:
    """Under a pending or typed row: why the tool refuses the value, or what it would do; "" for nothing to say."""
    if reply is None:
        return "checking…", "dim"
    if not reply.get("ok"):
        return "✗ " + str(reply.get("error") or "Not accepted."), "bad"
    if row.tool == "slotfather" and row.key in CAPACITY and isinstance(reply.get("slots"), int):
        return capacity_effect(int(reply["slots"]), context)
    if reply.get("preview"):
        return str(reply["preview"]), "dim"
    return ("Enter saves", "dim") if typed else ("", "dim")


def parse_typed(row: Row, text: str) -> tuple[Any, str]:
    """(value, "") for typed text this dashboard can read for `row`, else (None, why not)."""
    try:
        value = float(text)
    except ValueError:
        return None, "Enter a number."
    if not math.isfinite(value):
        return None, "Enter a number."
    if row.type == "interval":
        return (value, "") if 0.1 <= value <= 60 else (None, "Enter seconds from 0.1 to 60.")
    if row.type == "integer":
        return (int(value), "") if value.is_integer() else (None, "Enter a whole number.")
    return value, ""


def typed_note(panel: Panel, context: Context, row: Row) -> tuple[str, str]:
    if not panel.buffer:
        return "", "dim"
    if row.tool == GODFATHER:
        _, problem = parse_typed(row, panel.buffer)
        return ("✗ " + problem, "bad") if problem else ("Enter saves", "dim")
    return answer_note(row, check_reply(panel, row, panel.buffer), context, True)


def pending_note(panel: Panel, context: Context, row: Row) -> tuple[str, str]:
    change = panel.pending
    if change is None or (change.tool, change.key) != row.focus or row.tool == GODFATHER:
        return "", "dim"
    return answer_note(row, check_reply(panel, row, argument(change.value)), context, False)


# The lines of the list

def status_lines(panel: Panel, context: Context, found: Section) -> list[Line]:
    def line(label: str, text: str, style: str = "text") -> Line:
        return Line("status", None, ((2, label, "dim"), (2 + STATUS_LABEL, text, style)))

    reply = found.reply or {}
    items = [cast(dict[str, Any], item) for item in cast(list[Any], reply.get("status") or [])
             if isinstance(item, dict)]
    if found.tool != "slotfather":
        return [line(str(item.get("label") or ""), tilde(str(item.get("value") or "")),
                     str(item.get("style") or "text")) for item in items]
    # Slotfather's, as the panel showed it (#7): the capacity its queue read under its lock, from the model drawn
    model = context.model
    where = next((item.get("value") for item in items if item.get("label") == "queue"), None)
    mine = cast(dict[str, Any], reply.get("basis") or {})
    lines = [line("queue", tilde(str(where or mine.get("directory") or "?")))]
    if model:
        lines.append(line("slots", f"{model['slots']} total · {model.get('free', 0)} free · "
                                   f"{model.get('used', 0)} held · {len(model.get('waiters', []))} waiting"))
    else:
        lines.append(line("slots", "queue unavailable", "dim"))
    basis = cast(dict[str, Any], (model or {}).get("basis") or mine)
    if basis.get("mode") in ("fixed", "memory", "lower", "automatic") and "slots" in basis:
        per = number(basis.get("gib_per_slot") or 0)
        how = {"fixed": "saved, fixed count", "memory": f"saved, one per {per} GiB of memory",
               "lower": f"saved, lower of CPU and one per {per} GiB",
               "automatic": "automatic, lower of the two budgets"}[str(basis["mode"])]
        lines.append(line("capacity", f"{basis['slots']} slots · {how}"))
        slots = basis.get("memory_slots") if basis.get("memory_slots") is not None else mine.get("memory_slots")
        memory = basis.get("memory_gib") if basis.get("memory_gib") is not None else mine.get("memory_gib")
        lines.append(line("budgets", f"CPU {basis.get('cpu_slots')} ({basis.get('cores')} cores) · memory "
                                     + (f"{slots}" if slots is not None else "unknown")
                                     + (f" ({number(memory)} GiB)" if memory is not None else "")))
    drain = max(0, int(model.get("used", 0)) - int(model["slots"])) if model else 0
    lines.append(line("draining", f"{drain} slots · nothing starts until at most {model['slots']} are held"
                      if drain and model else "none", "warn" if drain else "dim"))
    environment = next((item for item in items if item.get("label") == "environment"), None)
    if environment and environment.get("value"):
        lines.append(line("environment", str(environment["value"]), "dim"))
    if context.problem:  # the Slotfather snapshot is stale: why, from the source
        lines.append(line("slotfather", context.problem, "bad"))
    return lines


def row_lines(panel: Panel, context: Context, row: Row, value_column: int) -> list[Line]:
    """The row; while it is typed or pending, or refused, a second line says what the value would do, or why not."""
    selected = row.focus == panel.focus and row.editable
    label, room = row.label, value_column - 3
    label = label if len(label) <= room else label[:max(0, room - 1)] + "…"
    parts: list[tuple[int, str, str]] = [(0, "▶" if selected else " ", "cursor"), (2, label, "text")]
    notes: list[tuple[str, str]] = []
    if selected and panel.editing:
        parts.append((value_column, "[" + panel.buffer + "▏]", "cursor"))
        notes.append(typed_note(panel, context, row))
    else:
        value = shown_value(panel, context, row)
        pending = panel.pending is not None and (panel.pending.tool, panel.pending.key) == row.focus
        saving = any((change.tool, change.key) == row.focus for change in panel.saving) and not pending
        text = value_text(row, value)
        if selected and row.type in ("choice", "theme", "boolean"):
            text = f"‹ {text} ›"
        parts.append((value_column, text, "cursor" if pending or saving else "accent" if row.editable else "dim"))
        after = value_column + len(text) + 2
        if pending or saving:
            parts.append((after, "pending" if pending else "saving…", "dim"))
            notes.append(pending_note(panel, context, row))
        elif not row.editable:
            why = row.source if row.source and row.source not in ("saved", "default") else "read-only"
            if row.writable and row.type not in EDITABLE:
                why = "read-only here: needs a newer godfather"
            parts.append((after, why, "dim"))
        elif row.tool == "slotfather" and row.key == "capacity.slots":
            note = drain_note(int(value or 0), context)
            if note:
                parts.append((after, note.replace("will drain", "draining"), "warn"))
        elif "effective" in row.schema and row.schema["effective"] != row.value:
            parts.append((after, f"effective here: {value_text(row, row.schema['effective'])}", "dim"))
    refusal = panel.refusal
    if refusal is not None and (refusal.tool, refusal.key) == row.focus or refusal is not None and selected \
            and refusal.tool == row.tool and row_of(panel, context, (refusal.tool, refusal.key)) is None:
        notes.append(("✗ " + refusal.text, "bad"))  # under its row, or the one that took its place
    found = [Line("row", row.focus if row.editable else None, tuple(parts))]
    found += [Line("note", row.focus if row.editable else None, ((4, text, style),)) for text, style in notes if text]
    return found


def section_title(panel: Panel, tool: str) -> Line:
    title = TITLES.get(tool, tool.upper())
    found = section(panel, tool)
    if tool == GODFATHER:
        note = "· this dashboard only"
    elif found is not None and found.reply is not None and found.reply.get("writable") is False:
        note = "· read-only here"
    elif found is not None and found.reply is not None and found.reply.get("status"):
        note = "· status read-only, live"
    else:
        note = ""
    return Line("title", None, ((0, title, "title"), (len(title) + 1, note, "dim")))


def lines(panel: Panel, context: Context, inner: int = 0) -> list[Line]:
    """Every line of the list, top to bottom: each section's title, its STATUS rows, then its settings; a rule
    between sections."""
    value_column = min(FIELD_LABEL, max(12, inner // 2)) if inner else FIELD_LABEL
    found: list[Line] = []
    for tool in tools(panel):
        if found:
            found.append(Line("rule", None, ()))
        found.append(section_title(panel, tool))
        current = section(panel, tool)
        if current is not None:
            if current.reply is not None:
                reason = current.reply.get("reason")
                if current.reply.get("writable") is False and reason:
                    found.append(Line("problem", None, ((2, str(reason), "dim"),)))
                found.extend(status_lines(panel, context, current))
            if current.problem:
                found.append(Line("problem", None, ((2, current.problem, "dim" if current.reply is None
                                                     else "bad"),)))
            elif current.reply is None:
                found.append(Line("problem", None, ((2, "reading…", "dim"),)))
        for row in rows(panel, context, tool):
            found.extend(row_lines(panel, context, row, value_column))
    return found


# Where it is on the screen: the reducer and the drawing share this

class Geometry(NamedTuple):
    rect: _layout.Rect
    inside: _layout.SettingsBox
    lines: list[Line]
    offset: int  # the first line shown

    @property
    def visible(self) -> int:
        return self.inside.rows.height

    def hits(self) -> list[tuple[Focus, int]]:
        """(row it selects, screen row) for each selectable line shown."""
        shown_lines = self.lines[self.offset:self.offset + self.visible]
        return [(line.focus, self.inside.rows.y + n) for n, line in enumerate(shown_lines)
                if line.focus is not None]


def geometry(panel: Panel, context: Context) -> Geometry:
    inner = _layout.overlay_text(context.width, _layout.SETTINGS_WIDTH, _layout.SETTINGS_INSET)
    found = lines(panel, context, inner)
    rect = _layout.centred(context.width, context.height, _layout.SETTINGS_WIDTH, len(found), _layout.SETTINGS_CHROME)
    inside = _layout.settings(rect)
    return Geometry(rect, inside, found, scrolled(panel, found, inside.rows.height))


def scrolled(panel: Panel, found: Sequence[Line], visible: int) -> int:
    """The first line shown: the panel's offset, moved to show the focused row when `reveal`, kept in range."""
    offset = panel.offset
    if panel.reveal:
        at = [n for n, line in enumerate(found) if line.focus == panel.focus]
        if at:
            offset = min(offset, at[0])
            offset = max(offset, at[-1] - visible + 1)
    return _layout.clamp_scroll(offset, len(found), visible)


def place(panel: Panel, context: Context) -> Panel:
    """The offset the screen shows, kept: a reveal is done once."""
    if not context.width or not context.height:
        return panel
    offset = geometry(panel, context).offset
    if offset == panel.offset and not panel.reveal:
        return panel
    return dataclasses.replace(panel, offset=offset, reveal=False)


def small(panel: Panel, context: Context) -> bool:
    """Too small to show anything but a note to enlarge the terminal: only the ways out work."""
    return context.width > 0 and geometry(panel, context).inside.small


# Transitions

def replace(panel: Panel, **changes: Any) -> Panel:
    return dataclasses.replace(panel, **changes)


def say(panel: Panel, text: str, style: str, now: float) -> Panel:
    return replace(panel, flash=Flash(text, style, now))


def update_section(panel: Panel, tool: str, **changes: Any) -> Panel:
    return replace(panel, sections=tuple(dataclasses.replace(found, **changes) if found.tool == tool else found
                                         for found in panel.sections))


def opened(sections: Sequence[Section], context: Context, now: float) -> Panel:
    """Settings as it opens: the cursor on the first tool's first setting, else this dashboard's first."""
    panel = Panel(tuple(found for found in sections if found.tool in ORDER and found.tool != GODFATHER))
    panel = replace(panel, sections=tuple(dataclasses.replace(found, read_at=now) for found in panel.sections))
    rows_now = selectable(panel, context)
    first = next((row for row in rows_now if row.tool != GODFATHER), rows_now[0] if rows_now else None)
    return replace(panel, focus=first.focus if first else (GODFATHER, "interval"))


def settle_focus(panel: Panel, context: Context) -> Panel:
    """A focused row no longer shown (Slots after a reset to Automatic) gives the cursor to the row of its section
    that took its place (another value of the same `when`), else to its section's first, else to the first; its
    edit and pending change go with it."""
    available = selectable(panel, context)
    keys = [row.focus for row in available]
    if panel.pending is not None and (panel.pending.tool, panel.pending.key) not in keys:
        panel = replace(panel, pending=None)
    if panel.focus in keys or not available:
        return panel
    tool, key = panel.focus
    gone = next((item for found in panel.sections if found.tool == tool
                 for item in schema_rows(found) if item["key"] == key), None)
    when: set[str] = set(cast(dict[str, Any], gone.get("when") or {})) if gone else set()
    same = [row for row in available if row.tool == tool]
    twin = next((row for row in same if when and set(cast(dict[str, Any], row.schema.get("when") or {})) == when),
                None)
    target = twin or (same[0] if same else available[0])
    return replace(panel, focus=target.focus, editing=False, reveal=True)


def wanted_checks(panel: Panel, context: Context) -> tuple[Panel, list[Request]]:
    """`settings check` for the pending and the typed value, once each while the row's revision lasts."""
    wanted: list[tuple[Row, str]] = []
    row = focused(panel, context)
    if panel.editing and row is not None and row.tool != GODFATHER and panel.buffer:
        wanted.append((row, panel.buffer))
    change = panel.pending
    if change is not None and change.tool != GODFATHER:
        found = row_of(panel, context, (change.tool, change.key))
        if found is not None:
            wanted.append((found, argument(change.value)))
    requests: list[Request] = []
    asked = set(panel.asked)
    for found, text in wanted:
        key = check_key(found, text)
        if key in panel.checked or key in asked:
            continue
        asked.add(key)
        requests.append(Request("check", found.tool, found.key, text, found.revision))
    if not requests:
        return panel, []
    return replace(panel, asked=frozenset(asked)), requests


def local(context: Context, key: str, value: Any) -> tuple[Context, list[Request]]:
    if key == "interval":
        context = context._replace(interval=float(value))
    else:
        context = context._replace(theme=_theme.checked_mode(str(value)))
    return context, [Request("local", interval=context.interval, theme=context.theme)]


def save(panel: Panel, context: Context, change: Pending, now: float) -> Result:
    """Saves one change: this dashboard's own at once; a tool's through `settings set`, with the row's revision as
    the change began, its answer later (answered())."""
    panel = replace(panel, pending=None, refusal=None if panel.refusal and (panel.refusal.tool, panel.refusal.key)
                    == (change.tool, change.key) else panel.refusal)
    if change.tool == GODFATHER:
        context, requests = local(context, change.key, change.value)
        row = row_of(panel, context, (change.tool, change.key))
        text = value_text(row, change.value) if row else str(change.value)
        label = row.label if row else change.key
        return say(panel, f"{label} {text} · this dashboard only", "ok", now), context, requests
    found = section(panel, change.tool)
    panel = update_section(panel, change.tool, generation=(found.generation if found else 0) + 1, reading=False)
    panel = replace(panel, saving=(*panel.saving, dataclasses.replace(change, sent=True)))
    return panel, context, [Request("set", change.tool, change.key, argument(change.value), change.revision)]


def commit_pending(panel: Panel, context: Context, now: float) -> Result:
    if panel.pending is None:
        return panel, context, []
    return save(panel, context, panel.pending, now)


def step(panel: Panel, context: Context, direction: int, now: float) -> Result:
    """Shows the next value of the focused row; a tick or Enter saves it."""
    row = focused(panel, context)
    if row is None:
        return panel, context, []
    current = shown_value(panel, context, row)
    value: Any
    if row.type in ("choice", "theme"):
        values = [item.get("value") for item in choices(row)]
        if not values:
            return panel, context, []
        value = values[(values.index(current) + direction) % len(values)] if current in values else values[0]
    elif row.type == "boolean":
        value = not current
    elif row.type == "interval":  # 0.1, 1, 2, 3 ... 60
        value = (1.0 if current < 1 else min(60.0, math.floor(current) + 1.0)) if direction > 0 else \
            (max(1.0, math.ceil(current) - 1.0) if current > 1 else 0.1)
    elif isinstance(current, (int, float)) and not isinstance(current, bool):
        size = row.schema.get("step") or 1
        least, most = row.schema.get("min"), row.schema.get("max")
        value = round(float(current) + direction * float(size), 3)
        if isinstance(least, (int, float)):
            floor = float(least) + (float(size) if row.schema.get("min_exclusive") else 0.0)
            value = max(floor, value)
        if isinstance(most, (int, float)):
            value = min(float(most), value)
        if row.type == "integer":
            value = int(value)
    else:
        return panel, context, []
    if value == current:
        return panel, context, []
    if value == saved_value(panel, context, row.tool, row.key) and not any(
            (change.tool, change.key) == row.focus for change in panel.saving):
        panel = say(replace(panel, pending=None), f"{row.label} back to its saved value", "dim", now)
    else:
        panel = replace(panel, pending=Pending(row.tool, row.key, value, now, row.revision),
                        refusal=None if panel.refusal and (panel.refusal.tool, panel.refusal.key) == row.focus
                        else panel.refusal)
        panel = say(panel, f"{row.label} {value_text(row, value)} · saves in {number(SETTLE_SECONDS)} s · "
                           f"Enter now · Esc cancels", "accent", now)
    return settle_focus(panel, context), context, []


def cancel_pending(panel: Panel, context: Context, now: float) -> Panel:
    if panel.pending is None:
        return panel
    return settle_focus(say(replace(panel, pending=None), "Change cancelled · nothing was saved", "dim", now), context)


def move_to(panel: Panel, context: Context, target: Optional[Focus], now: float, cancel_edit: bool = False) -> Result:
    """The cursor to `target`; a pending change is saved first, an edit cancelled (and said, for a click)."""
    if panel.flash and panel.flash.style == "bad":
        panel = replace(panel, flash=None)  # an earlier refusal belongs to the row it was about
    panel, context, requests = commit_pending(panel, context, now)
    if cancel_edit and panel.editing:
        panel = say(panel, "Edit cancelled · nothing was saved", "dim", now)
    if target is not None:
        panel = replace(panel, focus=target)
    return settle_focus(replace(panel, editing=False, reveal=True), context), context, requests


def move(panel: Panel, context: Context, count: int, now: float) -> Result:
    keys = [row.focus for row in selectable(panel, context)]
    if not keys:
        return panel, context, []
    target = keys[(keys.index(panel.focus) + count) % len(keys)] if panel.focus in keys else keys[0]
    return move_to(panel, context, target, now)


def edge(panel: Panel, context: Context, last: bool, now: float) -> Result:
    keys = [row.focus for row in selectable(panel, context)]
    return move_to(panel, context, (keys[-1] if last else keys[0]) if keys else None, now, cancel_edit=True)


def begin(panel: Panel, context: Context, text: str = "") -> Panel:
    row = focused(panel, context)
    if row is None or row.type not in NUMBERS:
        return panel
    saved = saved_value(panel, context, row.tool, row.key)
    refusal = None if panel.refusal and (panel.refusal.tool, panel.refusal.key) == row.focus else panel.refusal
    return replace(panel, pending=None, editing=True, replace=not text, flash=None, refusal=refusal,
                   buffer=text or (number(saved) if saved is not None else ""))


def commit_typed(panel: Panel, context: Context, now: float) -> tuple[Panel, Context, list[Request], bool]:
    """Saves the typed value; one refused stays in the field, unsaved, with why under the row. The flag: saved."""
    row = focused(panel, context)
    if row is None:
        return replace(panel, editing=False), context, [], False
    value, problem = parse_typed(row, panel.buffer)
    if problem:
        return panel, context, [], False
    if row.tool != GODFATHER:
        reply = check_reply(panel, row, panel.buffer)
        if reply is not None and not reply.get("ok"):
            return panel, context, [], False
    panel = replace(panel, editing=False)
    if row.tool != GODFATHER:  # what the tool gets is what was typed: 30 stays 30, not 30.0
        value = int(value) if isinstance(value, float) and value.is_integer() and "." not in panel.buffer else value
    panel, context, requests = save(panel, context, Pending(row.tool, row.key, value, now, row.revision), now)
    return panel, context, requests, True


def reset_row(panel: Panel, context: Context, now: float) -> Result:
    row = focused(panel, context)
    panel = replace(panel, pending=None)
    if row is None:
        return panel, context, []
    if row.tool == GODFATHER:
        default = DEFAULT_INTERVAL if row.key == "interval" else _theme.DEFAULT_MODE
        return save(panel, context, Pending(row.tool, row.key, default, now), now)
    found = section(panel, row.tool)
    panel = update_section(panel, row.tool, generation=(found.generation if found else 0) + 1, reading=False)
    panel = replace(panel, refusal=None)
    return panel, context, [Request("reset", row.tool, row.key, None, row.revision)]


def reset_section(panel: Panel, context: Context, now: float) -> Result:
    """`D` twice: the selected row's section, every setting at its default."""
    tool = panel.focus[0]
    panel = replace(panel, pending=None, armed=None, refusal=None)
    if tool == GODFATHER:
        context = context._replace(interval=DEFAULT_INTERVAL, theme=_theme.DEFAULT_MODE)
        panel = say(panel, "Saved · this dashboard's settings at their defaults", "ok", now)
        return panel, context, [Request("local", interval=context.interval, theme=context.theme)]
    found = section(panel, tool)
    panel = update_section(panel, tool, generation=(found.generation if found else 0) + 1, reading=False)
    return panel, context, [Request("reset", tool, None)]


def arm(panel: Panel, now: float) -> Panel:
    tool = panel.focus[0]
    scope = "this dashboard's settings" if tool == GODFATHER else f"every {NAMES.get(tool, tool)} setting"
    return say(replace(panel, pending=None, armed=now), f"Press D again to reset {scope} to its default"
               if tool != GODFATHER else f"Press D again to reset {scope} to their defaults", "warn", now)


def scroll(panel: Panel, context: Context, rows_by: int) -> Panel:
    view = geometry(panel, context)
    return replace(panel, offset=_layout.clamp_scroll(view.offset + rows_by, len(view.lines), view.visible),
                   reveal=False)


def page(panel: Panel, context: Context, direction: int) -> Panel:
    return scroll(panel, context, direction * max(1, geometry(panel, context).visible - 1))


# Answers from the tools

def basis_of(reply: Optional[dict[str, Any]]) -> dict[str, Any]:
    return cast(dict[str, Any], (reply or {}).get("basis") or {})


def saved_text(row: Optional[Row], panel: Panel, context: Context, key: str) -> str:
    if row is None:
        return f"Saved · {key}"
    if row.tool == "slotfather" and row.key in CAPACITY:
        found = section(panel, "slotfather")
        basis = basis_of(found.reply if found else None)
        return f"Saved · capacity {basis.get('slots', '?')} slots"
    return f"Saved · {row.label} {value_text(row, saved_value(panel, context, row.tool, row.key))}"


def conflict_text(panel: Panel, context: Context, tool: str, key: str, ending: str) -> str:
    if tool == "slotfather" and key in CAPACITY:
        found = section(panel, tool)
        basis = basis_of(found.reply if found else None)
        return f"Capacity changed elsewhere: now {basis.get('slots', '?')} slots · {ending}"
    row = row_of(panel, context, (tool, key))
    now_text = value_text(row, saved_value(panel, context, tool, key)) if row else "?"
    return f"Changed by another client: now {now_text} · {ending}"


def take_reply(panel: Panel, context: Context, tool: str, reply: dict[str, Any]) -> Panel:
    """A tool's current settings, from a read or from what set and reset print: a pending change chosen under an
    older revision of its row is a conflict, dropped and said under the row."""
    panel = update_section(panel, tool, reply=reply, problem=None)
    change = panel.pending
    found = section(panel, tool)
    if change is not None and change.tool == tool and found is not None:
        item = next((item for item in schema_rows(found) if item["key"] == change.key), None)
        revision = item.get("revision") if item else None
        if item is None or (str(revision) if revision is not None else None) != change.revision:
            panel = replace(panel, pending=None, refusal=Refusal(tool, change.key, conflict_text(
                panel, context, tool, change.key, "your change was not saved")))
    refusal = panel.refusal
    if refusal is not None and refusal.tool == tool:  # a row now read-only shows why itself
        row = row_of(panel, context, (refusal.tool, refusal.key))
        if row is not None and not row.editable:
            panel = replace(panel, refusal=None)
    return settle_focus(panel, context)


def read_done(panel: Panel, context: Context, tool: str, generation: int, reply: Optional[dict[str, Any]],
              problem: Optional[str], now: float) -> Panel:
    found = section(panel, tool)
    if found is None:
        return panel
    panel = update_section(panel, tool, reading=False, read_at=now)
    if generation != found.generation:  # a write since it started: its answer is newer
        return panel
    if problem is not None or reply is None:
        return update_section(panel, tool, problem=problem or f"{tool} settings cannot be read")
    return take_reply(panel, context, tool, reply)


def check_done(panel: Panel, tool: str, key: str, text: str, revision: Optional[str],
               reply: dict[str, Any]) -> Panel:
    ident = (tool, key, text, revision or "")
    return replace(panel, checked={**panel.checked, ident: reply}, asked=panel.asked - {ident})


def write_done(panel: Panel, context: Context, request: Request, outcome: str, reply: Optional[dict[str, Any]],
               message: str, now: float) -> tuple[Panel, list[Request]]:
    """A set or reset answered: `outcome` is ok, conflict, read-only, invalid or failed; `reply` the tool's
    settings when it printed them. A refusal shows under its row (a whole-section reset's in the notes); the
    settings are read again unless the answer carried them."""
    tool, key = request.tool, request.key
    first = next((n for n, change in enumerate(panel.saving) if (change.tool, change.key) == (tool, key)), None)
    saving = panel.saving if first is None else panel.saving[:first] + panel.saving[first + 1:]
    panel = replace(panel, saving=saving)
    if reply is not None:
        panel = take_reply(panel, context, tool, reply)
    requests: list[Request] = []
    found = section(panel, tool)
    if reply is None and found is not None:
        requests.append(Request("read", tool, generation=found.generation))
        panel = update_section(panel, tool, reading=True, read_at=now)
    if outcome == "ok":
        if key is None:
            basis = basis_of(reply)
            text = f"Saved · every {NAMES.get(tool, tool)} setting at its default" + (
                f", capacity {basis['slots']} slots" if tool == "slotfather" and "slots" in basis else "")
        else:
            text = saved_text(row_of(panel, context, (tool, key)), panel, context, key)
        panel = say(panel, text, "ok", now)
        requests.append(Request("changed", tool))
    elif key is None:
        panel = say(panel, message or "nothing was saved", "bad", now)
    elif outcome == "conflict":
        panel = replace(panel, refusal=Refusal(tool, key, conflict_text(panel, context, tool, key,
                                                                        "nothing was saved")))
    else:
        panel = replace(panel, refusal=Refusal(tool, key, message or "nothing was saved"))
    return settle_focus(panel, context), requests


def due_reads(panel: Panel, now: float) -> tuple[Panel, list[Request]]:
    """While Settings is open each tool's settings are read again: Slotfather's every second, the others' every
    two; never under a value being typed, nor while a read or a save of that tool is under way."""
    if panel.editing:
        return panel, []
    requests: list[Request] = []
    for found in panel.sections:
        every = READ_EVERY.get(found.tool, READ_OTHERS)
        busy = found.reading or any(change.tool == found.tool for change in panel.saving)
        if found.readable and not busy and now - found.read_at >= every:
            panel = update_section(panel, found.tool, reading=True, read_at=now)
            requests.append(Request("read", found.tool, generation=found.generation))
    return panel, requests


def tick(panel: Panel, context: Context, now: float) -> Result:
    """The loop's pass: a change that settled is saved, a flash that is no longer news goes, reads come due."""
    requests: list[Request] = []
    if panel.pending is not None and now - panel.pending.at >= SETTLE_SECONDS:
        panel, context, requests = commit_pending(panel, context, now)
    flash = panel.flash
    if flash is not None and now - flash.at >= FLASH_SECONDS and flash.style != "bad" \
            and not (flash.style == "accent" and panel.pending is not None):
        panel = replace(panel, flash=None)
    panel, reads = due_reads(panel, now)
    return panel, context, requests + reads


def notes(panel: Panel, context: Context) -> list[tuple[str, str]]:
    """Under the rule: the focused row's description, and what the last change did while it is news (the flash)."""
    row = focused(panel, context)
    found = [(row.description, "dim")] if row is not None and row.description else []
    flash = panel.flash
    if flash is not None:
        found = found[:1] + [(_FLASH_MARKS.get(flash.style, "") + flash.text, flash.style)]
    return found


_FLASH_MARKS = {"ok": "✓ ", "warn": "! ", "bad": "✗ ", "accent": "… "}


def footer(panel: Panel) -> list[tuple[str, str]]:
    """The panel's short key line, centred like the dashboard's footer."""
    if panel.editing:
        return [("enter", "save"), ("esc", "cancel")]
    if panel.pending:
        return [("enter", "save now"), ("esc", "cancel")]
    return [("↑↓", "select"), ("←→", "change"), ("enter", "edit"), ("d", "reset"), ("esc", "close")]
