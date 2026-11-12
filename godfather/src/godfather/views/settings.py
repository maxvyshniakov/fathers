"""The dashboard's Settings panel (#7), moved from Slotfather: a read-only status of the queue above the editable
settings, read and written through `slotfather settings` (docs/contracts/slotfather.md), which keeps Slotfather's
validation, ownership and queue lock.

A change is saved through `slotfather settings set` and applies at the queue's next grant, busy or not; nothing is
drafted. What a typed value would do, and why one is refused, comes from `slotfather settings check`. An arrow change is shown at once and saved once
the keys have settled for SETTLE_SECONDS (or at once on Enter), so stepping through the choices never applies the
ones on the way; a typed value is saved on Enter. A capacity change carries the revision this panel read, and is
refused (a conflict) when capacity.json changed since. A capacity below what running leases hold drains: they keep their slots and nothing more starts until
enough of them end. The refresh interval and the colour theme belong to this dashboard alone."""
from __future__ import annotations

import contextlib
import math
import os
import textwrap
import time
from typing import TYPE_CHECKING, Any, Callable, NamedTuple, cast

from .. import keys as _keys
from .. import theme as _theme
from ..adapters.slotfather import Conflict, SettingsClient, SettingsError

if TYPE_CHECKING:
    from ..tui import Canvas

MODES = ("automatic", "fixed", "memory", "lower")
MODE_LABELS = {"automatic": "Automatic", "fixed": "Fixed", "memory": "Memory", "lower": "CPU + memory"}
ORDER_LABELS = {"backfill": "Backfill", "window": "Arrival"}
# Each setting's label and arrow step; the unit goes beside the value (UNITS), not in the label. Display text only:
# the keys, order.json, the CLI flags and the environment keep their names.
KNOBS = {"safety": ("Backfill margin", 0.5), "limit": ("Overtaking stops after", 60),
         "fallback": ("Unknown job runtime", 60), "overrun": ("Overrun estimate", 0.1),
         "interactive": ("Interactive head start", 60), "batch": ("Batch held back", 60),
         "yield_after": ("Ask to yield after", 30), "idle_cpu": ("Idle when below", 0.05),
         "idle_after": ("Idle for", 60), "aging_batch": ("Batch promoted after", 300),
         "aging_normal": ("Normal promoted after", 300), "warn_wait": ("Warn after waiting", 300)}
UNITS = {"safety": "×", "overrun": "×", "limit": " s", "fallback": " s", "interactive": " s", "batch": " s",
         "yield_after": " s", "idle_after": " s", "idle_cpu": " cores", "interval": " s", "aging_batch": " s",
         "aging_normal": " s", "warn_wait": " s"}
# One flat list (owner, 2026-11-11), the STATUS block above it
FIELDS = ("mode", "amount", "order", "safety", "fallback", "overrun", "limit", "interactive", "batch", "aging_batch",
          "aging_normal", "warn_wait", "idle_cpu", "idle_after", "yield_after", "interval", "theme")
NEVER = ("limit", "aging_batch", "aging_normal", "warn_wait")  # 0 is off: shown as "never"
LOCAL = ("interval", "theme")  # this dashboard's own settings, never written to the shared queue
CHOICES = ("mode", "order", "theme")
DESCRIPTIONS = {
    "mode": "Shared by every client. Automatic: the lower of the CPU and memory budgets.",
    "amount.fixed": "Total slots for every client. Applies at the next grant; a shrink drains.",
    "amount.memory": "One slot per this many GiB of memory. Applies at the next grant.",
    "order": "Backfill lets short work pass; Arrival serves in arrival order.",
    "safety": "Multiplier on expected runtimes for backfill; at least 1.",
    "limit": "After this a waiting head lets nothing pass; 0: its reserved start alone.",
    "fallback": "Assumed runtime of work with no history.",
    "overrun": "Remaining runtime as a share of elapsed time once a run overruns.",
    "interactive": "How far interactive requests move ahead; 0: not at all.",
    "batch": "How far batch requests move back; 0: not at all.",
    "aging_batch": "A waiting batch request is served as normal after this; 0: never.",
    "aging_normal": "A waiting normal request is served as interactive after this; 0: never.",
    "warn_wait": "A request waiting this long says so and is flagged; 0: never.",
    "yield_after": "Before a yieldable holder is asked to yield; 0: nobody is asked.",
    "idle_cpu": "A lease below this for the idle time is idle (💤); 0: never.",
    "idle_after": "Below the idle cores this long: idle, and asked if it can yield.",
    "interval": "Seconds between refreshes, 0.1 to 60 · this dashboard only.",
    "theme": "Auto, Terminal (palette only) or Built-in colours · this dashboard only.",
}
DEFAULT_INTERVAL = 1.0
FLASH_SECONDS = 4.0  # how long a confirmation shows, and how long a first D stays armed
SETTLE_SECONDS = 1.0  # quiet after an arrow change before it is saved
READ_EVERY = 1.0  # seconds between looks at the shared settings while the panel is open
CAPACITY_PER_CORE = 4  # the most slots the panel saves: this many per core, unless Slotfather says otherwise
# Each knob's least value, where Slotfather's settings give no `min` (its ORDER_KNOBS)
MINIMUMS = {"safety": 1.0, "limit": 0.0, "fallback": 0.0, "overrun": 0.0, "interactive": 0.0, "batch": 0.0,
            "yield_after": 0.0, "idle_cpu": 0.0, "idle_after": 1.0, "aging_batch": 0.0, "aging_normal": 0.0,
            "warn_wait": 0.0}
AUTO_GIB_PER_SLOT = 4.0  # Slotfather's automatic rule, the GiB per slot a memory mode starts from without one
STATUS_LABEL = 13  # columns of a status label
FIELD_LABEL = 28  # columns of a setting's label, its gutter included
MIN_WIDTH, MIN_HEIGHT = 30, 12  # below this the panel only says to enlarge the terminal


class Row(NamedTuple):
    """One body line: `key` is the field it selects ("" for none); parts are (column, text, style)."""
    key: str
    parts: tuple[tuple[int, str, str], ...]


class Pending(NamedTuple):
    """An arrow change shown but not saved yet: saved at `at` + SETTLE_SECONDS, or at once on Enter. It keeps
    capacity.json as it was when the change began (`token`) and the capacity mode its value was chosen in, so a
    change made elsewhere meanwhile is a conflict, shown and never overwritten, and a value is never saved under
    another mode than the one it was chosen in."""
    key: str
    value: Any
    at: float
    token: bytes | None = None
    mode: str = ""


def number(value: float) -> str:
    return f"{value:g}"


def plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def setting_key(key: str, mode: str) -> str:
    """Slotfather's key for a panel field: capacity.slots or capacity.gib_per_slot for the amount, by mode."""
    if key == "mode":
        return "capacity.mode"
    if key == "amount":
        return "capacity.slots" if mode == "fixed" else "capacity.gib_per_slot"
    return "order." + key


def argument(value: Any) -> str:
    """A value as `settings set` takes it: 1800, 0.05, fixed."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


class Settings:
    def __init__(self, interval: float, theme: str = _theme.DEFAULT_MODE, clock: Callable[[], float] = time.monotonic,
                 client: SettingsClient | None = None, problem: Callable[[], str | None] = lambda: None,
                 local_only: bool = False):
        self.clock = clock
        # local_only: Slotfather is absent or has no settings contract; only this dashboard's own rows show
        self.local_only = local_only
        self.client = client if client is not None else SettingsClient()
        self.problem = problem  # why the Slotfather snapshot is stale or unsupported, for STATUS
        self.interval: float = interval
        self.theme: str = _theme.checked_mode(theme)
        self.focus: str = "mode"
        self.editing: bool = False
        self.buffer = ""
        self.replace = True
        self.pending: Pending | None = None
        self.flash: tuple[str, str, float] | None = None
        self.armed: float | None = None  # when D was pressed once
        self.small = False  # the last draw was below the minimum size
        self.offset = 0
        self.visible = 0
        self.total = 0
        self.reveal = True
        self.hits: list[tuple[str, int, int, int, int]] = []
        self.bounds = (0, 0, 0, 0)
        self.held = 0  # slots running leases hold, from the last model drawn
        self.waiting: list[int] = []  # the waiting requests' slot counts, in queue order, from the last model
        self.basis: dict[str, Any] = {}
        self.capacity_bytes: str | None = None  # capacity.json's revision as read: a save expects it unchanged
        self.reply: dict[str, Any] = {}  # the last `settings --json`
        self.rows_by_key: dict[str, dict[str, Any]] = {}
        self.checked: dict[tuple[str, str], dict[str, Any]] = {}  # `settings check` replies by (key, value)
        self.saved: dict[str, Any] = {}
        self.effective: dict[str, Any] = {}
        self.memory_budget: int | None = None
        self.memory_gib: float | None = None
        self.read_at = -math.inf
        self.read()

    # Shared settings

    def read(self) -> None:
        """The saved settings as every client reads them now (`settings --json`); raises SettingsError when they
        cannot be read."""
        if self.local_only:
            self.read_at = self.clock()
            self.settle_focus()
            return
        self.take(self.client.read())

    def take(self, reply: dict[str, Any]) -> None:
        """A `settings --json` object, from a read or from what set and reset print."""
        rows = {str(row.get("key")): row for section in reply.get("sections") or [] if isinstance(section, dict)
                for row in section.get("settings") or [] if isinstance(row, dict)}
        basis = reply.get("basis")
        if not isinstance(basis, dict) or "capacity.mode" not in rows:
            raise SettingsError("slotfather settings: no capacity in the reply; update slotfather")
        self.reply, self.rows_by_key, self.basis = reply, rows, cast(dict[str, Any], basis)
        self.capacity_bytes = cast(Any, rows["capacity.mode"].get("revision"))
        self.saved = {key[len("order."):]: row.get("value") for key, row in rows.items() if key.startswith("order.")}
        self.effective = {key[len("order."):]: row.get("effective", row.get("value")) for key, row in rows.items()
                          if key.startswith("order.")}
        self.memory_budget, self.memory_gib = self.basis.get("memory_slots"), self.basis.get("memory_gib")
        self.checked = {}
        self.read_at = self.clock()
        self.settle_focus()

    def refresh(self) -> None:
        """Reads the shared settings again, at most once a second, and never under a field being typed. A capacity
        change pending since before another client changed capacity.json becomes a conflict: dropped, and said."""
        if self.editing or self.clock() - self.read_at < READ_EVERY:
            return
        pending = self.pending
        try:
            self.read()
        except SettingsError as error:
            self.say(str(error), "bad")
            self.read_at = self.clock()
            return
        if pending and pending.key in ("mode", "amount") and self.capacity_bytes != pending.token:
            self.pending = None
            self.say(f"Capacity changed elsewhere: now {self.basis['slots']} slots · your change was not saved",
                     "bad")

    def mode(self) -> str:
        """The capacity mode shown: a pending one, else the saved one."""
        return self.pending.value if self.pending and self.pending.key == "mode" else \
            str(self.basis.get("mode") or "automatic")

    def amount_for(self, mode: str) -> Any:
        """The amount a mode starts from: the current capacity for Fixed, the GiB per slot for the memory modes."""
        if mode == "fixed":
            return self.basis["slots"]
        return self.basis.get("gib_per_slot") or AUTO_GIB_PER_SLOT

    def saved_value(self, key: str) -> Any:
        if key == "mode":
            return self.basis["mode"]
        if key == "amount":
            return self.amount_for(self.mode())
        if key == "interval":
            return self.interval
        if key == "theme":
            return self.theme
        return self.saved[key]

    def shown_value(self, key: str) -> Any:
        if self.pending and self.pending.key == key:
            return self.pending.value
        return self.saved_value(key)

    def text(self, key: str, value: Any) -> str:
        """A value as shown, with its unit beside it: 30 s, 0.05 cores, 1.5×, 8 GiB."""
        if key == "mode":
            return MODE_LABELS[value]
        if key == "order":
            return ORDER_LABELS.get(str(value), str(value).capitalize())
        if key == "theme":
            return _theme.MODE_LABELS[value]
        if key == "amount":
            return number(value) + ("" if self.mode() == "fixed" else " GiB")
        if key in NEVER and not value:
            return "never"
        return number(value) + UNITS.get(key, "")

    def label(self, key: str) -> str:
        if key == "mode":
            return "Capacity"
        if key == "amount":
            return "Slots" if self.mode() == "fixed" else "GiB per slot"
        if key == "order":
            return "Queue order"
        if key == "interval":
            return "Refresh every"
        if key == "theme":
            return "Colour theme"
        return KNOBS[key][0]

    def description(self, key: str) -> str:
        if key == "amount":
            return DESCRIPTIONS["amount.fixed" if self.mode() == "fixed" else "amount.memory"]
        return DESCRIPTIONS[key]

    def fields(self) -> list[str]:
        if self.local_only:
            return list(LOCAL)
        return [key for key in FIELDS if key != "amount" or self.mode() != "automatic"]

    def settle_focus(self) -> None:
        """A focused field that is no longer shown (Total slots after a reset to Automatic) gives the focus to the
        capacity mode; its edit and pending change go with it."""
        if self.pending and self.pending.key not in self.fields():
            self.pending = None
        if self.focus in self.fields():
            return
        self.focus, self.editing, self.reveal = self.fields()[0], False, True

    def most_slots(self) -> int:
        row = self.rows_by_key.get("capacity.slots") or {}
        if isinstance(row.get("max"), (int, float)):
            return int(row["max"])
        return CAPACITY_PER_CORE * int(self.basis.get("cores") or 1)

    def least(self, key: str) -> float:
        row = self.rows_by_key.get("order." + key) or {}
        return float(row["min"]) if isinstance(row.get("min"), (int, float)) else MINIMUMS[key]

    def ask(self, key: str, value: Any, mode: str | None = None) -> dict[str, Any]:
        """`settings check` for a field's value, once per value while this reading lasts."""
        name = setting_key(key, mode or self.mode())
        found = self.checked.get((name, argument(value)))
        if found is None:
            found = self.checked[name, argument(value)] = self.client.check(name, argument(value))
        return found

    def check(self, key: str, text: str) -> tuple[Any, str]:
        """(value, "") for typed text that `key` accepts, else (None, why not): Slotfather's answer, but for this
        dashboard's own Refresh every."""
        if key not in self.fields() or key in CHOICES:
            return None, "This setting takes no typed value."
        try:
            value = float(text)
        except ValueError:
            value = math.nan
        if key == "interval":
            if not math.isfinite(value):
                return None, "Enter a number."
            return (value, "") if 0.1 <= value <= 60 else (None, "Enter seconds from 0.1 to 60.")
        found = self.ask(key, text)
        if not found.get("ok"):
            return None, str(found.get("error") or "Not accepted.")
        if key == "amount" and self.mode() == "fixed":
            return int(text), ""
        return value, ""

    def capacity_of(self, key: str, value: Any) -> tuple[str, Any]:
        """(mode, amount) for a value of a capacity field."""
        return (value, self.amount_for(value)) if key == "mode" else (self.mode(), value)

    def slots_of(self, key: str, value: Any) -> int:
        """The capacity a capacity field's value would give, as `settings check` says; raises SettingsError with
        why it refuses the value."""
        mode, _ = self.capacity_of(key, value)
        found = self.ask(key, value, mode if key == "amount" else None)
        if not found.get("ok"):
            raise SettingsError(str(found.get("error") or "Not accepted."))
        if not isinstance(found.get("slots"), int):
            raise SettingsError("slotfather settings check gave no slot count; update slotfather")
        return int(found["slots"])

    def drain_note(self, slots: int) -> str:
        drain = max(0, self.held - slots)
        return f"will drain {drain}: running work keeps its slots" if drain else ""

    def starts(self, slots: int) -> int:
        """How many waiting requests a capacity of `slots` lets start now by slot counts alone, in queue order."""
        free, started = slots - self.held, 0
        for count in self.waiting:
            if 0 < count <= free:
                free, started = free - count, started + 1
        return started

    def effect(self, key: str, value: Any) -> tuple[str, str]:
        """What a capacity value would do, (text, style): the slots, what drains or starts, or why it is refused."""
        try:
            slots = self.slots_of(key, value)
        except SettingsError as error:
            return "✗ " + str(error), "bad"
        drain, start = self.drain_note(slots), self.starts(slots)
        if drain:
            return f"→ {plural(slots, 'slot')} · {drain}", "warn"
        return f"→ {plural(slots, 'slot')}" + (f" · will start {start} waiting" if start else ""), "dim"

    def preview(self) -> tuple[str, str]:
        """What the text being typed would do, (text, style): its effect, or why it is refused."""
        if not self.buffer:
            return "", "dim"
        value, problem = self.check(self.focus, self.buffer)
        if problem:
            return "✗ " + problem, "bad"
        return self.effect(self.focus, value) if self.focus == "amount" else ("Enter saves", "dim")

    def save(self, key: str, value: Any, pending: Pending | None = None) -> str:
        """Saves one field and says what it did; local fields change only this dashboard. A capacity change
        expects capacity.json as this panel read it, or as it was when `pending` began (state.Conflict otherwise),
        and an amount is saved under the mode it was chosen in."""
        if key == "interval":
            self.interval = float(value)
            return f"Refresh every {number(self.interval)} s · this dashboard only"
        if key == "theme":
            self.theme = _theme.checked_mode(value)
            return f"Colour theme {self.text(key, self.theme)} · this dashboard only"
        if key in ("mode", "amount"):
            mode, amount = self.capacity_of(key, value)
            if pending is not None and key == "amount":
                mode = pending.mode
            name = setting_key(key, mode)
            self.take(self.client.set(name, argument(value if key == "mode" else amount),
                                      self.capacity_bytes if pending is None else pending.token))
            # Short: what drains is said once in STATUS and once beside the field.
            return f"Saved · capacity {self.basis['slots']} slots"
        self.take(self.client.set(setting_key(key, ""), argument(value)))
        return f"Saved · {self.label(key)} {self.text(key, self.saved_value(key))}"

    def apply(self, key: str, value: Any, pending: Pending | None = None) -> bool:
        """save() with its outcome flashed; False when the change was refused."""
        self.pending = None
        try:
            text = self.save(key, value, pending)
        except Conflict as conflict:
            with contextlib.suppress(SettingsError):
                self.take(conflict.reply) if conflict.reply else self.read()
            self.say(f"Capacity changed elsewhere: now {self.basis['slots']} slots · nothing was saved", "bad")
            return False
        except SettingsError as error:
            self.say(str(error), "bad")
            with contextlib.suppress(SettingsError):
                self.read()
            return False
        self.say(text, "ok")
        return True

    def say(self, text: str, style: str) -> None:
        self.flash = (text, style, self.clock())

    # Arrow changes settle before they are saved

    def step(self, direction: int) -> None:
        """Shows the next value of the focused field; tick() or Enter saves it."""
        key = self.focus
        current = self.shown_value(key)
        if key == "mode":
            value: Any = MODES[(MODES.index(current) + direction) % len(MODES)]
        elif key == "order":
            value = "window" if current == "backfill" else "backfill"
        elif key == "theme":
            value = _theme.MODES[(_theme.MODES.index(current) + direction) % len(_theme.MODES)]
        elif key == "interval":  # 0.1, 1, 2, 3 ... 60
            value = (1.0 if current < 1 else min(60.0, math.floor(current) + 1.0)) if direction > 0 else \
                (max(1.0, math.ceil(current) - 1.0) if current > 1 else 0.1)
        else:
            fixed = key == "amount" and self.mode() == "fixed"
            minimum = 1 if fixed else 0.5 if key == "amount" else self.least(key)
            size = 1 if fixed else 0.5 if key == "amount" else KNOBS[key][1]
            value = max(minimum, round(float(current) + direction * size, 3))
            if fixed:
                value = min(self.most_slots(), int(value))
        if value == current:
            return
        if value == self.saved_value(key):
            self.pending = None
            self.say(f"{self.label(key)} back to its saved value", "dim")
        else:
            self.pending = Pending(key, value, self.clock(), self.capacity_bytes, self.mode() if key == "amount"
                                   else self.basis["mode"])
            self.say(f"{self.label(key)} {self.text(key, value)} · saves in {number(SETTLE_SECONDS)} s · "
                     f"Enter now · Esc cancels", "accent")
        self.settle_focus()

    def commit_pending(self) -> bool:
        """Saves the pending change now; False when there is none or it was refused."""
        if self.pending is None:
            return False
        return self.apply(self.pending.key, self.pending.value, self.pending)

    def tick(self) -> bool:
        """Saves a pending change once the keys have settled; True when something was saved."""
        if self.pending and self.clock() - self.pending.at >= SETTLE_SECONDS:
            return self.commit_pending()
        return False

    def cancel_pending(self) -> None:
        if self.pending:
            self.pending = None
            self.say("Change cancelled · nothing was saved", "dim")
            self.settle_focus()

    # Editing

    def move(self, count: int) -> bool:
        """Moves the focus; a pending change is saved first (True when it was)."""
        if self.flash and self.flash[1] == "bad":
            self.flash = None  # an earlier refusal belongs to the field it was about; one from this save stays
        saved = self.commit_pending()
        keys = self.fields()
        self.focus = keys[(keys.index(self.focus) + count) % len(keys)] if self.focus in keys else keys[0]
        self.editing, self.reveal = False, True
        return saved

    def select(self, key: str) -> bool:
        """Focuses `key`; a pending change is saved first (True when it was), an edit is cancelled."""
        saved = self.commit_pending()
        if self.editing:
            self.say("Edit cancelled · nothing was saved", "dim")
        self.focus, self.editing, self.reveal = key, False, True
        self.settle_focus()
        return saved

    def reset(self, key: str) -> bool:
        self.pending = None
        if key in ("mode", "amount"):
            saved = self.apply("mode", "automatic")
        elif key == "interval":
            saved = self.apply(key, DEFAULT_INTERVAL)
        elif key == "theme":
            saved = self.apply(key, _theme.DEFAULT_MODE)
        else:
            self.pending = None
            try:
                self.take(self.client.reset(setting_key(key, "")))
            except SettingsError as error:
                self.say(str(error), "bad")
                saved = False
            else:
                self.say(f"Saved · {self.label(key)} {self.text(key, self.saved_value(key))}", "ok")
                saved = True
        self.settle_focus()
        return saved

    def reset_all(self) -> bool:
        self.pending = None
        try:
            if not self.local_only:
                self.take(self.client.reset(None))
            self.interval, self.theme = DEFAULT_INTERVAL, _theme.DEFAULT_MODE
        except SettingsError as error:
            self.say(str(error), "bad")
            return False
        self.say("Saved · every setting at its default" + ("" if self.local_only else
                 f", capacity {self.basis['slots']} slots"), "ok")
        return True

    def begin(self, text: str = "") -> None:
        self.pending = None
        self.editing, self.replace = True, not text
        self.buffer = text or number(self.saved_value(self.focus))
        self.flash = None

    def commit(self) -> bool:
        """Saves the typed value; an invalid one stays in the field, unsaved, with why it was refused."""
        value, problem = self.check(self.focus, self.buffer)
        if not problem and self.focus == "amount":
            text, style = self.effect(self.focus, value)
            problem = text[2:] if style == "bad" else ""
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
        if not self.editing:  # a hotkey by its US-QWERTY position (#13); typed values stay as typed
            key = _keys.hotkey(key)
        if self.small:  # nothing to see: only the ways out; a pending change is cancelled first, and said
            if key in ("esc", "s") and self.pending:
                self.cancel_pending()
                return "handled"
            return "close" if key in ("esc", "s") else "quit" if key in ("q", "Q") else "handled"
        self.settle_focus()
        if self.editing:
            return self.edit_key(key)
        armed, self.armed = self.armed, None
        if armed is not None and key != "D":
            self.flash = None  # the reset-all question is withdrawn
        if key in ("esc", "s"):
            if self.pending:
                self.cancel_pending()
                return "handled"
            return "close"
        if key in ("q", "Q"):
            return "quit"
        choice = self.focus in CHOICES
        saved = False
        if key in ("down", "j", "\t"):
            saved = self.move(1)
        elif key in ("up", "k", "back_tab"):
            saved = self.move(-1)
        elif key in ("home", "g", "end", "G"):
            saved = self.select(self.fields()[0 if key in ("home", "g") else -1])
        elif key in ("left", "right", "h", "l", "-", "+"):
            self.step(-1 if key in ("left", "h", "-") else 1)
        elif key in ("\r", "\n", " "):
            if self.pending and key != " ":
                saved = self.commit_pending()
            elif choice:
                self.step(1)
            else:
                self.begin()
        elif len(key) == 1 and key in "0123456789." and not choice:
            self.begin(key)
        elif key == "d":
            saved = self.reset(self.focus)
        elif key == "D":
            if armed is not None and self.clock() - armed < FLASH_SECONDS:
                saved = self.reset_all()
            else:
                self.pending = None
                self.armed = self.clock()
                self.say("Press D again to reset every setting to its default", "warn")
        elif key in ("page_up", "page_down"):
            self.offset = max(0, min(max(0, self.total - self.visible),
                                     self.offset + (1 if key == "page_down" else -1) * max(1, self.visible - 1)))
            self.reveal = False
        return "changed" if saved else "handled"

    def mouse(self, event: tuple[str, int, int, int, bool]) -> str:
        """A click selects a field, a click on the selected one changes or edits it; the wheel scrolls."""
        _, button, mx, my, pressed = event
        x, y, width, height = self.bounds
        code = button & ~28
        if not (x <= mx < x + width and y <= my < y + height):  # outside: a left click closes, as Esc does (#15)
            return "close" if pressed and code == 0 and not self.editing and not self.pending else "handled"
        if self.small or not pressed or not (x < mx < x + width - 1 and y < my < y + height - 1):
            return "handled"
        if code in (64, 65):
            self.offset = max(0, min(max(0, self.total - self.visible), self.offset + (3 if code == 65 else -3)))
            self.reveal = False
            return "handled"
        if code != 0:
            return "handled"
        for key, left, top, columns, rows in self.hits:
            if left <= mx < left + columns and top <= my < top + rows:
                if key != self.focus:
                    return "changed" if self.select(key) else "handled"
                if self.editing:
                    return "handled"
                return self.key(" " if key in CHOICES else "\r")
        return "handled"

    # Drawing

    def status_rows(self, model: dict[str, Any] | None) -> list[Row]:
        def row(label: str, text: str, style: str = "text") -> Row:
            return Row("", ((2, label, "dim"), (2 + STATUS_LABEL, text, style)))

        rows = [Row("", ((0, "STATUS", "title"), (7, "· read-only, live", "dim")))]
        if self.local_only:
            rows.append(row("slotfather", self.problem() or "no settings contract; update slotfather", "dim"))
            return rows
        rows.append(row("queue", self.location()))
        if model:
            rows.append(row("slots", f"{model['slots']} total · {model.get('free', 0)} free · "
                                     f"{model.get('used', 0)} held · {len(model.get('waiters', []))} waiting"))
        else:
            rows.append(row("slots", "queue unavailable", "dim"))
        # The capacity the queue's last look read under its lock; this panel's own reading only without a model.
        basis = cast(dict[str, Any], (model or {}).get("basis") or self.basis)
        per = number(basis["gib_per_slot"] or 0)
        how = {"fixed": "saved, fixed count", "memory": f"saved, one per {per} GiB of memory",
               "lower": f"saved, lower of CPU and one per {per} GiB",
               "automatic": "automatic, lower of the two budgets"}[basis["mode"]]
        rows.append(row("capacity", f"{basis['slots']} slots · {how}"))
        budget_slots = basis["memory_slots"] if basis["memory_slots"] is not None else self.memory_budget
        memory = basis["memory_gib"] if basis["memory_gib"] is not None else self.memory_gib
        budget = (f"CPU {basis['cpu_slots']} ({basis['cores']} cores) · memory "
                  + (f"{budget_slots}" if budget_slots is not None else "unknown")
                  + (f" ({number(memory)} GiB)" if memory is not None else ""))
        rows.append(row("budgets", budget))
        drain = max(0, self.held - int(model["slots"])) if model else 0
        rows.append(row("draining", f"{drain} slots · nothing starts until at most {model['slots']} are held"
                        if drain and model else "none", "warn" if drain else "dim"))
        environment = next((item for item in self.reply.get("status") or []
                            if isinstance(item, dict) and item.get("label") == "environment"), None)
        if environment and environment.get("value"):
            rows.append(row("environment", str(environment["value"]), "dim"))
        problem = self.problem()
        if problem:  # the Slotfather snapshot is stale or unsupported: why, from the source
            rows.append(row("slotfather", problem, "bad"))
        return rows

    def location(self) -> str:
        """The queue's directory, tilde-shortened: Slotfather's STATUS `queue` row."""
        found = next((item.get("value") for item in self.reply.get("status") or []
                      if isinstance(item, dict) and item.get("label") == "queue"), None)
        directory, home = str(found or self.basis.get("directory") or "?"), os.path.expanduser("~")
        return "~" + directory[len(home):] if directory.startswith(home + os.sep) else directory

    def field_rows(self, key: str, value_column: int) -> list[Row]:
        """The field's row; while it is typed or pending, a second row says what the value would do, or why not."""
        selected = key == self.focus
        label, room = self.label(key), value_column - 3
        label = label if len(label) <= room else label[:max(0, room - 1)] + "…"
        parts: list[tuple[int, str, str]] = [(0, "▶" if selected else " ", "cursor"), (2, label, "text")]
        if selected and self.editing:
            parts.append((value_column, "[" + self.buffer + "▏]", "cursor"))
            note, style = self.preview()
            return [Row(key, tuple(parts)), Row(key, ((4, note, style),))]
        pending = self.pending is not None and self.pending.key == key
        value = self.text(key, self.shown_value(key))
        if selected and key in CHOICES:
            value = f"‹ {value} ›"
        parts.append((value_column, value, "cursor" if pending else "accent"))
        after = value_column + len(value) + 2
        if pending:
            parts.append((after, "pending", "dim"))
            if key in ("mode", "amount"):
                note, style = self.effect(key, self.shown_value(key))
                return [Row(key, tuple(parts)), Row(key, ((4, note, style),))]
            return [Row(key, tuple(parts))]
        if key == "amount" and self.mode() == "fixed":
            note = self.drain_note(self.basis["slots"])
            if note:
                parts.append((after, note.replace("will drain", "draining"), "warn"))
        elif key in self.effective and key not in LOCAL and self.effective[key] != self.saved[key]:
            effective = self.effective[key]
            shown = self.text(key, effective)
            parts.append((after, f"effective here: {shown}", "dim"))  # this dashboard's environment tightens it
        return [Row(key, tuple(parts))]

    def rows(self, model: dict[str, Any] | None, inner: int) -> list[Row]:
        rows = self.status_rows(model)
        rows.append(Row("", ((0, "─" * inner, "frame"),)))
        value_column = min(FIELD_LABEL, max(12, inner // 2))
        shown = self.fields()
        for key in shown:  # one flat list (owner, 2026-11-11)
            rows.extend(self.field_rows(key, value_column))
        return rows

    def footer(self) -> list[tuple[str, str]]:
        """The panel's short key line, centred like the dashboard's footer (Canvas.overlay_footer())."""
        if self.editing:
            return [("enter", "save"), ("esc", "cancel")]
        if self.pending:
            return [("enter", "save now"), ("esc", "cancel")]
        return [("↑↓", "select"), ("←→", "change"), ("enter", "edit"), ("d", "reset"), ("esc", "close")]

    def draw(self, canvas: Canvas, model: dict[str, Any] | None) -> None:
        self.refresh()
        if model:
            self.held = int(model.get("used", 0))
            self.waiting = [int(waiter.get("count", 1)) for waiter in model.get("waiters", [])]
        self.settle_focus()
        width = min(max(0, canvas.width - 2), 80)
        inner = max(0, width - 4)
        body = self.rows(model, max(0, inner - 2))  # two padding cells on the right, the scrollbar in one (#15)
        height = min(max(0, canvas.height - 2), len(body) + 9)  # a blank row above the list and one below it
        x, y = (canvas.width - width) // 2, (canvas.height - height) // 2
        self.bounds = x, y, width, height
        self.hits = []
        canvas.fill(y, x, height, width)
        canvas.box(y, x, height, width, "SETTINGS", "saved at once · applies at the next grant" if inner >= 60 else "")
        self.small = height < MIN_HEIGHT or width < MIN_WIDTH
        if self.small:
            canvas.put(y + 1, x + 2, "Enlarge terminal · Esc closes", "dim", inner)
            return
        self.visible, self.total = height - 9, len(body)
        if self.reveal:
            lines = [n for n, row in enumerate(body) if row.key == self.focus]
            if lines:
                self.offset = min(self.offset, lines[0])
                self.offset = max(self.offset, lines[-1] - self.visible + 1)
            self.reveal = False
        self.offset = max(0, min(self.offset, max(0, len(body) - self.visible)))
        canvas.scrollbar(y + 2, x + width - 2, self.visible, self.offset, self.visible, self.total, "settings")
        for line, row in enumerate(body[self.offset:self.offset + self.visible], y + 2):
            for column, text, style in row.parts:
                if column < inner - 2:
                    canvas.put(line, x + 2 + column, text, style, inner - 2 - column)
            if row.key:
                self.hits.append((row.key, x + 1, line, width - 2, 1))
                if row.key == self.focus:
                    canvas.select(line, x + 1, x + width - 1)
                    for frame in (x, x + width - 1):  # the selected row on both frames, as in QUEUE (#15)
                        canvas.put(line, frame, "┃", "text")
        rule = y + height - 6
        canvas.put(rule, x + 2, "─" * inner, "frame", inner)
        notes = [(text, "dim") for text in textwrap.wrap(self.description(self.focus), max(1, inner))[:2]]
        if self.flash and (self.clock() - self.flash[2] < FLASH_SECONDS or self.flash[1] == "bad"
                           or (self.flash[1] == "accent" and self.pending)):
            text, style, _ = self.flash
            mark = {"ok": "✓ ", "warn": "! ", "bad": "✗ ", "accent": "… "}.get(style, "")
            notes = notes[:1] + [(text, style) for text in textwrap.wrap(mark + text, max(1, inner))[:2]]
        for line, (text, style) in enumerate(notes[:3], rule + 1):
            canvas.put(line, x + 2, text, style, inner)
        canvas.overlay_footer(y + height - 2, x, width, self.footer())
