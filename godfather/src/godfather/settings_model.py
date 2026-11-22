"""Settings (fathers#87, fathers#233) as a pure state machine: one section per present tool, read with
`<tool> settings --json` and written with `settings set` and `settings reset` (SPEC.md, Settings contract), after this
dashboard's own section (Refresh every, Colour theme). The panel is a frozen value with explicit `EditState`,
`Pending`, `ArmedReset`, and per-tool serialized `WriteOp` queues; every transition returns the new panel and the
requests the app carries out, with no layout, geometry or canvas imports."""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, NamedTuple, Optional, Sequence, cast

from . import settings_schema as _schema
from . import theme as _theme
from .settings_schema import (
    CAPACITY,
    DEFAULT_INTERVAL,
    EDITABLE,
    GODFATHER,
    LOCAL_DESCRIPTIONS,
    NAMES,
    NUMBERS,
    ORDER,
    TITLES,
    CheckKey,
    Focus,
    Row,
    Section,
    argument,
    basis_of,
    choices,
    number,
    parse_typed,
    schema_rows,
    tool_row,
    value_text,
)

__all__ = [
    "CAPACITY",
    "DEFAULT_INTERVAL",
    "EDITABLE",
    "FLASH_SECONDS",
    "GODFATHER",
    "LOCAL_DESCRIPTIONS",
    "MAX_CHECKED",
    "NAMES",
    "NUMBERS",
    "ORDER",
    "READ_EVERY",
    "READ_OTHERS",
    "SETTLE_SECONDS",
    "TITLES",
    "ArmedReset",
    "CheckKey",
    "Context",
    "EditState",
    "Flash",
    "Focus",
    "Panel",
    "Pending",
    "Refusal",
    "Request",
    "Result",
    "Row",
    "Section",
    "WriteOp",
    "all_rows",
    "argument",
    "arm",
    "arm_or_reset",
    "basis_of",
    "begin",
    "cancel_pending",
    "check_done",
    "check_key",
    "check_reply",
    "choices",
    "commit_pending",
    "commit_typed",
    "disarm",
    "due_reads",
    "edge",
    "edit_cancel",
    "edit_key",
    "enqueue_or_send",
    "focused",
    "local",
    "move",
    "move_to",
    "number",
    "opened",
    "parse_typed",
    "read_done",
    "replace",
    "reset_row",
    "reset_section",
    "row_of",
    "rows",
    "save",
    "saved_value",
    "say",
    "schema_rows",
    "section",
    "sections_read",
    "selectable",
    "send_write",
    "settle_focus",
    "shown",
    "shown_value",
    "step",
    "take_reply",
    "tick",
    "tool_row",
    "tools",
    "update_section",
    "value_text",
    "wanted_checks",
    "write_done",
]

SETTLE_SECONDS = 1.0  # quiet after an arrow change before it is saved
FLASH_SECONDS = 4.0  # how long a confirmation shows, and how long a first D stays armed
READ_EVERY = {"slotfather": 1.0}  # seconds between reads of a tool's settings while Settings is open
READ_OTHERS = 2.0
MAX_CHECKED = 128  # upper bound on cached `settings check` answers


# Explicit state values

@dataclass(frozen=True)
class Pending:
    """An uncommitted arrow or choice change: saved at `at` + SETTLE_SECONDS, at once on Enter, or first when moving
    on. `revision` is the row's as the change began; `confirmed` waits for an in-flight `settings check`."""
    tool: str
    key: str
    value: Any
    at: float
    revision: Optional[str] = None
    confirmed: bool = False


@dataclass(frozen=True)
class EditState:
    """A numeric setting being typed. Dropping `EditState` or editing `buffer` clears `waiting` and `move_next` so an
    earlier Enter or Tab never auto-commits later edits."""
    buffer: str
    replace: bool = True
    waiting: bool = False  # Enter or Tab pressed while `settings check` has not answered yet
    move_next: bool = False  # Tab pressed: advance focus once the check answers ok and saves


@dataclass(frozen=True)
class ArmedReset:
    """After pressing `D` once in `tool`'s section: a second `D` within FLASH_SECONDS resets that section only."""
    tool: str
    at: float


@dataclass(frozen=True)
class WriteOp:
    """One mutating tool CLI operation (`set` or `reset`), either in flight (`saving`) or queued behind an in-flight
    write of the same tool (`queued`) so `set`, row `reset` and section `reset` serialize per tool."""
    kind: Literal["set", "reset"]
    tool: str
    key: Optional[str]
    value: Any = None
    revision: Optional[str] = None

    def to_request(self) -> Request:
        if self.kind == "set":
            return Request("set", self.tool, self.key, _schema.argument(self.value), self.revision)
        return Request("reset", self.tool, self.key, None, self.revision)


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


@dataclass(frozen=True)
class Panel:
    sections: tuple[Section, ...] = ()
    focus: Focus = ("", "")
    edit: Optional[EditState] = None
    pending: Optional[Pending] = None
    armed: Optional[ArmedReset] = None
    saving: tuple[WriteOp, ...] = ()  # writes sent, at most one per tool in flight
    queued: tuple[WriteOp, ...] = ()  # writes made while one of the same tool is in flight: sent after its answer
    flash: Optional[Flash] = None
    refusal: Optional[Refusal] = None
    loading: bool = False  # the tools' sections are being read: the cursor goes to the first tool's when they come
    offset: int = 0
    reveal: bool = True  # the next placement scrolls the focused row into view
    checked: Mapping[CheckKey, dict[str, Any]] = field(default_factory=lambda: cast(dict[CheckKey, Any], {}))
    asked: frozenset[CheckKey] = frozenset()
    kind: str = "settings"

    def __post_init__(self) -> None:
        if self.edit is not None and self.pending is not None:
            raise ValueError("Panel cannot have both edit and pending")
        if self.edit is not None and self.armed is not None:
            raise ValueError("Panel cannot have both edit and armed")
        if self.kind != "settings":
            raise ValueError(f"invalid Panel kind: {self.kind!r}")

    @property
    def editing(self) -> bool:
        return self.edit is not None

    @property
    def buffer(self) -> str:
        return self.edit.buffer if self.edit is not None else ""


class Context(NamedTuple):
    """What a transition reads but does not own: this dashboard's own settings, the model drawn (held and waiting
    slots, the capacity the queue read), the refresh error, and the screen's size."""
    interval: float = DEFAULT_INTERVAL
    theme: str = _theme.DEFAULT_MODE
    model: Optional[Mapping[str, Any]] = None
    problem: Optional[str] = None
    width: int = 0
    height: int = 0


@dataclass(frozen=True)
class Request:
    """What the app carries out for a transition: open · read · check · set · reset (a tool's settings CLI), local
    (this dashboard's own settings changed), changed (a tool saved: the model reloads), close, quit."""
    kind: str
    tool: str = ""
    key: Optional[str] = None
    value: Optional[str] = None
    revision: Optional[str] = None
    generation: int = 0
    interval: float = DEFAULT_INTERVAL
    theme: str = _theme.DEFAULT_MODE


Result = tuple[Panel, Context, list[Request]]


# Row queries over (Panel, Context)

def section(panel: Panel, tool: str) -> Optional[Section]:
    return _schema.section(panel.sections, tool)


def shown(panel: Panel, tool: str, key: str, saved: Any) -> Any:
    """A row's value as shown: pending, else queued/saving `set`, else saved (a newer queued/saving `reset` stops
    falling back to an older `set`)."""
    if panel.pending is not None and (panel.pending.tool, panel.pending.key) == (tool, key):
        return panel.pending.value
    for op in (*reversed(panel.queued), *reversed(panel.saving)):
        if op.tool != tool:
            continue
        if op.kind == "set" and op.key == key:
            return op.value
        if op.kind == "reset" and op.key in (key, None):
            return saved
    return saved


def rows(panel: Panel, context: Context, tool: str) -> list[Row]:
    return _schema.visible_rows(
        panel.sections,
        context.interval,
        context.theme,
        tool,
        shown_override=lambda row_tool, row_key, value: shown(panel, row_tool, row_key, value),
    )


def tools(panel: Panel) -> list[str]:
    return _schema.tools(panel.sections)


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
    return _schema.saved_value(panel.sections, context.interval, context.theme, tool, key)


def shown_value(panel: Panel, context: Context, row: Row) -> Any:
    return shown(panel, row.tool, row.key, saved_value(panel, context, row.tool, row.key))


def check_key(row: Row, text: str) -> CheckKey:
    return row.tool, row.key, text, row.revision or ""


def check_reply(panel: Panel, row: Row, text: str) -> Optional[dict[str, Any]]:
    return panel.checked.get(check_key(row, text))


# Transitions (single owner of Panel state)

def replace(panel: Panel, **changes: Any) -> Panel:
    return dataclasses.replace(panel, **changes)


def say(panel: Panel, text: str, style: str, now: float) -> Panel:
    return replace(panel, flash=Flash(text, style, now))


def disarm(panel: Panel) -> Panel:
    """Withdraws an armed `D` section-reset prompt and its warning flash."""
    if panel.armed is None:
        return panel
    flash = None if panel.flash is not None and panel.flash.style == "warn" else panel.flash
    return replace(panel, armed=None, flash=flash)


def update_section(panel: Panel, tool: str, **changes: Any) -> Panel:
    return replace(panel, sections=tuple(dataclasses.replace(found, **changes) if found.tool == tool else found
                                         for found in panel.sections))


def opened(sections: Sequence[Section], context: Context, now: float, loading: bool = False) -> Panel:
    """Settings as it opens: the cursor on the first tool's first setting, else this dashboard's first. `loading`:
    it opens at once, the tools' sections still being read (sections_read() brings them)."""
    if loading:
        return Panel(loading=True, focus=(GODFATHER, "interval"))
    panel = Panel(tuple(found for found in sections if found.tool in ORDER and found.tool != GODFATHER))
    panel = replace(panel, sections=tuple(dataclasses.replace(found, read_at=now) for found in panel.sections))
    rows_now = selectable(panel, context)
    first = next((row for row in rows_now if row.tool != GODFATHER), rows_now[0] if rows_now else None)
    return replace(panel, focus=first.focus if first else (GODFATHER, "interval"))


def sections_read(panel: Panel, context: Context, sections: Sequence[Section], now: float) -> Panel:
    """The tools' sections, read after Settings opened: the cursor goes to the first tool's first row unless it was
    moved meanwhile."""
    found = opened(sections, context, now)
    untouched = panel.focus == (GODFATHER, "interval") and panel.pending is None and panel.edit is None
    return replace(panel, sections=found.sections, loading=False, focus=found.focus if untouched else panel.focus,
                   reveal=True)


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
    return replace(panel, focus=target.focus, edit=None, reveal=True)


def wanted_checks(panel: Panel, context: Context) -> tuple[Panel, list[Request]]:
    """`settings check` for the pending and the typed value, once each while the row's revision lasts."""
    wanted: list[tuple[Row, str]] = []
    row = focused(panel, context)
    if panel.edit is not None and row is not None and row.tool != GODFATHER and panel.edit.buffer:
        wanted.append((row, panel.edit.buffer))
    change = panel.pending
    if change is not None and change.tool != GODFATHER:
        found = row_of(panel, context, (change.tool, change.key))
        if found is not None:
            wanted.append((found, _schema.argument(change.value)))
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


def send_write(panel: Panel, context: Context, op: WriteOp) -> Result:
    found = section(panel, op.tool)
    panel = update_section(panel, op.tool, generation=(found.generation if found else 0) + 1, reading=False)
    panel = replace(panel, saving=(*panel.saving, op))
    return panel, context, [op.to_request()]


def enqueue_or_send(panel: Panel, context: Context, op: WriteOp) -> Result:
    """Serializes all writes (`set`, row `reset`, section `reset`) per tool so concurrent writes never race."""
    if any(sent.tool == op.tool for sent in panel.saving):
        prior = (
            tuple(item for item in panel.queued if item.tool != op.tool)
            if op.kind == "reset" and op.key is None
            else panel.queued
        )
        return replace(panel, queued=(*prior, op)), context, []
    return send_write(panel, context, op)


def save(panel: Panel, context: Context, change: Pending, now: float) -> Result:
    """Saves one change: this dashboard's own at once; a tool's through `settings set`, with the row's revision as
    the change began, serialized per tool."""
    panel = replace(panel, pending=None, refusal=None if panel.refusal and (panel.refusal.tool, panel.refusal.key)
                    == (change.tool, change.key) else panel.refusal)
    if change.tool == GODFATHER:
        context, requests = local(context, change.key, change.value)
        row = row_of(panel, context, (change.tool, change.key))
        text = value_text(row, change.value) if row else str(change.value)
        label = row.label if row else change.key
        return say(panel, f"{label} {text} · this dashboard only", "ok", now), context, requests
    return enqueue_or_send(panel, context, WriteOp("set", change.tool, change.key, change.value, change.revision))


def commit_pending(panel: Panel, context: Context, now: float, leaving: bool = False) -> Result:
    """Saves the pending change once its check said yes (fathers#87 review): a refused value is never sent (moving
    on drops it, its reason under its row), and one whose check has not answered is saved when it answers."""
    change = panel.pending
    if change is None:
        return panel, context, []
    if change.tool == GODFATHER:
        return save(panel, context, change, now)
    row = row_of(panel, context, (change.tool, change.key))
    if row is None:
        return replace(panel, pending=None), context, []
    reply = check_reply(panel, row, _schema.argument(change.value))
    if reply is None:
        return replace(panel, pending=dataclasses.replace(change, confirmed=True)), context, []
    if not reply.get("ok"):
        if leaving:
            why = str(reply.get("error") or "Not accepted.") + " Nothing was saved."
            return replace(panel, pending=None, refusal=Refusal(change.tool, change.key, why)), context, []
        return panel, context, []
    return save(panel, context, change, now)


def step(panel: Panel, context: Context, direction: int, now: float) -> Result:
    """Shows the next value of the focused row; a tick or Enter saves it."""
    panel = disarm(panel)
    row = focused(panel, context)
    if row is None:
        return panel, context, []
    current = shown_value(panel, context, row)
    value, changed = _schema.step_value(row, current, direction)
    if not changed:
        return panel, context, []
    in_flight = any(op.tool == row.tool and op.key in (row.key, None) for op in (*panel.saving, *panel.queued))
    if value == saved_value(panel, context, row.tool, row.key) and not in_flight:
        panel = say(replace(panel, edit=None, pending=None), f"{row.label} back to its saved value", "dim", now)
    else:
        panel = replace(panel, edit=None, pending=Pending(row.tool, row.key, value, now, row.revision),
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
    panel = disarm(panel)  # a reset question belongs to the row it was asked on
    panel, context, requests = commit_pending(panel, context, now, leaving=True)
    if cancel_edit and panel.edit is not None:
        panel = say(panel, "Edit cancelled · nothing was saved", "dim", now)
    if target is not None:
        panel = replace(panel, focus=target)
    return settle_focus(replace(panel, edit=None, reveal=True), context), context, requests


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
    buffer = text or (number(saved) if saved is not None else "")
    return replace(panel, edit=EditState(buffer, replace=not text), pending=None, armed=None, flash=None,
                   refusal=refusal)


def edit_key(panel: Panel, key: str) -> Panel:
    """Updates the active `EditState` buffer and resets `waiting` and `move_next` whenever the text changes."""
    edit = panel.edit
    if edit is None:
        return panel
    if key in ("\x7f", "\b"):
        text = "" if edit.replace else edit.buffer[:-1]
    elif key == "\x15":  # Ctrl-U clears the field
        text = ""
    elif len(edit.buffer) < 16 or edit.replace:
        text = key if edit.replace else edit.buffer + key
    else:
        return panel
    return replace(panel, edit=EditState(text, replace=False))


def edit_cancel(panel: Panel, now: float) -> Panel:
    if panel.edit is None:
        return panel
    return say(replace(panel, edit=None), "Edit cancelled · nothing was saved", "dim", now)


def commit_typed(panel: Panel, context: Context, now: float, move_next: bool = False) -> Result:
    """Saves the typed value; one refused stays in the field, unsaved, with why under the row. When `move_next` is
    True (Tab), focus advances once the value saves (immediately or when its `settings check` answers)."""
    edit = panel.edit
    if edit is None:
        return panel, context, []
    row = focused(panel, context)
    if row is None:
        return replace(panel, edit=None), context, []
    value, problem = parse_typed(row, edit.buffer)
    if problem:
        return replace(panel, edit=dataclasses.replace(edit, waiting=False, move_next=False)), context, []
    if row.tool != GODFATHER:
        reply = check_reply(panel, row, edit.buffer)
        if reply is None:  # its check has not answered: saved when it says yes
            return replace(panel, edit=dataclasses.replace(edit, waiting=True, move_next=move_next)), context, []
        if not reply.get("ok"):
            return replace(panel, edit=dataclasses.replace(edit, waiting=False, move_next=False)), context, []
    panel = replace(panel, edit=None)
    if row.tool != GODFATHER:  # what the tool gets is what was typed: 30 stays 30, not 30.0
        value = int(value) if isinstance(value, float) and value.is_integer() and "." not in edit.buffer else value
    panel, context, requests = save(panel, context, Pending(row.tool, row.key, value, now, row.revision), now)
    if move_next:
        panel, context, more = move(panel, context, 1, now)
        requests = requests + more
    return panel, context, requests


def reset_row(panel: Panel, context: Context, now: float) -> Result:
    row = focused(panel, context)
    panel = replace(disarm(panel), edit=None, pending=None)
    if row is None:
        return panel, context, []
    if row.tool == GODFATHER:
        default: Any = DEFAULT_INTERVAL if row.key == "interval" else _theme.DEFAULT_MODE
        return save(panel, context, Pending(row.tool, row.key, default, now), now)
    panel = replace(panel, refusal=None)
    return enqueue_or_send(panel, context, WriteOp("reset", row.tool, row.key, revision=row.revision))


def reset_section(panel: Panel, context: Context, now: float) -> Result:
    """`D` twice: the selected row's section, every setting at its default."""
    tool = panel.focus[0]
    panel = replace(panel, edit=None, pending=None, armed=None, refusal=None)
    if tool == GODFATHER:
        context = context._replace(interval=DEFAULT_INTERVAL, theme=_theme.DEFAULT_MODE)
        panel = say(panel, "Saved · this dashboard's settings at their defaults", "ok", now)
        return panel, context, [Request("local", interval=context.interval, theme=context.theme)]
    found = section(panel, tool)
    if found is None or not found.readable or (found.reply is not None and found.reply.get("writable") is False):
        return panel, context, []
    return enqueue_or_send(panel, context, WriteOp("reset", tool, None))


def arm(panel: Panel, now: float) -> Panel:
    tool = panel.focus[0]
    scope = "this dashboard's settings" if tool == GODFATHER else f"every {NAMES.get(tool, tool)} setting"
    prompt = (
        f"Press D again to reset {scope} to its default"
        if tool != GODFATHER
        else f"Press D again to reset {scope} to their defaults"
    )
    return say(replace(panel, edit=None, pending=None, armed=ArmedReset(tool, now)), prompt, "warn", now)


def arm_or_reset(panel: Panel, context: Context, now: float) -> Result:
    """`D`: once, it asks; again within FLASH_SECONDS in the same section, that section is reset."""
    armed = panel.armed
    if armed is not None and armed.tool == panel.focus[0] and now - armed.at < FLASH_SECONDS:
        return reset_section(panel, context, now)
    return arm(panel, now), context, []


# Answers from the tools

def take_reply(panel: Panel, context: Context, tool: str, reply: dict[str, Any], own: bool = False) -> Panel:
    """A tool's current settings, from a read or from what set and reset print: stale cached checks are evicted, a
    pending change chosen under an older revision of its row is a conflict (dropped and said under the row), while
    changes made on top of this panel's own save (`own`) rebase to the revision it gave."""
    panel = update_section(panel, tool, reply=reply, problem=None)
    revisions = _schema.revisions_of(reply)

    def fresh_check(ident: CheckKey) -> bool:
        if ident[0] != tool:
            return True
        if own and ident[3] == "":
            return False
        return revisions.get(ident[1]) == ident[3]

    checked = {ident: answer for ident, answer in panel.checked.items() if fresh_check(ident)}
    asked = frozenset(ident for ident in panel.asked if fresh_check(ident))
    if len(checked) != len(panel.checked) or asked != panel.asked:
        panel = replace(panel, checked=checked, asked=asked)

    if own:
        def rebased_pending(change: Pending) -> Pending:
            return dataclasses.replace(change, revision=_schema.revision_of(panel.sections, change.tool, change.key)) \
                if change.tool == tool else change

        def rebased_op(op: WriteOp) -> WriteOp:
            if op.tool != tool or op.key is None or op.key not in revisions:
                return op
            return dataclasses.replace(op, revision=_schema.revision_of(panel.sections, op.tool, op.key))

        panel = replace(
            panel,
            pending=rebased_pending(panel.pending) if panel.pending is not None else None,
            queued=tuple(rebased_op(op) for op in panel.queued),
        )
    change = panel.pending
    found = section(panel, tool)
    if change is not None and change.tool == tool and found is not None:
        item = next((item for item in schema_rows(found) if item["key"] == change.key), None)
        revision = item.get("revision") if item else None
        if item is None or (str(revision) if revision is not None else None) != change.revision:
            row = row_of(panel, context, (tool, change.key))
            panel = replace(
                panel,
                pending=None,
                refusal=Refusal(
                    tool,
                    change.key,
                    _schema.conflict_text(
                        row, panel.sections, context.interval, context.theme, tool, change.key,
                        "your change was not saved",
                    ),
                ),
            )
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


def check_done(panel: Panel, context: Context, tool: str, key: str, text: str, revision: Optional[str],
               reply: dict[str, Any], now: float) -> Result:
    """A check answered; a change waiting on it (Enter, Tab, the settle or moving on came first) is saved if it said
    yes, and dropped with why if not."""
    ident = (tool, key, text, revision or "")
    found = section(panel, tool)
    current_rev = _schema.revision_of(panel.sections, tool, key) or ""
    asked = panel.asked - {ident}
    if found is None or found.reply is None or ident[3] == current_rev:
        checked = {k: v for k, v in panel.checked.items() if k != ident}
        checked[ident] = reply
        while len(checked) > MAX_CHECKED:
            checked.pop(next(iter(checked)))
        panel = replace(panel, checked=checked, asked=asked)
    elif asked != panel.asked:
        panel = replace(panel, asked=asked)

    change = panel.pending
    if change is not None and change.confirmed and (change.tool, change.key, _schema.argument(change.value)) == (
            tool, key, text):
        return commit_pending(panel, context, now, leaving=(change.tool, change.key) != panel.focus)
    edit = panel.edit
    row = focused(panel, context)
    if edit is not None and edit.waiting and row is not None and (row.tool, row.key, edit.buffer) == (
            tool, key, text):
        return commit_typed(panel, context, now, move_next=edit.move_next)
    return panel, context, []


def write_done(panel: Panel, context: Context, request: Request, outcome: str, reply: Optional[dict[str, Any]],
               message: str, now: float) -> tuple[Panel, list[Request]]:
    """A set or reset answered: `outcome` is ok, conflict, read-only, refused or failed; `reply` the tool's settings
    when it printed them. A refusal shows under its row (a whole-section reset's in the notes); the settings are read
    again unless the answer carried them."""
    tool, key = request.tool, request.key
    first = next(
        (
            n for n, op in enumerate(panel.saving)
            if (op.tool, op.key) == (tool, key) and (request.kind not in ("set", "reset") or op.kind == request.kind)
        ),
        None,
    )
    saving = panel.saving if first is None else panel.saving[:first] + panel.saving[first + 1:]
    panel = replace(panel, saving=saving)
    if reply is not None:
        panel = take_reply(panel, context, tool, reply, own=outcome == "ok")
    requests: list[Request] = []
    waiting = [op for op in panel.queued if op.tool == tool]
    if waiting and outcome == "ok" and not any(op.tool == tool for op in panel.saving):
        panel = replace(panel, queued=tuple(op for op in panel.queued if op is not waiting[0]))
        panel, context, sent = send_write(panel, context, waiting[0])
        requests += sent
    elif waiting and outcome != "ok":  # made on top of a change that was not saved: not saved either
        panel = replace(panel, queued=tuple(op for op in panel.queued if op.tool != tool))
    found = section(panel, tool)
    if reply is None and found is not None and not any(op.tool == tool for op in panel.saving):
        requests.append(Request("read", tool, generation=found.generation))
        panel = update_section(panel, tool, reading=True, read_at=now)
    if outcome == "ok":
        if key is None:
            basis = basis_of(reply)
            text = f"Saved · every {NAMES.get(tool, tool)} setting at its default" + (
                f", capacity {basis['slots']} slots" if tool == "slotfather" and "slots" in basis else "")
        else:
            text = _schema.saved_text(row_of(panel, context, (tool, key)), panel.sections, context.interval,
                                      context.theme, key)
        panel = say(panel, text, "ok", now)
        requests.append(Request("changed", tool))
    elif key is None:
        panel = say(panel, message or "nothing was saved", "bad", now)
    elif outcome == "conflict":
        row = row_of(panel, context, (tool, key))
        panel = replace(panel, refusal=Refusal(
            tool, key, _schema.conflict_text(row, panel.sections, context.interval, context.theme, tool, key,
                                             "nothing was saved")))
    else:
        panel = replace(panel, refusal=Refusal(tool, key, message or "nothing was saved"))
    return settle_focus(panel, context), requests


def due_reads(panel: Panel, now: float) -> tuple[Panel, list[Request]]:
    """While Settings is open each tool's settings are read again: Slotfather's every second, the others' every two;
    never under a value being typed, nor while a read, save or reset of that tool is under way."""
    editing = panel.focus[0] if panel.edit is not None else ""  # only the section being typed in waits
    requests: list[Request] = []
    for found in panel.sections:
        if found.tool == editing:
            continue
        every = READ_EVERY.get(found.tool, READ_OTHERS)
        busy = found.reading or any(op.tool == found.tool for op in (*panel.saving, *panel.queued))
        if found.readable and not busy and now - found.read_at >= every:
            panel = update_section(panel, found.tool, reading=True, read_at=now)
            requests.append(Request("read", found.tool, generation=found.generation))
    return panel, requests


def tick(panel: Panel, context: Context, now: float) -> Result:
    """The loop's pass: a change that settled is saved, an old flash or armed reset goes, reads come due."""
    requests: list[Request] = []
    if panel.pending is not None and now - panel.pending.at >= SETTLE_SECONDS:
        panel, context, requests = commit_pending(panel, context, now)
    if panel.armed is not None and now - panel.armed.at >= FLASH_SECONDS:
        panel = replace(panel, armed=None)
    flash = panel.flash
    if flash is not None and now - flash.at >= FLASH_SECONDS and flash.style != "bad" \
            and not (flash.style == "accent" and panel.pending is not None):
        panel = replace(panel, flash=None)
    panel, reads = due_reads(panel, now)
    return panel, context, requests + reads
