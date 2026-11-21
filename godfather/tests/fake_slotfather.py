"""A fake `slotfather` for Godfather's tests: SF-1 (capabilities, status --json --full) and SF-2 (settings) over a
JSON state file, with Slotfather's capacity rules and messages as the panel showed them (docs/contracts/slotfather.md),
and `settings --json` field for field as the real one prints it (test_contract.py compares the two).

FAKE_SLOTFATHER_STATE names the state file; every call's argv is appended to FAKE_SLOTFATHER_CALLS. State keys:
cores, memory_gib (null: unknown), capacity (null, {"slots": N} or {"mode", "gib_per_slot", "slots"}), order (the
saved knobs), environment (the SLOTFATHER_* names set), writable, version, snapshot (the status reply to print),
capabilities (to print instead), fail ({"status": "message"} makes that command fail), sleep ({"status": s}),
ignore_hangup (true: a call that outlives the dashboard's terminal), ignore_term (true: one that ignores SIGTERM).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import sys
import time
from typing import Any, cast

CORES_PER_SLOT, AUTO_GIB_PER_SLOT, CAPACITY_PER_CORE = 3, 4.0, 4
# From the default row of the real `slotfather settings --json` in an isolated queue. Per-read fields belong below.
RESERVATION = cast(dict[str, Any], json.loads(
    (Path(__file__).parent / "fixtures" / "slotfather-reservation-timeout.json").read_text()))
ORDER_DEFAULTS = {"order": "backfill", "safety": 3.0, "limit": 0.0, "fallback": 3600.0, "overrun": 0.5,
                  "interactive": 1800.0, "batch": 1800.0, "yield_after": 120.0, "idle_cpu": 0.1, "idle_after": 300.0,
                  "aging_batch": 1800.0, "aging_normal": 3600.0, "warn_wait": 1800.0,
                  "reservation_timeout": float(RESERVATION["default"])}
MINIMUMS = {"safety": 1.0, "limit": 0.0, "fallback": 0.0, "overrun": 0.0, "interactive": 0.0, "batch": 0.0,
            "yield_after": 0.0, "idle_cpu": 0.0, "idle_after": 1.0, "aging_batch": 0.0, "aging_normal": 0.0,
            "warn_wait": 0.0, "reservation_timeout": float(RESERVATION["min"])}
MODES = ("automatic", "fixed", "memory", "lower")
UNKNOWN = "the machine's memory is unknown here; save a count instead (slotfather slots N)"
PATH = os.environ.get("FAKE_SLOTFATHER_STATE", "")


def load() -> dict[str, Any]:
    with open(PATH, encoding="utf-8") as handle:
        state = json.load(handle)
    state.setdefault("cores", 12)
    state.setdefault("memory_gib", 64)
    state.setdefault("capacity", None)
    state.setdefault("order", {})
    state.setdefault("environment", ["SLOTFATHER_CORES", "SLOTFATHER_MEMORY_GIB"])
    state.setdefault("writable", True)
    state.setdefault("version", "0.2.0")
    return state


def save(state: dict[str, Any]) -> None:
    pending = PATH + ".pending"
    with open(pending, "w", encoding="utf-8") as handle:
        json.dump(state, handle)
    os.replace(pending, PATH)


def memory_slots(gib: Any, per_slot: float) -> int | None:
    return None if gib is None else max(1, math.floor(round(gib) / per_slot + 1e-9))


def basis(state: dict[str, Any]) -> dict[str, Any]:
    cores = int(state["cores"])
    cpu = max(1, cores // CORES_PER_SLOT)
    gib = state["memory_gib"]
    found: dict[str, Any] = {"saved": state["capacity"] is not None, "cores": cores, "cpu_slots": cpu,
                             "memory_gib": None if gib is None else round(gib, 1), "gib_per_slot": None,
                             "memory_slots": memory_slots(gib, AUTO_GIB_PER_SLOT)}
    data = state["capacity"] or {"mode": "automatic", "gib_per_slot": AUTO_GIB_PER_SLOT}
    mode = data.get("mode", "fixed")
    if mode == "fixed":
        return {**found, "slots": data["slots"], "mode": "fixed"}
    by_memory = memory_slots(gib, float(data["gib_per_slot"]))
    slots = (data.get("slots") or cpu) if by_memory is None else by_memory if mode == "memory" else min(cpu, by_memory)
    return {**found, "mode": mode, "gib_per_slot": data["gib_per_slot"], "memory_slots": by_memory, "slots": slots}


def revision(state: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(state["capacity"], sort_keys=True).encode()).hexdigest()


def capacity_for(state: dict[str, Any], mode: str, amount: Any) -> dict[str, Any] | None:
    """What a capacity value saves, as `slotfather slots` does; ValueError with Slotfather's message."""
    if mode == "automatic":
        return None
    if mode == "fixed":
        return {"slots": int(amount)}
    per_slot = float(amount)
    per_slot = int(per_slot) if per_slot.is_integer() else per_slot
    by_memory = memory_slots(state["memory_gib"], per_slot)
    if by_memory is None:
        raise ValueError(UNKNOWN)
    cpu = max(1, int(state["cores"]) // CORES_PER_SLOT)
    return {"slots": by_memory if mode == "memory" else min(cpu, by_memory), "mode": mode, "gib_per_slot": per_slot}


def number(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        return math.nan
    return value


def duration(text: str) -> float:
    """The fake's duration values: ordered hour, minute and second terms, with the last unit optional."""
    terms = re.split(r"([hms])", text.strip())
    parts = list(zip(terms[::2], terms[1::2]))
    if terms[-1].strip():
        parts.append((terms[-1], "s"))
    units = [unit for _, unit in parts]
    if not parts or units != [unit for unit in "hms" if unit in units]:
        return math.nan
    if any(not re.fullmatch(r"\d+(?:\.\d+)?", value.strip()) for value, _ in parts):
        return math.nan
    seconds = {"h": 3600, "m": 60, "s": 1}
    return sum(float(value) * seconds[unit] for value, unit in parts)


def validate(state: dict[str, Any], key: str, text: str) -> tuple[Any, dict[str, Any] | None]:
    """(value, the capacity it saves for a capacity key); ValueError with the panel's wording."""
    current = basis(state)
    most = CAPACITY_PER_CORE * int(state["cores"])
    if key == "capacity.mode":
        if text not in MODES:
            raise ValueError(f"Choose one of {', '.join(MODES)}.")
        amount = current["slots"] if text == "fixed" else current["gib_per_slot"] or AUTO_GIB_PER_SLOT
        return text, capacity_for(state, text, amount)
    if key == "capacity.slots":
        if not (text.isascii() and text.isdigit() and int(text) >= 1):
            raise ValueError("Enter a whole number of slots, at least 1.")
        if int(text) > most:
            raise ValueError(f"At most {most} slots ({CAPACITY_PER_CORE} per core).")
        return int(text), capacity_for(state, "fixed", int(text))
    if key == "capacity.gib_per_slot":
        value = number(text)
        if not math.isfinite(value):
            raise ValueError("Enter a number.")
        if value <= 0:
            raise ValueError("Enter GiB per slot, more than 0.")
        mode = current["mode"] if current["mode"] in ("memory", "lower") else "memory"
        return value, capacity_for(state, mode, value)
    name = key[len("order."):] if key.startswith("order.") else ""
    if name == "order":
        if text not in ("backfill", "window"):
            raise ValueError("Choose backfill or window.")
        return text, None
    if name not in MINIMUMS:
        raise KeyError(key)
    kind = RESERVATION["type"] if name == "reservation_timeout" else KNOBS[name][2]
    value = duration(text) if kind == "duration" else number(text)
    if not math.isfinite(value):
        raise ValueError("Enter a number.")
    if value < MINIMUMS[name]:
        raise ValueError(f"Enter a number of at least {MINIMUMS[name]:g}.")
    return value, None


# What `slotfather settings --json` prints (slotfather/src/slotfather/settings.py), field for field
MODE_LABELS = {"automatic": "Automatic", "fixed": "Fixed", "memory": "Memory", "lower": "CPU + memory"}
KNOBS = {"safety": ("Backfill margin", 0.5, "number", "×"), "fallback": ("Unknown job runtime", 60, "duration", "s"),
         "overrun": ("Overrun estimate", 0.1, "number", "×"), "limit": ("Overtaking stops after", 60, "duration", "s"),
         "interactive": ("Interactive head start", 60, "duration", "s"),
         "batch": ("Batch held back", 60, "duration", "s"),
         "aging_batch": ("Batch promoted after", 300, "duration", "s"),
         "aging_normal": ("Normal promoted after", 300, "duration", "s"),
         "warn_wait": ("Warn after waiting", 300, "duration", "s"),
         "idle_cpu": ("Idle when below", 0.05, "number", "cores"),
         "idle_after": ("Idle for", 60, "duration", "s"), "yield_after": ("Ask to yield after", 30, "duration", "s")}
NEVER = ("limit", "aging_batch", "aging_normal", "warn_wait")
ORDER_FIELDS = ("order", "safety", "fallback", "overrun", "limit", "interactive", "batch", "aging_batch",
                "aging_normal", "warn_wait", "reservation_timeout", "idle_cpu", "idle_after", "yield_after")  # the panel's order
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
def effect(slots: int, state: dict[str, Any]) -> str:
    """What a capacity of `slots` would do, as Slotfather's settings.effect() says it from its queue: `held` slots
    held and `waiting` the waiting requests' slot counts in queue order (state keys; none by default)."""
    held, waiting = int(state.get("held", 0)), [int(count) for count in state.get("waiting", [])]
    drain = max(0, held - slots)
    free, started = slots - held, 0
    for count in waiting:
        if 0 < count <= free:
            free, started = free - count, started + 1
    found = f"→ {slots} slot{'' if slots == 1 else 's'}"
    if drain:
        return found + f" · will drain {drain}: running work keeps its slots"
    return found + (f" · will start {started} waiting" if started else "")


FOREIGN = "the queue /fake/state belongs to uid 502 (other); only that user can change its settings"


def order_revision(state: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(state["order"], sort_keys=True).encode()).hexdigest()


def settings(state: dict[str, Any]) -> dict[str, Any]:
    found = basis(state)
    rev, order_rev = revision(state), order_revision(state)
    writable = bool(state["writable"])
    effective_order = os.environ.get("SLOTFATHER_QUEUE") or None
    knobs: list[dict[str, Any]] = []
    for name in ORDER_FIELDS:
        default = ORDER_DEFAULTS[name]
        value = state["order"].get(name, default)
        row: dict[str, Any] = {"key": "order." + name, "value": value, "default": default,
                               "writable": writable, "revision": order_rev, "description": DESCRIPTIONS.get(name, ""),
                               "source": "saved" if name in state["order"] else "default"}
        if name == "order":
            row.update(label="Queue order", type="choice",
                       choices=[{"value": "backfill", "label": "Backfill"}, {"value": "window", "label": "Arrival"}])
            if effective_order in ("backfill", "window") and effective_order != value:
                row.update(effective=effective_order, source="env SLOTFATHER_QUEUE")
        elif name == "reservation_timeout":
            row.update(RESERVATION)
        else:
            label, step, kind, unit = KNOBS[name]
            row.update(label=label, type=kind, min=MINIMUMS[name], max=None, step=step, unit=unit)
            if name in NEVER:
                row["zero"] = "never"
        knobs.append(row)
    source = "saved" if found["saved"] else "default"
    capacity = [
        {"key": "capacity.mode", "label": "Capacity", "type": "choice", "value": found["mode"], "default": "automatic",
         "choices": [{"value": mode, "label": MODE_LABELS[mode]} for mode in MODES], "source": source,
         "writable": writable, "revision": rev, "description": DESCRIPTIONS["mode"]},
        {"key": "capacity.slots", "label": "Slots", "type": "integer", "value": found["slots"], "default": None,
         "min": 1, "max": CAPACITY_PER_CORE * int(state["cores"]), "step": 1, "unit": "slots", "source": source,
         "writable": writable, "revision": rev, "when": {"capacity.mode": ["fixed"]},
         "description": DESCRIPTIONS["amount.fixed"]},
        {"key": "capacity.gib_per_slot", "label": "GiB per slot", "type": "number",
         "value": found["gib_per_slot"] or AUTO_GIB_PER_SLOT, "default": AUTO_GIB_PER_SLOT, "min": 0,
         "min_exclusive": True, "max": None, "step": 0.5, "unit": "GiB", "source": source, "writable": writable,
         "revision": rev, "when": {"capacity.mode": ["memory", "lower"]}, "description": DESCRIPTIONS["amount.memory"]}]
    per = f"{found['gib_per_slot'] or 0:g}"
    how = {"fixed": "saved, fixed count", "memory": f"saved, one per {per} GiB of memory",
           "lower": f"saved, lower of CPU and one per {per} GiB",
           "automatic": "automatic, lower of the two budgets"}[found["mode"]]
    memory = (f"{found['memory_slots']}" if found["memory_slots"] is not None else "unknown") + \
        (f" ({found['memory_gib']:g} GiB)" if found["memory_gib"] is not None else "")
    status = [{"label": "queue", "value": state.get("directory", "~/.local/state/slotfather"), "style": "text"},
              {"label": "slots", "value": f"{found['slots']} total · {found['slots']} free · 0 held · 0 waiting",
               "style": "text"},
              {"label": "capacity", "value": f"{found['slots']} slots · {how}", "style": "text"},
              {"label": "budgets", "value": f"CPU {found['cpu_slots']} ({found['cores']} cores) · memory {memory}",
               "style": "text"},
              {"label": "draining", "value": "none", "style": "dim"}]
    if state["environment"]:
        status.append({"label": "environment", "value": ", ".join(state["environment"]), "style": "dim"})
    return {"v": 1, "tool": "slotfather", "writable": writable, "reason": None if writable else FOREIGN,
            "status": status, "basis": dict(found, cgroup=None),
            "sections": [{"id": "capacity", "title": "Capacity", "settings": capacity},
                         {"id": "order", "title": "Queue order", "settings": knobs}]}


def idle_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    found = basis(state)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {"v": 2, "host": "fake", "time": now, "slots": found["slots"], "free": found["slots"], "held": 0,
            "draining": 0, "dashboard": {
                "v": 1, "directory": "/fake/state", "slots": found["slots"], "saved": found["saved"], "basis": found,
                "cores": state["cores"], "averages": [1.0, 1.0, 1.0], "draining": 0,
                "advice": {"unleased_load": 1.0}, "holders": [], "waiters": [], "used": 0, "free": found["slots"],
                "memory_block": None, "admission": found["slots"], "order": "backfill", "start": None,
                "reserved": None, "limit": 0, "memory": {"total": 65536, "used": 20000, "swap_total": 0,
                                                         "swap_free": 0, "low": False, "swap_low": False},
                "history": [], "events": [], "updated": now, "overtake_window": 600}}


def reply(value: Any, code: int = 0) -> int:
    print(json.dumps(value))
    return code


def host() -> str:
    """This machine's short name as the tools write it, so Godfather reads the line as its own host's."""
    import re
    import socket

    name = re.sub(r"[^a-z0-9-]", "-", socket.gethostname().split(".")[0].lower()).strip("-")[:63]
    return name.strip("-") or "localhost"


def action(state: dict[str, Any], argv: list[str]) -> int:
    """`reprioritize PID CLASS --start START` and `ask-yield PID --start START` on the snapshot's rows, Slotfather's
    way: the pid and start must name a row, the change is saved in the snapshot, the event goes to the event log
    with `actor.via` from GODFATHER_ACTOR (`godfather/<version>` gives `godfather`)."""
    import datetime

    start = argv[argv.index("--start") + 1] if "--start" in argv else None
    pid = int(argv[1])
    snapshot = cast(dict[str, Any], state.get("snapshot") or {})
    block = cast(dict[str, Any], snapshot.get("dashboard") or {})
    found = cast(list[dict[str, Any]], block.get("holders", [])) + cast(list[dict[str, Any]], block.get("waiters", []))
    rows = [row for row in found if row.get("pid") == pid]
    if not rows or rows[0].get("start") != start:
        print(f"slotfather {argv[0]}: pid {pid} is not that request any more", file=sys.stderr)
        return 1
    row = rows[0]
    details: dict[str, Any]
    actor = os.environ.get("GODFATHER_ACTOR", "")
    if argv[0] == "reprioritize":
        kind, details = "request.reprioritized", {"from": row.get("priority"), "to": argv[2]}
        row["priority"] = argv[2]
        print(f"{row.get('label')} (pid {pid}, held): {argv[2]}")
    else:
        kind, details = "lease.yield_asked", {"for": "the waiter"}
        row["asked"], row["asked_at"] = "the waiter", row.get("granted")
        print(f"{row.get('label')} (pid {pid}): asked to yield 1 slot(s) for the waiter")
    save(state)
    now = datetime.datetime.now(datetime.timezone.utc)
    event: dict[str, Any] = {"v": 1, "time": now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z",
             "host": host(), "source": "slotfather", "kind": kind,
             "subject": {"type": "request", "id": f"r-{pid}", "label": row.get("label"), "pid": pid},
             "actor": {"type": "process", "pid": os.getpid(), **({"via": actor.split("/")[0]} if actor else {})},
             "details": details}
    with open(os.path.join(os.path.dirname(os.path.abspath(PATH)), "events.jsonl"), "a", encoding="utf-8") as log:
        log.write(json.dumps(event) + "\n")
    with open(PATH + ".env", "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"argv": argv, "GODFATHER_ACTOR": actor}) + "\n")
    return 0


def main(argv: list[str]) -> int:
    calls = os.environ.get("FAKE_SLOTFATHER_CALLS")
    if calls:
        with open(calls, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(argv) + "\n")
    state = load()
    command = argv[0] if argv else ""
    if state.get("ignore_hangup"):  # a call that outlives the dashboard that ran it, as one under nohup does
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    if state.get("ignore_term"):  # one that also ignores SIGTERM: only SIGKILL ends it
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if command in state.get("sleep", {}):
        time.sleep(float(state["sleep"][command]))
    if command in state.get("fail", {}):
        print(f"slotfather: {state['fail'][command]}", file=sys.stderr)
        return 1
    if argv == ["capabilities", "--json"]:
        return reply(state.get("capabilities") or {
            "v": 1, "tool": "slotfather", "version": state["version"],
            "dashboard": {"v": 1, "tag": "slot",
                          "snapshot": {"argv": ["status", "--json", "--full"], "versions": [2], "poll": 1},
                          "settings": {"versions": [1]}, "actions": state.get("actions") or ["reprioritize"],
                          # EVENTS' log (fathers#84), beside the state file: tests' LiveCase.queue() writes it
                          "events": {"path": os.path.join(os.path.dirname(os.path.abspath(PATH)), "events.jsonl"),
                                     "versions": [1]}}})
    if argv == ["status", "--json", "--full"]:
        return reply(state.get("snapshot") or idle_snapshot(state))
    if command in ("reprioritize", "ask-yield"):  # fathers#88: as Slotfather does, its event's actor.via from
        return action(state, argv)               # GODFATHER_ACTOR
    if command != "settings" or argv[-1:] != ["--json"]:
        print("fake slotfather: unknown command " + " ".join(argv), file=sys.stderr)
        return 2
    args = argv[1:-1]
    if not args:
        return reply(settings(state))
    if args[0] == "check" and len(args) == 3:
        try:
            _, capacity = validate(state, args[1], args[2])
        except (ValueError, KeyError) as error:
            return reply({"ok": False, "error": str(error).strip("'"), "preview": None}, 2)
        answer: dict[str, Any] = {"ok": True, "error": None, "preview": None}
        if args[1].startswith("capacity."):
            answer["slots"] = basis(dict(state, capacity=capacity))["slots"] if capacity else \
                basis(dict(state, capacity=None))["slots"]
            answer["preview"] = effect(answer["slots"], state)
        return reply(answer)
    expected = args[args.index("--revision") + 1] if "--revision" in args else None
    args = [arg for index, arg in enumerate(args) if arg != "--revision" and (index == 0 or args[index - 1] != "--revision")]
    if not state["writable"]:
        return reply({"ok": False, "error": FOREIGN}, 4)
    if args[0] == "set" and len(args) == 3:
        key = args[1]
        current = revision(state) if key.startswith("capacity.") else order_revision(state)
        if expected is not None and expected != current:
            return reply(dict(settings(state), ok=False, conflict=True,
                              error=f"settings changed since they were read; now {basis(state)['slots']} slots"), 3)
        try:
            value, capacity = validate(state, key, args[2])
        except (ValueError, KeyError) as error:
            return reply({"ok": False, "error": str(error).strip("'")}, 2)
        if key.startswith("capacity."):
            state["capacity"] = capacity
        else:
            state["order"][key[len("order."):]] = value
        save(state)
        return reply(dict(settings(state), ok=True))
    if args[0] == "reset" and len(args) == 2:
        if args[1] == "--all":
            state["capacity"], state["order"] = None, {}
        elif args[1].startswith("capacity."):
            if expected is not None and expected != revision(state):
                return reply(dict(settings(state), ok=False, conflict=True), 3)
            state["capacity"] = None
        else:
            if expected is not None and expected != order_revision(state):
                return reply(dict(settings(state), ok=False, conflict=True), 3)
            state["order"][args[1][len("order."):]] = ORDER_DEFAULTS[args[1][len("order."):]]
        save(state)
        return reply(settings(state))
    print("fake slotfather: unknown settings command " + " ".join(argv), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
