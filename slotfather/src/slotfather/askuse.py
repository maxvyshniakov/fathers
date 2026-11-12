"""Slots asked against cores used, per label family (fathers#97): `stats` reports it and `run` hints when a request
asks for far more than its family's recent runs used."""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Iterable

from . import config as _config
from . import schedule as _schedule
from . import state as _state

RECENT = 20  # a family's last runs that count for the hint
LEAST = 5  # fewer measured runs than this give no hint
SHARE = 0.5  # a family "asks far above use" when its median use is at most this share of the cores it asks
TAIL = 4000  # history lines read for the hint


def median(values: list[float]) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def per_slot(item: dict[str, Any], fallback: float) -> float:
    """The cores one slot held when the request ran: its line's cores over its capacity, else `fallback`."""
    cores, capacity = item.get("cores"), item.get("capacity")
    if isinstance(cores, int) and isinstance(capacity, int) and cores > 0 and capacity > 0:
        return cores / capacity
    return fallback


def used(item: dict[str, Any]) -> float | None:
    """Mean cores a released, measured request used while it held its slots: CPU over wall less yielded time. None
    for an all-slot request or a session, which ask for the machine to themselves rather than for cores."""
    if item.get("outcome") != "released" or not isinstance(item.get("cpu"), (int, float)):
        return None
    capacity = item.get("capacity")
    if item.get("session") or (isinstance(capacity, int) and isinstance(item.get("count"), int)
                               and capacity > 1 and item["count"] >= capacity):
        return None
    wall = item.get("wall") if isinstance(item.get("wall"), (int, float)) else item.get("ran")
    if not isinstance(wall, (int, float)):
        return None
    away = item.get("yielded")
    held = float(wall) - (float(away) if isinstance(away, (int, float)) else 0.0)
    return float(item["cpu"]) / held if held > 0 else None


def family_row(name: str, items: list[dict[str, Any]], fallback: float) -> dict[str, Any]:
    """One family's measured runs: median slots asked, median cores used, median cores asked (slots times the
    cores per slot then), the slots that median use needs, and whether it is at most SHARE of the ask."""
    measured = [(item, use) for item in items for use in [used(item)]
                if use is not None and isinstance(item.get("count"), int) and item["count"] >= 1]
    slots = median([float(item["count"]) for item, _ in measured])
    cores = median([use for _, use in measured])
    asked = median([item["count"] * per_slot(item, fallback) for item, _ in measured])
    width = median([per_slot(item, fallback) for item, _ in measured])
    needed = None if cores is None or not width else max(1, math.ceil(round(cores / width, 6)))
    over = bool(measured and asked and cores is not None and needed is not None and slots is not None
                and needed < slots and cores <= SHARE * asked)
    return {"family": name, "measured": len(measured), "slots_p50": slots,
            "cores_p50": None if cores is None else round(cores, 2),
            "cores_asked_p50": None if asked is None else round(asked, 2), "slots_needed": needed,
            "over_ask": over}


def by_family(items: Iterable[dict[str, Any]], fallback: float) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        groups.setdefault(_schedule.family(item.get("label")), []).append(item)
    rows = [family_row(name, members, fallback) for name, members in groups.items()]
    rows.sort(key=lambda r: (not r["over_ask"], -r["measured"], r["family"]))
    return rows


def hint(directory: Path, label: str, count: int) -> str | None:
    """One line for a request of `count` slots under `label` whose family's last RECENT measured runs (at least
    LEAST) used a median of at most SHARE of the cores they asked, in fewer slots than `count`; else None."""
    if count <= 1:
        return None
    name = _schedule.family(label)
    try:
        fallback = _config.cores() / max(1, _config.total())
    except (_config.SlotError, OSError, ValueError):
        return None
    lines = _state.tail_objects(directory / "history.jsonl", TAIL)
    recent = [item for item in lines if _schedule.family(item.get("label")) == name and used(item) is not None]
    recent = recent[-RECENT:]
    if len(recent) < LEAST:
        return None
    row = family_row(name, recent, fallback)
    needed, cores = row["slots_needed"], row["cores_p50"]
    asked = count * fallback
    if needed is None or cores is None or needed >= count or cores > SHARE * asked:
        return None
    return (f"hint: {name} requests used a median {cores:.1f} cores over their last {row['measured']} runs; "
            f"{count} slots hold {asked:g} cores, {needed} would hold that use (-n {needed})")
