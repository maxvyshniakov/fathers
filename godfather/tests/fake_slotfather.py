"""A fake `slotfather` for Godfather's tests: SF-1 (capabilities, status --json --full) and SF-2 (settings) over a
JSON state file, with Slotfather's capacity rules and messages as the panel showed them (docs/contracts/slotfather.md).

FAKE_SLOTFATHER_STATE names the state file; every call's argv is appended to FAKE_SLOTFATHER_CALLS. State keys:
cores, memory_gib (null: unknown), capacity (null, {"slots": N} or {"mode", "gib_per_slot", "slots"}), order (the
saved knobs), environment (the SLOTFATHER_* names set), writable, version, snapshot (the status reply to print),
capabilities (to print instead), fail ({"status": "message"} makes that command fail), sleep ({"status": s}).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import time
from typing import Any

CORES_PER_SLOT, AUTO_GIB_PER_SLOT, CAPACITY_PER_CORE = 3, 4, 4
ORDER_DEFAULTS = {"order": "backfill", "safety": 3.0, "limit": 0.0, "fallback": 3600.0, "overrun": 0.5,
                  "interactive": 1800.0, "batch": 1800.0, "yield_after": 120.0, "idle_cpu": 0.1, "idle_after": 300.0,
                  "aging_batch": 1800.0, "aging_normal": 3600.0, "warn_wait": 1800.0}
MINIMUMS = {"safety": 1.0, "limit": 0.0, "fallback": 0.0, "overrun": 0.0, "interactive": 0.0, "batch": 0.0,
            "yield_after": 0.0, "idle_cpu": 0.0, "idle_after": 1.0, "aging_batch": 0.0, "aging_normal": 0.0,
            "warn_wait": 0.0}
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
    by_memory = memory_slots(gib, data["gib_per_slot"])
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
    value = number(text)
    if not math.isfinite(value):
        raise ValueError("Enter a number.")
    if value < MINIMUMS[name]:
        raise ValueError(f"Enter a number of at least {MINIMUMS[name]:g}.")
    return value, None


def settings(state: dict[str, Any]) -> dict[str, Any]:
    found = basis(state)
    rev = revision(state)
    effective_order = os.environ.get("SLOTFATHER_QUEUE") or None
    knobs = []
    for name, default in ORDER_DEFAULTS.items():
        value = state["order"].get(name, default)
        row: dict[str, Any] = {"key": "order." + name, "value": value, "default": default, "source": "saved",
                               "writable": True, "revision": "sha256:order"}
        if name == "order":
            row.update(type="choice", choices=[{"value": "backfill"}, {"value": "window"}])
            if effective_order in ("backfill", "window"):
                row["effective"] = effective_order
        else:
            row.update(type="number", min=MINIMUMS[name])
        knobs.append(row)
    capacity = [
        {"key": "capacity.mode", "type": "choice", "value": found["mode"], "default": "automatic",
         "choices": [{"value": mode} for mode in MODES], "revision": rev, "writable": True, "source": "saved"},
        {"key": "capacity.slots", "type": "integer", "value": found["slots"], "min": 1,
         "max": CAPACITY_PER_CORE * int(state["cores"]), "revision": rev, "writable": True},
        {"key": "capacity.gib_per_slot", "type": "number", "value": found["gib_per_slot"] or AUTO_GIB_PER_SLOT,
         "revision": rev, "writable": True}]
    return {"v": 1, "tool": "slotfather", "writable": state["writable"], "reason": None,
            "status": [{"label": "queue", "value": state.get("directory", "~/.local/state/slotfather")},
                       {"label": "environment", "value": ", ".join(state["environment"])}],
            "basis": found,
            "sections": [{"id": "capacity", "settings": capacity}, {"id": "order", "settings": knobs}]}


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


def main(argv: list[str]) -> int:
    calls = os.environ.get("FAKE_SLOTFATHER_CALLS")
    if calls:
        with open(calls, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(argv) + "\n")
    state = load()
    command = argv[0] if argv else ""
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
                          "settings": {"versions": [1]}, "actions": ["reprioritize"]}})
    if argv == ["status", "--json", "--full"]:
        return reply(state.get("snapshot") or idle_snapshot(state))
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
        return reply(answer)
    expected = args[args.index("--revision") + 1] if "--revision" in args else None
    args = [arg for index, arg in enumerate(args) if arg != "--revision" and (index == 0 or args[index - 1] != "--revision")]
    if not state["writable"]:
        return reply({"ok": False, "error": "this queue belongs to another user; settings are read-only"}, 4)
    if args[0] == "set" and len(args) == 3:
        key = args[1]
        if key.startswith("capacity.") and expected is not None and expected != revision(state):
            return reply(dict(settings(state), conflict=True), 3)
        try:
            value, capacity = validate(state, key, args[2])
        except (ValueError, KeyError) as error:
            return reply({"ok": False, "error": str(error).strip("'")}, 2)
        if key.startswith("capacity."):
            state["capacity"] = capacity
        else:
            state["order"][key[len("order."):]] = value
        save(state)
        return reply(settings(state))
    if args[0] == "reset" and len(args) == 2:
        if args[1] == "--all":
            state["capacity"], state["order"] = None, {}
        elif args[1].startswith("capacity."):
            if expected is not None and expected != revision(state):
                return reply(dict(settings(state), conflict=True), 3)
            state["capacity"] = None
        else:
            state["order"].pop(args[1][len("order."):], None)
        save(state)
        return reply(settings(state))
    print("fake slotfather: unknown settings command " + " ".join(argv), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
