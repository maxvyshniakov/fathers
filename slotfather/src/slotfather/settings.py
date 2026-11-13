"""`slotfather settings`: the shared settings as data, for Godfather's Settings panel and any other program (#27).

  slotfather settings --json                               the settings, their values and the queue's STATUS rows
  slotfather settings check KEY VALUE --json               validate without saving: {"ok", "error", "preview"}
  slotfather settings set KEY VALUE [--revision R] --json  save; refused (exit 3) when R no longer matches
  slotfather settings reset (KEY | --all) [--revision R] --json

Every change goes through state.configure(), the same validation, ownership check and queue lock as `slotfather
slots` and `slotfather order`, and applies at the queue's next grant. The labels, checks and previews here are
the ones Godfather's Settings panel shows. Exit codes: 0
done, 2 invalid (the reply says why), 3 conflict (the reply is the current settings), 4 another user's queue.
docs/api.md has the reply shapes.
"""
from __future__ import annotations

import contextlib
import io
import json
import math
import os
import pwd
from typing import Any, cast

from . import config as _config
from . import entry as _entry
from . import schedule as _schedule
from . import state as _state

VERSION = 1

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
# The queue's settings in the panel's order (one flat list, owner, 2026-11-11)
ORDER_FIELDS = ("order", "safety", "fallback", "overrun", "limit", "interactive", "batch", "aging_batch",
                "aging_normal", "warn_wait", "idle_cpu", "idle_after", "yield_after")
NEVER = ("limit", "aging_batch", "aging_normal", "warn_wait")  # 0 is off: shown as "never"
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
}
CAPACITY_PER_CORE = 4  # the most slots a setting saves: this many per core
CAPACITY_KEYS = ("capacity.mode", "capacity.slots", "capacity.gib_per_slot")
EXIT_INVALID, EXIT_CONFLICT, EXIT_READ_ONLY = 2, 3, 4


def number(value: float) -> str:
    return f"{value:g}"


def plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def most_slots() -> int:
    return CAPACITY_PER_CORE * _config.cores()


def parse(key: str, text: str, mode: str) -> tuple[Any, str]:
    """(value, "") for typed text that a numeric setting accepts, else (None, why not, as the panel says it). `key`
    is the panel's: "amount" (the slots in mode fixed, else GiB per slot) or an order knob."""
    try:
        value = _config.parse_duration(text) if key in _config.DURATION_KNOBS else float(text)  # 30m, 1.5h
    except ValueError:
        value = math.nan
    if key == "amount" and mode == "fixed":
        if not (text.isascii() and text.isdigit() and int(text) >= 1):
            return None, "Enter a whole number of slots, at least 1."
        if int(text) > most_slots():
            return None, f"At most {most_slots()} slots ({CAPACITY_PER_CORE} per core)."
        return int(text), ""
    if not math.isfinite(value):
        return None, "Enter a number."
    if key == "amount":
        return (value, "") if value > 0 else (None, "Enter GiB per slot, more than 0.")
    least = _config.ORDER_KNOBS[key][1]
    return (value, "") if value >= least else (None, f"Enter a number of at least {least:g}.")


def capacity_value(mode: str, amount: Any) -> dict[str, Any] | None:
    """What `slotfather slots` would save for the mode and amount; raises SlotError where it would refuse, or where
    it gives more slots than a setting saves."""
    from . import cli
    arguments = ["reset"] if mode == "automatic" else [str(amount)] if mode == "fixed" else [mode, str(amount)]
    with contextlib.redirect_stderr(io.StringIO()):
        found = cli.slots_value(arguments)
    if found and found["slots"] > most_slots():
        raise _config.SlotError(f"{found['slots']} slots is above the most the panel saves, "
                                f"{most_slots()} ({CAPACITY_PER_CORE} per core).")
    return found


def capacity_slots(mode: str, amount: Any) -> int:
    found = capacity_value(mode, amount)
    return int(found["slots"]) if found else _config.automatic_capacity()


def drain_note(slots: int, held: int) -> str:
    drain = max(0, held - slots)
    return f"will drain {drain}: running work keeps its slots" if drain else ""


def starts(slots: int, held: int, waiting: list[int]) -> int:
    """How many waiting requests a capacity of `slots` lets start now by slot counts alone, in queue order."""
    free, started = slots - held, 0
    for count in waiting:
        if 0 < count <= free:
            free, started = free - count, started + 1
    return started


def effect(slots: int, held: int, waiting: list[int]) -> tuple[str, str]:
    """What a capacity of `slots` would do, (text, style): the slots and what drains or starts."""
    drain, start = drain_note(slots, held), starts(slots, held, waiting)
    if drain:
        return f"→ {plural(slots, 'slot')} · {drain}", "warn"
    return f"→ {plural(slots, 'slot')}" + (f" · will start {start} waiting" if start else ""), "dim"


def location() -> str:
    directory, home = str(_config.state_dir()), os.path.expanduser("~")
    return "~" + directory[len(home):] if directory.startswith(home + os.sep) else directory


def filled_basis(basis: dict[str, Any]) -> dict[str, Any]:
    """The capacity basis with the memory budget filled in by the automatic rule where the saved mode does not work
    it out (Fixed), as STATUS' budgets row shows it."""
    from . import platform as _platform
    found = dict(basis)
    if found["memory_slots"] is None and found["mode"] == "fixed":
        gib = _platform.memory_gib()
        found["memory_slots"] = _config.memory_slots(gib, _config.AUTO_GIB_PER_SLOT)
        found["memory_gib"] = None if gib is None else round(gib, 1)
    return found


def status_rows(basis: dict[str, Any], queue: dict[str, Any] | None,
                where: str | None = None) -> list[tuple[str, str, str]]:
    """The STATUS block, (label, value, style): `queue` is {"slots", "free", "used", "waiting"} as the queue's last
    look read them (None: unavailable), `basis` filled_basis(), `where` the queue's directory as shown (location())."""
    rows = [("queue", location() if where is None else where, "text")]
    if queue:
        rows.append(("slots", f"{queue['slots']} total · {queue.get('free', 0)} free · "
                              f"{queue.get('used', 0)} held · {queue.get('waiting', 0)} waiting", "text"))
    else:
        rows.append(("slots", "queue unavailable", "dim"))
    per = number(basis["gib_per_slot"] or 0)
    how = {"fixed": "saved, fixed count", "memory": f"saved, one per {per} GiB of memory",
           "lower": f"saved, lower of CPU and one per {per} GiB",
           "automatic": "automatic, lower of the two budgets"}[basis["mode"]]
    rows.append(("capacity", f"{basis['slots']} slots · {how}", "text"))
    budget = (f"CPU {basis['cpu_slots']} ({basis['cores']} cores) · memory "
              + (f"{basis['memory_slots']}" if basis["memory_slots"] is not None else "unknown")
              + (f" ({number(basis['memory_gib'])} GiB)" if basis["memory_gib"] is not None else ""))
    rows.append(("budgets", budget, "text"))
    drain = max(0, int(queue["used"]) - int(queue["slots"])) if queue else 0
    rows.append(("draining", f"{drain} slots · nothing starts until at most {queue['slots']} are held"
                 if drain and queue else "none", "warn" if drain else "dim"))
    overrides = ["SLOTFATHER_" + name for name in ("CORES", "MEMORY_GIB", "CGROUP") if _config.setting(name)]
    if overrides:
        rows.append(("environment", ", ".join(overrides), "dim"))
    return rows


# The CLI

revision = _state.revision  # an opaque token for a settings file as read


def owner_reason() -> str | None:
    directory = _config.state_dir()
    owner = _state.foreign_owner(directory)
    if owner is None:
        return None
    try:
        name = pwd.getpwuid(owner).pw_name
    except KeyError:
        name = str(owner)
    return f"the queue {directory} belongs to uid {owner} ({name}); only that user can change its settings"


class Reading:
    """The shared settings and the queue as one reply reads them: capacity.json and the basis read until they
    agree, the order settings with and without this environment, and the slots held and waiting."""

    def __init__(self) -> None:
        directory = _config.state_dir()
        files = _state.configuration_bytes(directory)
        for _ in range(3):
            self.basis: dict[str, Any] = _config.capacity_basis()
            again = _state.configuration_bytes(directory)
            if again == files:
                break
            files = again
        self.files = files
        self.saved = _schedule.order_settings(directory, environment=False)
        self.effective = _schedule.order_settings(directory)
        try:
            stored = json.loads(files["order.json"] or b"{}")
        except ValueError:
            stored = {}
        self.stored: set[str] = set(cast(dict[str, Any], stored)) if isinstance(stored, dict) else set()
        self.queue: dict[str, Any] | None = None
        self.held, self.waiting = 0, cast(list[int], [])
        with contextlib.suppress(_config.SlotError, OSError):
            from . import query as _query
            look = _query.look(directory)
            slots = int(look.basis["slots"])
            self.held = _query.held_slots(look.live, look.legacy, slots)
            waiters = [e for e in look.live if not _entry.is_held(e)]
            _schedule.fit(waiters, slots)
            if self.effective["order"] == "backfill":
                import time
                waiters = _schedule.ranked(waiters, self.effective, time.time())
            self.waiting = [int(e.get("count") or 1) for e in waiters]
            self.queue = {"slots": slots, "free": max(0, slots - self.held), "used": self.held,
                          "waiting": len(waiters)}

    def mode(self) -> str:
        return str(self.basis["mode"])

    def amount_for(self, mode: str) -> Any:
        """The amount a mode starts from: the current capacity for Fixed, the GiB per slot for the memory modes."""
        if mode == "fixed":
            return self.basis["slots"]
        return self.basis["gib_per_slot"] or _config.AUTO_GIB_PER_SLOT

    def knob_row(self, key: str, writable: bool) -> dict[str, Any]:
        name = _config.ORDER_KNOBS[key][0] if key != "order" else "QUEUE"
        row: dict[str, Any] = {"key": f"order.{key}", "value": self.saved[key], "default": _config.ORDER_DEFAULTS[key],
                               "writable": writable, "revision": revision(self.files["order.json"]),
                               "description": DESCRIPTIONS[key]}
        if key == "order":
            row.update(label="Queue order", type="choice",
                       choices=[{"value": value, "label": ORDER_LABELS[value]} for value in _config.QUEUE_ORDERS])
        else:
            row.update(label=KNOBS[key][0], type="duration" if key in _config.DURATION_KNOBS else "number",
                       min=_config.ORDER_KNOBS[key][1], max=None, step=KNOBS[key][1], unit=UNITS[key].strip())
            if key in NEVER:
                row["zero"] = "never"
        row["source"] = "saved" if key in self.stored else "default"
        if self.effective[key] != self.saved[key]:
            row.update(effective=self.effective[key], source=f"env SLOTFATHER_{name}")
        return row

    def document(self) -> dict[str, Any]:
        reason = owner_reason()
        writable = reason is None
        token = revision(self.files["capacity.json"])
        source = "saved" if self.basis["saved"] else "default"
        mode = self.mode()
        capacity = [
            {"key": "capacity.mode", "label": "Capacity", "type": "choice", "value": mode, "default": "automatic",
             "choices": [{"value": value, "label": MODE_LABELS[value]} for value in MODES], "source": source,
             "writable": writable, "revision": token, "description": DESCRIPTIONS["mode"]},
            {"key": "capacity.slots", "label": "Slots", "type": "integer", "value": self.amount_for("fixed"),
             "default": None, "min": 1, "max": most_slots(), "step": 1, "unit": "slots", "source": source,
             "writable": writable, "revision": token, "when": {"capacity.mode": ["fixed"]},
             "description": DESCRIPTIONS["amount.fixed"]},
            {"key": "capacity.gib_per_slot", "label": "GiB per slot", "type": "number",
             "value": self.amount_for("memory"), "default": _config.AUTO_GIB_PER_SLOT, "min": 0, "min_exclusive": True,
             "max": None,
             "step": 0.5, "unit": "GiB", "source": source, "writable": writable, "revision": token,
             "when": {"capacity.mode": ["memory", "lower"]}, "description": DESCRIPTIONS["amount.memory"]}]
        basis = filled_basis(self.basis)
        return {"v": VERSION, "tool": "slotfather", "writable": writable, "reason": reason,
                "status": [{"label": label, "value": value, "style": style}
                           for label, value, style in status_rows(basis, self.queue)],
                "basis": basis,
                "sections": [{"id": "capacity", "title": "Capacity", "settings": capacity},
                             {"id": "order", "title": "Queue order",
                              "settings": [self.knob_row(key, writable) for key in ORDER_FIELDS]}]}


class Invalid(_config.SlotError):
    pass


def change(reading: Reading, key: str, text: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """(the configure() change for KEY=text, the check reply) or Invalid with the panel's words for why not."""
    reply: dict[str, Any] = {"ok": True, "error": None, "preview": None}
    if key in CAPACITY_KEYS:
        if key == "capacity.mode":
            if text not in MODES:
                raise Invalid(f"Capacity is one of {', '.join(MODES)}.")
            mode, amount = text, reading.amount_for(text)
        elif key == "capacity.slots":
            mode = "fixed"
            amount, problem = parse("amount", text, mode)
            if problem:
                raise Invalid(problem)
        else:
            mode = reading.mode()
            if mode not in _config.CAPACITY_MODES:
                raise Invalid("GiB per slot applies in the Memory and CPU + memory modes; set capacity.mode first.")
            amount, problem = parse("amount", text, mode)
            if problem:
                raise Invalid(problem)
        try:
            value = capacity_value(mode, amount)
        except _config.SlotError as error:
            raise Invalid(str(error)) from None
        slots = int(value["slots"]) if value else _config.automatic_capacity()
        preview, _ = effect(slots, reading.held, reading.waiting)
        reply.update(preview=preview, slots=slots)
        return {"capacity.json": value}, reply
    knob = key[len("order."):] if key.startswith("order.") else ""
    if knob == "order":
        if text not in _config.QUEUE_ORDERS:
            raise Invalid(f"Queue order is one of {', '.join(_config.QUEUE_ORDERS)}.")
        return {"order.json": {"order": text}}, reply
    if knob not in _config.ORDER_KNOBS:
        raise Invalid(f"no setting {key!r}; `slotfather settings --json` lists them")
    value, problem = parse(knob, text, reading.mode())
    if problem:
        raise Invalid(problem)
    return {"order.json": {knob: value}}, reply


def reset_change(key: str) -> dict[str, Any]:
    if key == "--all":
        return {"capacity.json": None, "order.json": None}
    if key in CAPACITY_KEYS:
        return {"capacity.json": None}
    knob = key[len("order."):] if key.startswith("order.") else ""
    if knob in _config.ORDER_DEFAULTS:
        return {"order.json": {knob: _config.ORDER_DEFAULTS[knob]}}
    raise Invalid(f"no setting {key!r}; `slotfather settings --json` lists them")


def message(document: dict[str, Any], changes: dict[str, Any]) -> str:
    if "capacity.json" in changes and "order.json" in changes:
        return f"Saved · every setting at its default, capacity {document['basis']['slots']} slots"
    if "capacity.json" in changes:
        return f"Saved · capacity {document['basis']['slots']} slots"
    knob = next(iter(cast(dict[str, Any], changes["order.json"])))
    row = next(row for section in document["sections"] for row in section["settings"] if row["key"] == f"order.{knob}")
    value = row["value"]
    shown = ORDER_LABELS.get(str(value), str(value)) if knob == "order" else \
        "never" if knob in NEVER and not value else number(value) + UNITS.get(knob, "")
    return f"Saved · {row['label']} {shown}"


def save(changes: dict[str, Any], revision_wanted: str | None) -> tuple[int, dict[str, Any]]:
    """Applies `changes` through state.configure(); with a revision, only while every file it touches is as read
    then (exit 3 and the current settings otherwise)."""
    if owner_reason() is not None:
        return EXIT_READ_ONLY, {"ok": False, "error": owner_reason()}
    # One revision covers capacity.json (the three capacity keys, and reset --all), another order.json.
    expected = None if revision_wanted is None else \
        {"capacity.json" if "capacity.json" in changes else "order.json": revision_wanted}
    try:
        _state.configure(changes, revisions=expected)
    except _state.Conflict as error:
        document = Reading().document()
        return EXIT_CONFLICT, dict(document, ok=False, conflict=True,
                                   error=f"{error}; now {document['basis']['slots']} slots")
    except _config.SlotError as error:
        return EXIT_INVALID, {"ok": False, "error": str(error)}
    document = Reading().document()
    return 0, dict(document, ok=True, message=message(document, changes))


USAGE = ("settings takes --json, check KEY VALUE --json, set KEY VALUE [--revision R] --json or "
         "reset (KEY | --all) [--revision R] --json")


def command(argv: list[str]) -> int:
    words = list(argv)
    if "--json" not in words:
        raise _config.SlotError(USAGE)
    words.remove("--json")
    wanted: str | None = None
    if "--revision" in words:
        at = words.index("--revision")
        if at + 1 >= len(words):
            raise _config.SlotError(USAGE)
        wanted = words[at + 1]
        del words[at:at + 2]
    code, reply = EXIT_INVALID, cast(dict[str, Any], {})
    try:
        if not words and wanted is None:
            code, reply = 0, Reading().document()
        elif len(words) == 3 and words[0] == "check" and wanted is None:
            _, reply = change(Reading(), words[1], words[2])
            code = 0
        elif len(words) == 3 and words[0] == "set":
            changes, _ = change(Reading(), words[1], words[2])
            code, reply = save(changes, wanted)
        elif len(words) == 2 and words[0] == "reset":
            code, reply = save(reset_change(words[1]), wanted)
        else:
            raise _config.SlotError(USAGE)
    except Invalid as error:
        reply = {"ok": False, "error": str(error), "preview": None} if words[:1] == ["check"] else \
            {"ok": False, "error": str(error)}
    print(json.dumps(reply, sort_keys=True))
    return code
