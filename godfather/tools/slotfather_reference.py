#!/usr/bin/env python3
"""A stand-in for Slotfather's SF-1 commands, built from Slotfather's old in-tree dashboard, for the parity fixture.

    SLOTFATHER_SRC=DIR/src python3 tools/slotfather_reference.py capabilities --json
    SLOTFATHER_SRC=DIR/src python3 tools/slotfather_reference.py status --json --full

It imports the in-tree dashboard (slotfather.dashboard.dashboard_model()) and prints its model serialised as
docs/contracts/slotfather.md says; tests/parity_reference.py uses it to write the parity fixture's
`status --json --full` reply. That dashboard is gone upstream (slotfather#28): DIR must be an archive of Slotfather
37c4df7, the last commit with it (`mkdir DIR && git -C ~/slotfather archive 37c4df7 | tar -x -C DIR`). Any other
command goes to the real `slotfather`. Godfather itself never imports Slotfather; only this tool does.
"""
from __future__ import annotations

import datetime
import json
import os
import socket
import sys
from pathlib import Path
from typing import Any

SOURCE = os.path.expanduser(os.environ.get("SLOTFATHER_SRC", ""))
MISSING = ("needs SLOTFATHER_SRC=DIR/src, DIR an archive of Slotfather 37c4df7, the last commit with the in-tree "
           "dashboard (slotfather#28 removed it): mkdir DIR && git -C ~/slotfather archive 37c4df7 | tar -x -C DIR")


def checkout(source: str) -> None:
    """Exit with how to get Slotfather 37c4df7 unless `source` has its in-tree dashboard and demo."""
    root = Path(source) if source else None
    if root is None or not (root / "slotfather" / "dashboard.py").is_file() or \
            not (root.parent / "tools" / "dashboard-demo.py").is_file():
        sys.exit(f"{Path(sys.argv[0]).name}: {MISSING}" + (f" (not at {source})" if source else ""))


def load() -> Any:
    checkout(SOURCE)
    sys.path.insert(0, SOURCE)
    import slotfather.dashboard as dashboard  # noqa: PLC0415 - from the checkout named above

    return dashboard


def stamp(value: datetime.datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def plain(value: Any) -> Any:
    """JSON values for the model: times as RFC 3339 UTC strings, tuples as arrays."""
    if isinstance(value, datetime.datetime):
        return stamp(value)
    if isinstance(value, dict):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    return value


def block(dashboard: Any, model: dict[str, Any]) -> dict[str, Any]:
    """The `dashboard` block of `status --json --full` for one in-tree model."""
    found = plain({key: value for key, value in model.items() if key not in ("installed",)})
    for item in found["holders"] + found["waiters"]:
        item.pop("style", None)
        item.pop("lane", None)
    memory = dict(found["memory"])
    status = dashboard.machine_memory_status(model["memory"])[1]
    memory["low"] = None if status == "dim" else status == "bad"
    swap = [r for r in dashboard.machine_resources(model) if r.label == "swap"]
    memory["swap_low"] = bool(swap and swap[0].critical)
    config = dashboard._config
    limits = (config.number_setting("MAX_PSI_SOME", config.MAX_PSI_SOME),
              config.number_setting("MAX_PSI_FULL", config.MAX_PSI_FULL))
    memory["psi_low"] = any(isinstance(memory.get(f"psi_{kind}"), (int, float)) and memory[f"psi_{kind}"] >= limit
                            for kind, limit in zip(("some", "full"), limits))
    found["memory"] = memory
    found["overtake_window"] = dashboard._config.overtake_window()
    found["v"] = 1
    return found


def snapshot(dashboard: Any, model: dict[str, Any]) -> dict[str, Any]:
    return {"v": 2, "host": socket.gethostname().split(".")[0].lower(),
            "time": stamp(datetime.datetime.now(datetime.timezone.utc)),
            "slots": model["slots"], "free": model["free"], "held": model["used"], "draining": model.get("draining", 0),
            "dashboard": block(dashboard, model)}


def capabilities(dashboard: Any) -> dict[str, Any]:
    changelog = Path(SOURCE).resolve().parent / "CHANGELOG.md"
    return {"v": 1, "tool": "slotfather", "version": dashboard._version.VERSION,
            "dashboard": {"v": 1, "tag": "slot",
                          "snapshot": {"argv": ["status", "--json", "--full"], "versions": [2], "poll": 1},
                          "actions": [], "changelog": str(changelog)}}


def main(argv: list[str]) -> int:
    if argv == ["capabilities", "--json"]:
        print(json.dumps(capabilities(load())))
        return 0
    if argv == ["status", "--json", "--full"]:
        dashboard = load()
        print(json.dumps(snapshot(dashboard, dashboard.dashboard_model())))
        return 0
    os.execvp("slotfather", ["slotfather", *argv])
    return 127


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
