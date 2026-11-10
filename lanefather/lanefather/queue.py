"""The ranked queue (#446): queue/queue.toml beside the state directory (LANEFATHER_QUEUE replaces it) ranks [[lane]]
entries: ticket, id, claims, optional after (lane ids that must have landed), optional blocked (an owner question
or hardware) and an optional brief printed after the header. next picks the first entry that is not blocked, not
registered yet, whose after lanes have landed and whose claims overlap no planned or active lane.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import tomllib
from typing import Any

from . import claims, config, state
from .errors import LaneError
from .state import Record


def queue_path(project: config.Config | None = None) -> Path:
    configured = (project or config.current()).env("QUEUE")
    directory = state.Store(project).directory if project else state.state_dir()
    return Path(os.path.expanduser(configured)) if configured else directory.parent / "queue" / "queue.toml"


def read_queue() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The queue's top-level settings and its [[lane]] entries in rank order, each checked."""
    path = queue_path()
    try:
        text = path.read_text()
    except FileNotFoundError:
        raise LaneError(f"no queue at {path}: rank the next tickets there as [[lane]] entries") from None
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise LaneError(f"{path}: {error}") from None
    return parse_queue(text, path)


def read_settings() -> dict[str, Any]:
    """The queue's top-level settings (`base`) without checking its entries: {} without a queue. An entry next
    refuses does not change them; a file that is no TOML raises LaneError."""
    path = queue_path()
    try:
        data = tomllib.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise LaneError(f"{path}: {error}") from None
    return {key: value for key, value in data.items() if key != "lane"}


def parse_queue(text: str, path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Validate queue text before a caller can replace the configured queue atomically."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise LaneError(f"{path}: {error}") from None
    entries = data.get("lane", [])
    if not isinstance(entries, list):
        raise LaneError(f"{path}: lane must be an array of tables, [[lane]]")
    checked: list[dict[str, Any]] = []
    for index, entry in enumerate(entries, 1):  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
        item: dict[str, Any] = entry if isinstance(entry, dict) else {}  # pyright: ignore[reportUnknownVariableType]
        problem = None
        if not isinstance(item.get("id"), str) or not state.ID.match(item["id"]):
            problem = "id: use letters, digits, '_' or '-'"
        elif not isinstance(item.get("ticket"), (int, str)) or not re.fullmatch(r"#?\d+|none", str(item["ticket"])):
            problem = "ticket: an issue number, or \"none\""
        elif (not isinstance(item.get("claims"), list) or not item["claims"]
              or not all(isinstance(c, str) and c for c in item["claims"])):
            problem = "claims: a non-empty list of path globs"
        elif not isinstance(item.get("after", []), list) or not all(isinstance(a, str) for a in item.get("after", [])):
            problem = "after: a list of lane ids"
        elif not isinstance(item.get("blocked", ""), (str, bool)):
            problem = "blocked: why it waits (an owner question, hardware), or false"
        if problem:
            raise LaneError(f"{path} entry {index} ({item.get('id', '?')}): {problem}")
        checked.append(item)
    return data, checked


def queue_states(entries: list[dict[str, Any]], records: list[Record]) -> list[tuple[dict[str, Any], str | None]]:
    """Each entry with why it is not ready, or None when it is."""
    by_id = {r["id"]: r for r in records}
    active = state.occupying(records)
    states: list[tuple[dict[str, Any], str | None]] = []
    for entry in entries:
        reason = None
        blocked = entry.get("blocked")
        if blocked:
            reason = f"blocked: {blocked}" if isinstance(blocked, str) else "blocked"
        elif entry["id"] in by_id:
            reason = f"registered ({by_id[entry['id']]['status']})"
        else:
            waiting = [f"{name} ({by_id[name]['status'] if name in by_id else 'not started'})"
                       for name in entry.get("after", []) if (by_id.get(name) or {}).get("status") not in state.LANDED]
            if waiting:
                reason = "waits for " + ", ".join(waiting)
            for other in [] if waiting else active:
                pairs = claims.overlapping(entry["claims"], other)
                if pairs:
                    reason = f"claims overlap lane {other['id']}: " + ", ".join(f"{m} with {t}" for m, t in pairs)
                    break
        states.append((entry, reason))
    return states


def next_ready() -> tuple[dict[str, Any], dict[str, Any] | None, list[tuple[dict[str, Any], str | None]]]:
    """The queue's settings, its first ready entry (None when nothing is ready) and every entry's state."""
    data, entries = read_queue()
    states = queue_states(entries, state.lanes())
    return data, next((entry for entry, reason in states if reason is None), None), states
