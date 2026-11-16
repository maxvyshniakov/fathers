"""The queue's entries as the screen tells them apart (fathers#21): each one's identity, and the colour a running
lease keeps. Data the reducer and the views share, so the reducer imports no view (fathers#141)."""
from __future__ import annotations

from typing import Any, cast

Item = dict[str, Any]
from .model import Dashboard as Model  # noqa: E402


def entry_ids(items: list[Item]) -> list[tuple[Any, int]]:
    """Each entry's identity on screen (fathers#21): its key, the lease id (fathers#101), and which of the entries
    sharing that key it is, so two entries of an older Slotfather's (keyed `[pid, since]`) of one pid created in the
    same second never share a colour, a selection or their details."""
    seen: dict[Any, int] = {}
    found: list[tuple[Any, int]] = []
    for item in items:
        key = item["key"]
        found.append((key, seen.get(key, 0)))
        seen[key] = seen.get(key, 0) + 1
    return found


def dashboard_colours(model: Model, assigned: dict[Any, int], count: int) -> None:
    """Gives each running lease one of `count` colours (the theme's holders(): 8 less any it reserves, or the
    builtin 6, #14) it keeps until it ends, preferring colours no other lease shows, so the slot bar, the table
    swatch and the detail heading stay linked as the queue moves. A lease that yields keeps its colour while it waits
    to resume (fathers#21)."""
    holders = cast(list[Item], model["holders"])
    ids = entry_ids(holders + cast(list[Item], model.get("waiters") or []))
    for key in [key for key in assigned if key not in ids]:
        del assigned[key]
    for holder, identity in zip(holders, ids):
        if holder["kind"] != "held":
            continue
        if identity not in assigned:
            taken = [index % count for index in assigned.values()]
            assigned[identity] = min(range(count), key=lambda n: (taken.count(n), n))
        holder["style"] = f"h{assigned[identity] % count}"
