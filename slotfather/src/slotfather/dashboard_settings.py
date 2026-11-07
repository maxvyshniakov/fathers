"""Draft settings for one dashboard; shared writes use the CLI's state boundary."""
from __future__ import annotations

import contextlib
import io
import math
import textwrap
from typing import TYPE_CHECKING, Any, cast

from . import cli, config, schedule, state

if TYPE_CHECKING:
    from .dashboard import Canvas

MODES = ("automatic", "fixed", "memory", "lower")
MODE_LABELS = {"automatic": "Automatic", "fixed": "Fixed", "memory": "Memory", "lower": "CPU + memory"}
KNOBS = {"safety": ("Safety multiplier", 0.5), "limit": ("Overtaking limit (s)", 60),
         "fallback": ("Unknown runtime (s)", 60), "overrun": ("Overrun factor", 0.1),
         "interactive": ("Interactive advance (s)", 60), "batch": ("Batch delay (s)", 60),
         "yield_after": ("Yield after (s)", 30)}
DESCRIPTIONS = {
    "mode": "Shared capacity for every client. Automatic uses the lower CPU and memory budget.",
    "amount": "Fixed: total slots. Memory modes: GiB per slot. Capacity changes require an idle queue.",
    "interval": "Refresh interval in seconds, 0.1–60. Local to this dashboard; no shared file changes.",
    "advanced": "Queue settings are shared and take effect at the next queue check, including while busy.",
    "order": "Backfill lets predicted short work pass. Window uses arrival order and its overtaking window.",
    "safety": "Expected runtime multiplier for backfill. Minimum 1; higher is more conservative.",
    "limit": "Overtaking limit in seconds. 0 means no limit.",
    "fallback": "Assumed runtime in seconds for work with no history.",
    "overrun": "Expected remaining runtime as a share of elapsed time after a request overruns.",
    "interactive": "Move interactive requests this many seconds earlier in the queue. 0 disables the advance.",
    "batch": "Move batch requests this many seconds later in the queue. 0 disables the delay.",
    "yield_after": "Cooperative yield delay in seconds. 0 disables yield requests; no process is stopped.",
    "apply": "Apply the draft and close. Shared settings are checked again under the queue lock.",
    "defaults": "Reset this draft to automatic capacity, default queue policy and 1-second refresh. Apply to save.",
    "reload": "Discard the draft and read the latest shared settings. The local refresh interval is retained.",
    "cancel": "Close without applying this draft.",
}


class Settings:
    def __init__(self, interval: float):
        self.focus = "mode"
        self.advanced = False
        self.editing = False
        self.buffer = ""
        self.replace = True
        self.offset = 0
        self.visible = 0
        self.total = 0
        self.reveal = True
        self.message = ""
        self.busy = ""
        self.reset_order = False
        self.hits: list[tuple[str, int, int, int, int]] = []
        self.bounds = (0, 0, 0, 0)
        self.interval = interval
        self.values: dict[str, str] = {}
        self.original: dict[str, str] = {}
        self.expected: dict[str, bytes | None] = {}
        self.basis: dict[str, Any] = {}
        self.effective: dict[str, Any] = {}
        self.reload()

    def reload(self) -> None:
        directory = config.state_dir()
        for _ in range(3):
            before = state.configuration_bytes(directory)
            basis: dict[str, Any] = cast(Any, config).capacity_basis()
            saved: dict[str, Any] = cast(Any, schedule).order_settings(environment=False)
            effective: dict[str, Any] = cast(Any, schedule).order_settings()
            if before == state.configuration_bytes(directory):
                break
        else:
            raise config.SlotError("Settings keep changing. Try opening Settings again.")
        self.expected, self.basis, self.effective = before, basis, effective
        self.values = {"mode": basis["mode"],
                       "amount": f"{basis['slots'] if basis['mode'] == 'fixed' else basis['gib_per_slot'] or config.AUTO_GIB_PER_SLOT:g}",
                       "interval": f"{self.interval:g}",
                       **{key: str(saved[key]) for key in config.ORDER_DEFAULTS}}
        self.original = dict(self.values)
        self.message = ""
        self.editing = False
        self.reset_order = False

    def capacity_changed(self) -> bool:
        return (self.values["mode"] != self.original["mode"]
                or self.values["mode"] != "automatic" and self.values["amount"] != self.original["amount"])

    def apply(self) -> float:
        try:
            interval = float(self.values["interval"])
        except ValueError:
            interval = math.nan
        if not 0.1 <= interval <= 60:
            raise config.SlotError("Refresh interval must be from 0.1 to 60 seconds.")
        changes: dict[str, dict[str, Any] | None] = {}
        if self.capacity_changed():
            mode, amount = self.values["mode"], self.values["amount"]
            arguments = ["reset"] if mode == "automatic" else [amount] if mode == "fixed" else [mode, amount]
            with contextlib.redirect_stderr(io.StringIO()):
                changes["capacity.json"] = cli.slots_value(arguments)
        if self.reset_order or any(self.values[key] != self.original[key] for key in config.ORDER_DEFAULTS):
            try:
                order = schedule.checked_order({
                    key: self.values[key] if key == "order" else float(self.values[key])
                    for key in config.ORDER_DEFAULTS})
                if self.reset_order and order == config.ORDER_DEFAULTS:
                    if self.expected["order.json"] is not None:
                        changes["order.json"] = None
                else:
                    changes["order.json"] = order
            except ValueError as error:
                raise config.SlotError(str(error)) from None
        state.configure(changes, expected=self.expected)
        self.interval = interval
        return interval

    def fields(self) -> list[str]:
        return (["mode"] + (["amount"] if self.values["mode"] != "automatic" else [])
                + ["interval", "advanced"] + (["order", *KNOBS] if self.advanced else [])
                + ["apply", "defaults", "reload", "cancel"])

    def move(self, count: int) -> None:
        keys = self.fields()
        self.focus = keys[(keys.index(self.focus) + count) % len(keys)]
        self.reveal = True

    def change(self, direction: int) -> None:
        if self.focus == "mode":
            mode = MODES[(MODES.index(self.values["mode"]) + direction) % len(MODES)]
            self.values["mode"] = mode
            self.values["amount"] = (str(self.basis["slots"]) if mode == "fixed"
                                     else f"{self.basis['gib_per_slot'] or config.AUTO_GIB_PER_SLOT:g}")
        elif self.focus == "advanced":
            self.advanced = not self.advanced
        elif self.focus == "order":
            self.values["order"] = "window" if self.values["order"] == "backfill" else "backfill"
        elif self.focus in self.values:
            minimum = (0.1 if self.focus == "interval" else 1 if self.focus == "amount" and self.values["mode"] == "fixed"
                       else 0.1 if self.focus == "amount" else config.ORDER_KNOBS[self.focus][1])
            step = KNOBS[self.focus][1] if self.focus in KNOBS else 1 if self.focus == "interval" else 0.5
            if self.focus == "amount" and self.values["mode"] == "fixed":
                step = 1
            try:
                value = float(self.values[self.focus])
                if not math.isfinite(value):
                    value = minimum
            except ValueError:
                value = minimum
            value = max(minimum, round(value + direction * step, 3))
            self.values[self.focus] = f"{min(60, value) if self.focus == 'interval' else value:g}"
        self.message = ""
        self.reveal = True

    def activate(self) -> str:
        if self.focus == "cancel":
            return "close"
        if self.focus == "reload":
            try:
                self.reload()
            except (config.SlotError, OSError) as error:
                self.message = str(error)
            return "reload"
        if self.focus == "defaults":
            self.values.update(mode="automatic", amount="4", interval="1",
                               **{key: str(value) for key, value in config.ORDER_DEFAULTS.items()})
            self.reset_order = True
        elif self.focus == "apply":
            try:
                if self.capacity_changed() and self.busy:
                    raise config.SlotError(self.busy)
                self.apply()
            except (config.SlotError, OSError) as error:
                self.message = str(error)
                return "handled"
            return "applied"
        elif self.focus in ("mode", "order", "advanced"):
            self.change(1)
        else:
            self.editing, self.replace, self.buffer = True, True, self.values[self.focus]
        return "handled"

    def preview(self) -> str:
        try:
            mode, amount = self.values["mode"], self.values["amount"]
            with contextlib.redirect_stderr(io.StringIO()):
                if mode == "automatic":
                    # Reset removes a saved cgroup: sample the caller's budget, just as a new automatic queue does.
                    try:
                        value = (cli.slots_value(["lower", str(config.AUTO_GIB_PER_SLOT)]) or {})["slots"]
                    except config.SlotError:  # Automatic sizing falls back to CPU when memory is unavailable.
                        value = max(1, config.cores() // config.CORES_PER_SLOT)
                else:
                    value = (cli.slots_value([amount] if mode == "fixed" else [mode, amount]) or {})["slots"]
            return f"Draft capacity: {value} slots"
        except (config.SlotError, OSError):
            return "Draft capacity: invalid or unavailable value"

    def key(self, key: str) -> str:
        if self.editing:
            if key == "esc":
                return "close"
            elif key in ("\r", "\n", "\t", "\x13"):
                self.values[self.focus] = self.buffer
                self.editing = False
                if key == "\t":
                    self.move(1)
                elif key == "\x13":
                    self.focus = "apply"
                    return self.activate()
            elif key in ("\x7f", "\b"):
                self.buffer = "" if self.replace else self.buffer[:-1]
                self.replace = False
            elif len(key) == 1 and key in "0123456789.eE+-" and len(self.buffer) < 32:
                self.buffer = key if self.replace else self.buffer + key
                self.replace = False
            return "handled"
        if key in ("esc", "s"):
            return "close"
        if key in ("q", "Q"):
            return "quit"
        if key in ("down", "j", "\t"):
            self.move(1)
        elif key in ("up", "k", "back_tab"):
            self.move(-1)
        elif key in ("home", "g", "end", "G"):
            self.focus = self.fields()[0 if key in ("home", "g") else -1]
            self.reveal = True
        elif key in ("left", "right", "-", "+"):
            self.change(-1 if key in ("left", "-") else 1)
        elif key in ("\r", "\n", " "):
            return self.activate()
        elif key == "\x13":
            self.focus = "apply"
            return self.activate()
        elif key == "r":
            self.focus = "reload"
            return self.activate()
        elif key in ("page_up", "page_down"):
            self.offset = max(0, min(max(0, self.total - self.visible),
                                     self.offset + (1 if key == "page_down" else -1) * max(1, self.visible - 1)))
            self.reveal = False
        return "handled"

    def rows(self, model: dict[str, Any] | None) -> list[tuple[str, str, str]]:
        rows = [("", "SHARED MACHINE", "title"), ("", str(config.state_dir()), "dim")]
        if model:
            rows.append(("", f"Total {model['slots']} · Free {model.get('free', '?')} · Admission {model.get('admission', '?')}", "dim"))
        rows.append(("mode", "Capacity mode:  " + MODE_LABELS[self.values["mode"]], "text"))
        if self.values["mode"] != "automatic":
            rows.append(("amount", ("Total slots:  " if self.values["mode"] == "fixed" else "GiB per slot:  ")
                         + self.values["amount"], "text"))
        rows.append(("", self.preview(), "dim"))
        source = "saved" if self.basis["saved"] else "automatic default"
        rows.append(("", f"Capacity source: {source} · CPU budget {self.basis['cpu_slots']} slots", "dim"))
        if self.busy:
            rows.append(("", self.busy, "warn"))
        overrides = ["SLOTFATHER_" + name for name in ("CORES", "MEMORY_GIB", "CGROUP") if config.setting(name)]
        if overrides:
            rows.append(("", "Environment: " + ", ".join(overrides), "dim"))
        rows.extend([("", "", "text"), ("", "THIS DASHBOARD", "title"),
                     ("interval", "Refresh interval (s):  " + self.values["interval"], "text"),
                     ("advanced", "Advanced queue settings:  " + ("Hide" if self.advanced else "Show"), "text")])
        if self.advanced:
            effective_order = self.effective["order"]
            annotation = f" · effective {effective_order.capitalize()}" if effective_order != self.original["order"] else ""
            rows.extend([("", "Shared policy · changes also affect a busy queue", "dim"),
                         ("order", "Queue order:  " + self.values["order"].capitalize() + annotation, "text")])
            for key, (label, _) in KNOBS.items():
                value = self.values[key]
                effective = str(self.effective[key])
                annotation = f" · effective {effective}" if effective != self.original[key] else ""
                rows.append((key, label + ":  " + value + annotation, "text"))
            rows.append(("", "Saved policy is shared; each client's environment can only tighten it.", "dim"))
        return rows

    def draw(self, canvas: Canvas, model: dict[str, Any] | None) -> None:
        width = min(max(0, canvas.width - 2), 78)
        height = min(max(0, canvas.height - 2), 32 if self.advanced else 27)
        x, y = (canvas.width - width) // 2, (canvas.height - height) // 2
        self.bounds = x, y, width, height
        self.hits = []
        canvas.fill(y, x, height, width)
        canvas.box(y, x, height, width, "SETTINGS")
        inner = max(0, width - 4)
        if height < 12 or width < 30:
            canvas.put(y + 1, x + 2, "Enlarge terminal · Esc closes", "dim", inner)
            return
        holders, waiters = (len(model.get("holders", [])), len(model.get("waiters", []))) if model else (0, 0)
        self.busy = (f"Capacity locked: {holders} running, {waiters} waiting. Reload when idle."
                     if holders or waiters else "")
        canvas.put(y + 1, x + 2, "Draft changes · Apply to save", "dim", inner)
        lines: list[tuple[str, str, str]] = []
        for key, text, style in self.rows(model):
            if key == self.focus and self.editing:
                text = text.rsplit(":  ", 1)[0] + ":  [" + self.buffer + "|]"
            for wrapped in textwrap.wrap(text, max(1, inner - 2), break_on_hyphens=False) or [""]:
                lines.append((key, wrapped, style))
        self.visible, self.total = max(0, height - 10), len(lines)
        if self.reveal:
            positions = [i for i, (key, _, _) in enumerate(lines) if key == self.focus]
            if positions:
                self.offset = min(self.offset, positions[0])
                self.offset = max(self.offset, positions[-1] - self.visible + 1)
            self.reveal = False
        self.offset = max(0, min(self.offset, max(0, len(lines) - self.visible)))
        if self.offset:
            canvas.put(y + 2, x + 2, f"↑ {self.offset} above", "dim", inner)
        for row, (key, text, style) in enumerate(lines[self.offset:self.offset + self.visible], y + 3):
            canvas.put(row, x + 2, ("▶ " if key == self.focus else "  ") + text, style, inner)
            if key:
                self.hits.append((key, x + 1, row, width - 2, 1))
                if key == self.focus:
                    canvas.select(row, x + 1, x + width - 1)
        below = max(0, len(lines) - self.offset - self.visible)
        if below:
            canvas.put(y + height - 7, x + 2, f"↓ {below} more", "dim", inner)
        description = self.message or DESCRIPTIONS[self.focus]
        for row, text in enumerate(textwrap.wrap(description, max(1, inner))[:3 if self.message else 2], y + height - 6):
            canvas.put(row, x + 2, text, "bad" if self.message else "dim", inner)
        hint = ("↑↓/Tab select · ←→ change · Enter edit · Ctrl-S apply · Esc cancel" if inner >= 66
                else "Tab select · Enter edit · ^S apply · Esc cancel" if inner >= 47 else "Tab · Enter · ^S · Esc")
        canvas.put(y + height - 3, x + 2, hint, "key", inner)
        column = x + 2
        labels = ("Apply", "Defaults", "Reload", "Cancel") if inner >= 43 else ("OK", "Reset", "Read", "X")
        for key, label in zip(("apply", "defaults", "reload", "cancel"), labels):
            text = f"[{label}]"
            if column + len(text) > x + width - 2:
                break
            canvas.put(y + height - 2, column, text, "dim" if key == "apply" and self.capacity_changed() and self.busy else "key")
            self.hits.append((key, column, y + height - 2, len(text), 1))
            if key == self.focus:
                canvas.select(y + height - 2, column, column + len(text))
            column += len(text) + 2

    def mouse(self, event: tuple[str, int, int, int, bool]) -> str:
        _, button, mx, my, pressed = event
        x, y, width, height = self.bounds
        if not pressed or not (x < mx < x + width - 1 and y < my < y + height - 1):
            return "handled"
        code = button & ~28
        if code in (64, 65):
            self.offset = max(0, min(max(0, self.total - self.visible), self.offset + (3 if code == 65 else -3)))
            self.reveal = False
        elif code == 0:
            for key, left, top, columns, rows in self.hits:
                if left <= mx < left + columns and top <= my < top + rows:
                    if self.editing:
                        self.values[self.focus] = self.buffer
                        self.editing = False
                    self.focus = key
                    return self.activate()
        return "handled"
