"""Shared state for the fake xcrun, docker, tailscale, open and osascript the resource tests put on PATH.

$JUNKFATHER_FAKE_STATE is one JSON file: {"sims": {udid: {name, state}}, "containers": {id: {name, image,
running}}, "routes": {route: target}, "stp": [pid, ...], "fail": [command words that fail], "calls": [argv, ...]}.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Tuple


def load() -> Dict[str, Any]:
    with open(os.environ["JUNKFATHER_FAKE_STATE"], encoding="utf-8") as handle:
        return json.load(handle)


def save(state: Dict[str, Any]) -> None:
    path = os.environ["JUNKFATHER_FAKE_STATE"]
    with open(path + ".tmp", "w", encoding="utf-8") as handle:
        json.dump(state, handle)
    os.replace(path + ".tmp", path)


def begin(tool: str) -> Tuple[Dict[str, Any], List[str]]:
    state = load()
    args = sys.argv[1:]
    calls: List[List[str]] = state.setdefault("calls", [])
    calls.append([tool, *args])
    save(state)
    fails: List[List[str]] = state.get("fail") or []
    for failing in fails:
        if [tool, *args][:len(failing)] == failing:
            print(f"{tool}: failing on purpose", file=sys.stderr)
            sys.exit(3)
    return state, args
