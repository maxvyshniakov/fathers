"""A fake `lanefather` or `junkfather` for Godfather's Settings tests: `capabilities --json` and `settings`
(--json, check, set, reset) over a JSON state file, with each tool's rows field for field as the real one prints them
(lanefather/lanefather/settings.py, junkfather/junkfather/settings.py; test_contract.py compares them).

FAKE_TOOL_STATE names the state file ({"tool", "values", "environment", "writable"}); every call's argv is appended
to FAKE_TOOL_CALLS. A row its environment names is read-only (`source: "env NAME"`); `writable: false` makes every
row read-only and a write exit with the tool's read-only code: 4 for Lanefather, 5 for Junkfather (fathers#118).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from typing import Any

PATH = os.environ.get("FAKE_TOOL_STATE", "")
READ_ONLY = {"lanefather": 4, "junkfather": 5}
PRIORITY = [{"value": "project", "label": "Project"}, {"value": "interactive", "label": "Interactive"},
            {"value": "normal", "label": "Normal"}, {"value": "batch", "label": "Batch"}]
# (section id, title, key, label, type, default, extra, the variable that makes it read-only, description)
ROWS: dict[str, list[tuple[str, str, str, str, str, Any, dict[str, Any], str, str]]] = {
    "lanefather": [
        ("review", "Review", "review.start", "Review start", "choice", "auto",
         {"choices": [{"value": "auto", "label": "Automatic"}, {"value": "manual", "label": "Manual"}]},
         "LANEFATHER_REVIEW_START",
         "Whether land starts the lane's review in the background; manual waits for lanefather review ID."),
        ("review", "Review", "rerun.wait", "Rerun waits", "duration", 3300,
         {"min": 1, "max": 86400, "step": 300, "unit": "s"}, "LANEFATHER_RERUN_WAIT",
         "How long a red check's rerun waits for its slot lease before its checks stay unclassified."),
        ("slots", "Slot priorities", "slots.priority.check", "check priority", "choice", "project",
         {"choices": PRIORITY}, "LANEFATHER_PRIORITY_CHECK",
         "The Slotfather priority of this machine's check runs; project follows the project's [slots.priority] and "
         "the built-in."),
    ],
    "junkfather": [
        ("own", "Owning", "own.default_ttl", "Default TTL", "duration", 7200,
         {"min": 60, "max": 604800, "step": 300, "unit": "s"}, "",
         "How long own, own-resource and adopt keep what they start when the command gives no --ttl."),
        ("sweep", "Sweep", "sweep.interval", "Sweep every", "duration", 3600,
         {"min": 60, "max": 86400, "step": 300, "unit": "s"}, "",
         "The hourly sweep agent's interval; the loaded agent takes a new one when reinstalled (install.py --launchd)."),
        ("reap", "Reaping", "reap.grace", "Grace before SIGKILL", "duration", 10,
         {"min": 1, "max": 600, "step": 1, "unit": "s"}, "",
         "Between SIGTERM and SIGKILL when reap has no --grace; the sweep and the supervisor use it too."),
    ],
}
STATUS = {"lanefather": [("settings", "/fake/config/lanefather/settings.json"),
                         ("events", "/fake/state/lanefather/events.jsonl"),
                         ("projects", "2 registered in /fake/state/lanefather/projects.json")],
          "junkfather": [("settings", "/fake/state/junkfather/settings.json"),
                         ("state", "/fake/state/junkfather"), ("last sweep", "never")]}


def load() -> dict[str, Any]:
    with open(PATH, encoding="utf-8") as handle:
        state = json.load(handle)
    state.setdefault("values", {})
    state.setdefault("environment", {})
    state.setdefault("writable", True)
    return state


def save(state: dict[str, Any]) -> None:
    pending = PATH + ".pending"
    with open(pending, "w", encoding="utf-8") as handle:
        json.dump(state, handle)
    os.replace(pending, PATH)


def revision(state: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(state["values"], sort_keys=True).encode()).hexdigest()


def document(state: dict[str, Any]) -> dict[str, Any]:
    tool, rev, writable = state["tool"], revision(state), bool(state["writable"])
    sections: list[dict[str, Any]] = []
    for ident, title, key, label, kind, default, extra, variable, description in ROWS[tool]:
        if not sections or sections[-1]["id"] != ident:
            sections.append({"id": ident, "title": title, "settings": []})
        row = {"key": key, "label": label, "type": kind, "value": state["values"].get(key, default),
               "default": default, "source": "saved" if key in state["values"] else "default",
               "writable": writable, "revision": rev, "description": description, **extra}
        given = state["environment"].get(variable) if variable else None
        if given:
            row.update(source=f"env {variable}", writable=False, value=parse(tool, key, given))
        sections[-1]["settings"].append(row)
    if tool == "junkfather":
        sections.append({"id": "environment", "title": "Environment", "settings": [
            {"key": "env.JUNKFATHER_STATE", "label": "State directory", "type": "text",
             "value": "/fake/state/junkfather", "default": None, "source": "default", "writable": False,
             "description": "Where entries, logs, events and these settings live. Set by JUNKFATHER_STATE; "
                            "read-only here."}]})
    return {"v": 1, "tool": tool, "writable": writable, "reason": None if writable else "/fake: is not writable",
            "revision": rev, "status": [{"label": label, "value": value} for label, value in STATUS[tool]],
            "sections": sections}


def parse(tool: str, key: str, text: str) -> Any:
    """The value of `text` for `key`, ValueError with the tool's message."""
    row = next(row for row in ROWS[tool] if row[2] == key)
    kind, extra = row[4], row[6]
    if kind == "choice":
        allowed = [choice["value"] for choice in extra["choices"]]
        if text not in allowed:
            raise ValueError(f"{key}: {text!r} is not one of {', '.join(allowed)}")
        return text
    found = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd]?)", text.strip())
    if not found:
        raise ValueError(f"{key}: {text!r} is not a duration; give seconds, or 90s, 30m, 2h, 1d")
    seconds = float(found[1]) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[found[2]]
    if not extra["min"] <= seconds <= extra["max"]:
        raise ValueError(f"{key}: {text!r} must be from {extra['min']} to {extra['max']} s")
    return int(seconds) if seconds.is_integer() else seconds


def reply(value: Any, code: int = 0) -> int:
    print(json.dumps(value))
    return code


def main(argv: list[str]) -> int:
    calls = os.environ.get("FAKE_TOOL_CALLS")
    if calls:
        with open(calls, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(argv) + "\n")
    state = load()
    tool = state["tool"]
    if argv == ["capabilities", "--json"]:
        return reply({"v": 1, "tool": tool, "version": "0.9.0", "dashboard": {
            "v": 1, "tag": tool[:4], "snapshot": {"argv": ["status", "--json"], "versions": [1], "poll": 5},
            "events": {"path": os.path.join(os.path.dirname(os.path.abspath(PATH)), f"{tool}.jsonl"),
                       "versions": [1]},
            "settings": {"versions": [1]}}})
    if argv[:1] != ["settings"] or argv[-1:] != ["--json"]:
        print(f"fake {tool}: unknown command {' '.join(argv)}", file=sys.stderr)
        return 2
    args = argv[1:-1]
    expected = args[args.index("--revision") + 1] if "--revision" in args else None
    args = [arg for n, arg in enumerate(args) if arg != "--revision" and (n == 0 or args[n - 1] != "--revision")]
    if not args:
        return reply(document(state))
    keys = [row[2] for row in ROWS[tool]]
    try:
        key = args[1] if len(args) > 1 else ""
        if key not in keys and key != "--all":
            raise ValueError(f"unknown setting {key!r}; the settings are {', '.join(keys)}")
        variable = next((row[7] for row in ROWS[tool] if row[2] == key), "")
        if variable and state["environment"].get(variable):
            return reply({"v": 1, "ok": False, "error": f"{key} is set by {variable} in the environment; unset it to "
                                                        "change the saved value", "preview": None}, READ_ONLY[tool])
        if args[0] == "check" and len(args) == 3:
            value = parse(tool, key, args[2])
            return reply({"v": 1, "ok": True, "error": None, "preview": f"{key} would be {value}"})
        if not state["writable"]:
            return reply({"v": 1, "ok": False, "error": "/fake: is not writable", "preview": None}, READ_ONLY[tool])
        if expected is not None and expected != revision(state):
            return reply({**document(state), "ok": False, "conflict": True,
                          "error": f"the settings changed since revision {expected}; read them again"}, 3)
        if args[0] == "set" and len(args) == 3:
            state["values"][key] = parse(tool, key, args[2])
        elif args[0] == "reset" and len(args) == 2:
            for name in keys if key == "--all" else [key]:
                state["values"].pop(name, None)
        else:
            raise ValueError("settings takes check KEY VALUE, set KEY VALUE or reset (KEY | --all)")
    except ValueError as error:
        return reply({"v": 1, "ok": False, "error": str(error), "preview": None}, 2)
    save(state)
    return reply({**document(state), "ok": True, "error": None})


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
