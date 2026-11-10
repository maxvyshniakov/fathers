"""The dashboard's Settings panel (#7): a read-only status of the queue above the editable settings.

Every change is saved at once through the CLI's state boundary (state.configure(): validation, ownership and the
queue lock) and applies at the queue's next grant, busy or not; nothing is drafted. A capacity below what running
leases hold drains: they keep their slots and nothing more starts until enough of them end. The refresh interval
belongs to this dashboard alone."""
from __future__ import annotations

import contextlib
import io
import math
import os
import textwrap
import time
from typing import TYPE_CHECKING, Any, NamedTuple, cast

from . import cli, config, platform, schedule, state

if TYPE_CHECKING:
    from .dashboard import Canvas

MODES = ("automatic", "fixed", "memory", "lower")
MODE_LABELS = {"automatic": "Automatic", "fixed": "Fixed", "memory": "Memory", "lower": "CPU + memory"}
KNOBS = {"safety": ("Safety multiplier", 0.5), "limit": ("Overtaking limit (s)", 60),
         "fallback": ("Unknown runtime (s)", 60), "overrun": ("Overrun factor", 0.1),
         "interactive": ("Interactive advance (s)", 60), "batch": ("Batch delay (s)", 60),
         "yield_after": ("Yield after (s)", 30)}
GROUPS = (("SHARED CAPACITY", ("mode", "amount")), ("QUEUE POLICY", ("order", *KNOBS)),
          ("THIS DASHBOARD", ("interval",)))
DESCRIPTIONS = {
    "mode": "Shared by every client. Automatic: the lower of the CPU and memory budgets.",
    "amount.fixed": "Total slots for every client. Applies at the next grant; a shrink drains.",
    "amount.memory": "One slot per this many GiB of memory. Applies at the next grant.",
    "order": "Backfill lets predicted short work pass; Window serves in arrival order.",
    "safety": "Multiplier on expected runtimes for backfill; at least 1, higher is safer.",
    "limit": "Seconds a protected request waits before nothing passes it; 0: no limit.",
    "fallback": "Assumed runtime, in seconds, of work with no history.",
    "overrun": "Remaining runtime as a share of elapsed time once a run overruns.",
    "interactive": "Seconds interactive requests move ahead in the queue; 0: no advance.",
    "batch": "Seconds batch requests move back in the queue; 0: no delay.",
    "yield_after": "Seconds before a yieldable holder is asked to yield; 0: nobody is asked.",
    "interval": "Seconds between this dashboard's refreshes, 0.1 to 60. Not shared.",
}
DEFAULT_INTERVAL = 1.0
FLASH_SECONDS = 4.0
READ_EVERY = 1.0  # seconds between looks at the shared settings while the panel is open
STATUS_LABEL = 13  # columns of a status label
FIELD_LABEL = 28  # columns of a setting's label, its gutter included


class Row(NamedTuple):
    """One body line: `key` is the field it selects ("" for none); parts are (column, text, style)."""
    key: str
    parts: tuple[tuple[int, str, str], ...]


def number(value: float) -> str:
    return f"{value:g}"


class Settings:
    def __init__(self, interval: float):
        self.interval = interval
        self.focus = "mode"
        self.editing = False
        self.buffer = ""
        self.replace = True
        self.flash: tuple[str, str, float] | None = None
        self.confirm_reset = False
        self.offset = 0
        self.visible = 0
        self.total = 0
        self.reveal = True
        self.hits: list[tuple[str, int, int, int, int]] = []
        self.bounds = (0, 0, 0, 0)
        self.held = 0  # slots running leases hold, from the last model drawn
        self.basis: dict[str, Any] = {}
        self.saved: dict[str, Any] = {}
        self.effective: dict[str, Any] = {}
        self.memory_budget: int | None = None
        self.memory_gib: float | None = None
        self.read_at = -math.inf
        self.read()

    # Shared settings

    def read(self) -> None:
        """The saved settings as every client reads them now; raises SlotError for a file that cannot be read."""
        self.basis = cast(Any, config).capacity_basis()
        self.saved = cast(Any, schedule).order_settings(environment=False)
        self.effective = cast(Any, schedule).order_settings()
        self.memory_budget, self.memory_gib = self.basis["memory_slots"], self.basis["memory_gib"]
        if self.memory_budget is None and self.basis["mode"] == "fixed":  # fixed: the automatic rule's budget
            gib = platform.memory_gib()
            self.memory_budget = config.memory_slots(gib, config.AUTO_GIB_PER_SLOT)
            self.memory_gib = None if gib is None else round(gib, 1)
        self.read_at = time.monotonic()

    def refresh(self) -> None:
        """Reads the shared settings again, at most once a second, and never under a field being typed."""
        if self.editing or time.monotonic() - self.read_at < READ_EVERY:
            return
        try:
            self.read()
        except (config.SlotError, OSError) as error:
            self.say(str(error), "bad")
            self.read_at = time.monotonic()

    def value(self, key: str) -> str:
        if key == "mode":
            return self.basis["mode"]
        if key == "amount":
            return number(self.basis["slots"] if self.basis["mode"] == "fixed"
                          else self.basis["gib_per_slot"] or config.AUTO_GIB_PER_SLOT)
        if key == "interval":
            return number(self.interval)
        return self.saved[key] if key == "order" else number(self.saved[key])

    def label(self, key: str) -> str:
        if key == "mode":
            return "Capacity mode"
        if key == "amount":
            return "Total slots" if self.basis["mode"] == "fixed" else "GiB per slot"
        if key == "order":
            return "Queue order"
        if key == "interval":
            return "Refresh interval (s)"
        return KNOBS[key][0]

    def description(self, key: str) -> str:
        if key == "amount":
            return DESCRIPTIONS["amount.fixed" if self.basis["mode"] == "fixed" else "amount.memory"]
        return DESCRIPTIONS[key]

    def fields(self) -> list[str]:
        return [key for _, keys in GROUPS for key in keys if key != "amount" or self.basis["mode"] != "automatic"]

    def check(self, key: str, text: str) -> tuple[Any, str]:
        """(value, "") for typed text that `key` accepts, else (None, why not)."""
        try:
            value = float(text)
        except ValueError:
            value = math.nan
        if key == "amount" and self.basis["mode"] == "fixed":
            if not (text.isascii() and text.isdigit() and int(text) >= 1):
                return None, "Enter a whole number of slots, at least 1."
            return int(text), ""
        if not math.isfinite(value):
            return None, "Enter a number."
        if key == "amount":
            return (value, "") if value > 0 else (None, "Enter GiB per slot, more than 0.")
        if key == "interval":
            return (value, "") if 0.1 <= value <= 60 else (None, "Enter seconds from 0.1 to 60.")
        least = config.ORDER_KNOBS[key][1]
        return (value, "") if value >= least else (None, f"Enter a number of at least {least:g}.")

    def capacity_value(self, mode: str, amount: Any) -> dict[str, Any] | None:
        """What `slotfather slots` would save for the mode and amount; raises SlotError where it would refuse."""
        arguments = ["reset"] if mode == "automatic" else [str(amount)] if mode == "fixed" else [mode, str(amount)]
        with contextlib.redirect_stderr(io.StringIO()):
            return cli.slots_value(arguments)

    def drain_note(self, slots: int) -> str:
        drain = max(0, self.held - slots)
        return f"will drain {drain}: running work keeps its slots" if drain else ""

    def preview(self) -> tuple[str, str]:
        """What the text being typed would do, (text, style): the capacity it gives, or why it is refused."""
        if not self.buffer:
            return "", "dim"
        value, problem = self.check(self.focus, self.buffer)
        if problem:
            return "✗ " + problem, "bad"
        if self.focus != "amount":
            return "Enter saves", "dim"
        try:
            found = self.capacity_value(self.basis["mode"], value)
        except config.SlotError as error:
            return "✗ " + str(error), "bad"
        slots = (found or {}).get("slots", value)
        note = self.drain_note(slots)
        return (f"→ {slots} slot{'s' if slots != 1 else ''}" + (f" · {note}" if note else ""),
                "warn" if note else "dim")

    def save(self, key: str, value: Any) -> str:
        """Saves one field at once and says what it did; local fields change only this dashboard."""
        if key == "interval":
            self.interval = float(value)
            return f"Refresh interval {number(self.interval)} s · this dashboard only"
        if key in ("mode", "amount"):
            mode = value if key == "mode" else self.basis["mode"]
            amount = value if key == "amount" else (
                self.basis["slots"] if mode == "fixed" else self.basis["gib_per_slot"] or config.AUTO_GIB_PER_SLOT)
            state.configure({"capacity.json": self.capacity_value(mode, amount)})
            self.read()
            # Short: what drains is said once in STATUS and once beside the field.
            return f"Saved · capacity {self.basis['slots']} slots"
        state.configure({"order.json": {key: value}})
        self.read()
        return f"Saved · {self.label(key)} {self.value(key).capitalize() if key == 'order' else self.value(key)}"

    def apply(self, key: str, value: Any) -> bool:
        """save() with its outcome flashed; False when the change was refused."""
        try:
            text = self.save(key, value)
        except (config.SlotError, OSError, ValueError) as error:
            self.say(str(error), "bad")
            with contextlib.suppress(config.SlotError, OSError):
                self.read()
            return False
        self.say(text, "ok")
        return True

    def say(self, text: str, style: str) -> None:
        self.flash = (text, style, time.monotonic())

    # Editing

    def move(self, count: int) -> None:
        if self.flash and self.flash[1] == "bad":
            self.flash = None  # a refusal belongs to the field it was about
        keys = self.fields()
        self.focus = keys[(keys.index(self.focus) + count) % len(keys)] if self.focus in keys else keys[0]
        self.reveal = True

    def step(self, direction: int) -> bool:
        key = self.focus
        if key == "mode":
            return self.apply(key, MODES[(MODES.index(self.basis["mode"]) + direction) % len(MODES)])
        if key == "order":
            return self.apply(key, "window" if self.saved["order"] == "backfill" else "backfill")
        fixed = key == "amount" and self.basis["mode"] == "fixed"
        minimum = (1 if fixed else 0.5 if key == "amount" else 0.1 if key == "interval"
                   else config.ORDER_KNOBS[key][1])
        size = 1 if fixed or key == "interval" else 0.5 if key == "amount" else KNOBS[key][1]
        current = float(self.value(key))
        value = max(minimum, round(current + direction * size, 3))
        if key == "interval":
            value = min(60.0, value)
        if value == current:
            return False
        return self.apply(key, int(value) if fixed else value)

    def reset(self, key: str) -> bool:
        if key in ("mode", "amount"):
            return self.apply("mode", "automatic")
        if key == "interval":
            return self.apply(key, DEFAULT_INTERVAL)
        return self.apply(key, config.ORDER_DEFAULTS[key])

    def reset_all(self) -> bool:
        try:
            state.configure({"capacity.json": None, "order.json": None})
            self.interval = DEFAULT_INTERVAL
            self.read()
        except (config.SlotError, OSError) as error:
            self.say(str(error), "bad")
            return False
        self.say(f"Saved · every setting at its default, capacity {self.basis['slots']} slots", "ok")
        return True

    def begin(self, text: str = "") -> None:
        self.editing, self.replace = True, not text
        self.buffer = text or self.value(self.focus)
        self.flash = None

    def commit(self) -> bool:
        """Saves the typed value; an invalid one stays in the field, unsaved, with why it was refused."""
        value, problem = self.check(self.focus, self.buffer)
        if problem:
            self.say(problem + " Nothing was saved.", "bad")
            return False
        self.editing = False
        return self.apply(self.focus, value)

    def edit_key(self, key: str) -> str:
        if key == "esc":
            self.editing = False
            self.say("Edit cancelled · nothing was saved", "dim")
            return "handled"
        if key in ("\r", "\n", "\t"):
            saved = self.commit()
            if saved and key == "\t":
                self.move(1)
            return "changed" if saved else "handled"
        if key in ("\x7f", "\b"):
            self.buffer = "" if self.replace else self.buffer[:-1]
        elif key == "\x15":  # Ctrl-U clears the field
            self.buffer = ""
        elif len(key) == 1 and key in "0123456789." and len(self.buffer) < 16:
            self.buffer = key if self.replace else self.buffer + key
        else:
            return "handled"
        self.replace = False
        return "handled"

    def key(self, key: str) -> str:
        """One key: "close", "quit", "changed" when a setting was saved, else "handled"."""
        if self.editing:
            return self.edit_key(key)
        confirming, self.confirm_reset = self.confirm_reset, False
        if confirming and key != "D":
            self.flash = None  # the reset-all question is withdrawn
        if key in ("esc", "s"):
            return "close"
        if key in ("q", "Q"):
            return "quit"
        choice = self.focus in ("mode", "order")
        if key in ("down", "j", "\t"):
            self.move(1)
        elif key in ("up", "k", "back_tab"):
            self.move(-1)
        elif key in ("home", "g", "end", "G"):
            self.focus = self.fields()[0 if key in ("home", "g") else -1]
            self.reveal = True
        elif key in ("left", "right", "h", "l", "-", "+"):
            return "changed" if self.step(-1 if key in ("left", "h", "-") else 1) else "handled"
        elif key in ("\r", "\n", " "):
            if choice:
                return "changed" if self.step(1) else "handled"
            self.begin()
        elif len(key) == 1 and key in "0123456789." and not choice:
            self.begin(key)
        elif key == "d":
            return "changed" if self.reset(self.focus) else "handled"
        elif key == "D":
            if confirming:
                return "changed" if self.reset_all() else "handled"
            self.confirm_reset = True
            self.say("Press D again to reset every setting to its default", "warn")
        elif key in ("page_up", "page_down"):
            self.offset = max(0, min(max(0, self.total - self.visible),
                                     self.offset + (1 if key == "page_down" else -1) * max(1, self.visible - 1)))
            self.reveal = False
        return "handled"

    def mouse(self, event: tuple[str, int, int, int, bool]) -> str:
        """A click selects a field, a click on the selected one changes or edits it; the wheel scrolls."""
        _, button, mx, my, pressed = event
        x, y, width, height = self.bounds
        if not pressed or not (x < mx < x + width - 1 and y < my < y + height - 1):
            return "handled"
        code = button & ~28
        if code in (64, 65):
            self.offset = max(0, min(max(0, self.total - self.visible), self.offset + (3 if code == 65 else -3)))
            self.reveal = False
            return "handled"
        if code != 0:
            return "handled"
        for key, left, top, columns, rows in self.hits:
            if left <= mx < left + columns and top <= my < top + rows:
                if key != self.focus:
                    self.editing, self.confirm_reset = False, False
                    self.focus = key
                    return "handled"
                if self.editing:
                    return "handled"
                return self.key("\r")
        return "handled"

    # Drawing

    def status_rows(self, model: dict[str, Any] | None, inner: int) -> list[Row]:
        def row(label: str, text: str, style: str = "text") -> Row:
            return Row("", ((2, label, "dim"), (2 + STATUS_LABEL, text, style)))

        rows = [Row("", ((0, "STATUS", "title"), (7, "· read-only, live", "dim")))]
        rows.append(row("queue", self.location()))
        if model:
            rows.append(row("slots", f"{model['slots']} total · {model.get('free', 0)} free · "
                                     f"{model.get('used', 0)} held · {len(model.get('waiters', []))} waiting"))
        else:
            rows.append(row("slots", "queue unavailable", "dim"))
        basis = self.basis
        per = number(basis["gib_per_slot"] or 0)
        how = {"fixed": "saved, fixed count", "memory": f"saved, one per {per} GiB of memory",
               "lower": f"saved, lower of CPU and one per {per} GiB",
               "automatic": "automatic, lower of the two budgets"}[basis["mode"]]
        rows.append(row("capacity", f"{basis['slots']} slots · {how}"))
        memory = self.memory_gib
        budget = (f"CPU {basis['cpu_slots']} ({basis['cores']} cores) · memory "
                  + (f"{self.memory_budget}" if self.memory_budget is not None else "unknown")
                  + (f" ({number(memory)} GiB)" if memory is not None else ""))
        rows.append(row("budgets", budget))
        drain = max(0, self.held - (model or {}).get("slots", basis["slots"])) if model else 0
        rows.append(row("draining", f"{drain} slots · nothing starts until at most {model['slots']} are held"
                        if drain and model else "none", "warn" if drain else "dim"))
        overrides = ["SLOTFATHER_" + name for name in ("CORES", "MEMORY_GIB", "CGROUP") if config.setting(name)]
        if overrides:
            rows.append(row("environment", ", ".join(overrides), "dim"))
        return rows

    def location(self) -> str:
        directory, home = str(config.state_dir()), os.path.expanduser("~")
        return "~" + directory[len(home):] if directory.startswith(home + os.sep) else directory

    def field_rows(self, key: str, value_column: int) -> list[Row]:
        """The field's row; while it is being typed, a second row says what the value would do, or why not."""
        selected = key == self.focus
        label, room = self.label(key), value_column - 3
        label = label if len(label) <= room else label[:max(0, room - 1)] + "…"
        parts: list[tuple[int, str, str]] = [(0, "▶" if selected else " ", "cursor"), (2, label, "text")]
        if selected and self.editing:
            parts.append((value_column, "[" + self.buffer + "▏]", "cursor"))
            note, style = self.preview()
            return [Row(key, tuple(parts)), Row(key, ((4, note, style),))]
        value = self.value(key)
        if key == "mode":
            value = MODE_LABELS[value]
        elif key == "order":
            value = value.capitalize()
        if selected and key in ("mode", "order"):
            value = f"‹ {value} ›"
        parts.append((value_column, value, "accent"))
        after = value_column + len(value) + 2
        if key == "amount" and self.basis["mode"] == "fixed":
            note = self.drain_note(self.basis["slots"])
            if note:
                parts.append((after, note.replace("will drain", "draining"), "warn"))
        elif key in self.effective and key != "interval" and self.effective[key] != self.saved[key]:
            effective = self.effective[key]
            shown = effective.capitalize() if key == "order" else number(effective)
            parts.append((after, f"effective here: {shown}", "dim"))  # this dashboard's environment tightens it
        return [Row(key, tuple(parts))]

    def rows(self, model: dict[str, Any] | None, inner: int) -> list[Row]:
        rows = self.status_rows(model, inner)
        rows.append(Row("", ((0, "─" * inner, "frame"),)))
        value_column = min(FIELD_LABEL, max(12, inner // 2))
        shown = self.fields()
        for title, keys in GROUPS:
            keys = tuple(key for key in keys if key in shown)
            if not keys:
                continue
            rows.append(Row("", ((0, title, "title"),)))
            for key in keys:
                rows.extend(self.field_rows(key, value_column))
        return rows

    def footer(self, inner: int) -> list[tuple[str, str]]:
        """The panel's one key-hint line; on a narrow panel the least needed hints go first, never Esc."""
        if self.editing:
            hints = [("Enter", "save", 0), ("Esc", "cancel", 0), ("⌫", "delete", 1), ("^U", "clear", 2)]
        else:
            hints = [("↑↓", "select", 1), ("←→", "change", 1), ("Enter", "edit", 2), ("d", "default", 3),
                     ("D", "all defaults", 4), ("Esc", "close", 0)]
        kept: list[tuple[str, str]] = []
        for drop in (5, 4, 3, 2, 1, 0):
            kept = [(key, action) for key, action, rank in hints if rank < drop]
            if sum(len(key) + len(action) + 3 for key, action in kept) - 2 <= inner:
                break
        parts: list[tuple[str, str]] = []
        for key, action in kept:
            parts += [(key, "key"), (f" {action}  ", "dim")]
        return parts

    def draw(self, canvas: Canvas, model: dict[str, Any] | None) -> None:
        self.refresh()
        if model:
            self.held = int(model.get("used", 0))
        if self.focus not in self.fields():
            self.focus = self.fields()[0]
        width = min(max(0, canvas.width - 2), 80)
        inner = max(0, width - 4)
        body = self.rows(model, inner)
        height = min(max(0, canvas.height - 2), len(body) + 7)
        x, y = (canvas.width - width) // 2, (canvas.height - height) // 2
        self.bounds = x, y, width, height
        self.hits = []
        canvas.fill(y, x, height, width)
        canvas.box(y, x, height, width, "SETTINGS", "saved at once · applies at the next grant" if inner >= 60 else "")
        if height < 12 or width < 30:
            canvas.put(y + 1, x + 2, "Enlarge terminal · Esc closes", "dim", inner)
            return
        self.visible, self.total = height - 7, len(body)
        if self.reveal:
            at = next((n for n, row in enumerate(body) if row.key == self.focus), None)
            if at is not None:
                self.offset = min(self.offset, at)
                self.offset = max(self.offset, at - self.visible + 1)
            self.reveal = False
        self.offset = max(0, min(self.offset, max(0, len(body) - self.visible)))
        if self.offset:
            canvas.right(y, x + width - 2, f" ↑ {self.offset} above ", "dim")
        for line, row in enumerate(body[self.offset:self.offset + self.visible], y + 1):
            for column, text, style in row.parts:
                if column < inner:
                    canvas.put(line, x + 2 + column, text, style, inner - column)
            if row.key:
                self.hits.append((row.key, x + 1, line, width - 2, 1))
                if row.key == self.focus:
                    canvas.select(line, x + 1, x + width - 1)
        rule = y + height - 6
        canvas.put(rule, x + 2, "─" * inner, "frame", inner)
        below = max(0, len(body) - self.offset - self.visible)
        if below:
            canvas.right(rule, x + width - 2, f" ↓ {below} more ", "dim")
        notes = [(text, "dim") for text in textwrap.wrap(self.description(self.focus), max(1, inner))[:2]]
        if self.flash and (time.monotonic() - self.flash[2] < FLASH_SECONDS or self.flash[1] == "bad"):
            text, style, _ = self.flash
            mark = {"ok": "✓ ", "warn": "! ", "bad": "✗ "}.get(style, "")
            notes = notes[:1] + [(text, style) for text in textwrap.wrap(mark + text, max(1, inner))[:2]]
        for line, (text, style) in enumerate(notes[:3], rule + 1):
            canvas.put(line, x + 2, text, style, inner)
        canvas.segments(y + height - 2, x + 2, self.footer(inner), inner)
