"""Settings schema, rows and value codecs (fathers#87, fathers#233): pure data-layer types, formatting, parsing,
stepping and visibility rules (`when`), with no UI state, geometry or canvas imports."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, NamedTuple, Optional, Sequence, cast

from . import theme as _theme
from .text import span, tilde

GODFATHER = "godfather"
ORDER = ("godfather", "slotfather", "lanefather", "junkfather")  # the sections, top to bottom
DEFAULT_INTERVAL = 1.0
EDITABLE = ("choice", "boolean", "integer", "number", "duration", "interval", "theme")  # what Godfather can change
NUMBERS = ("integer", "number", "duration", "interval")
CAPACITY = ("capacity.mode", "capacity.slots", "capacity.gib_per_slot")  # Slotfather's, one revision
LOCAL_DESCRIPTIONS = {
    "interval": "Seconds between refreshes, 0.1 to 60 · this dashboard only.",
    "theme": "Auto, Terminal (palette only) or Built-in colours · this dashboard only.",
}
TITLES = {"godfather": "GODFATHER", "slotfather": "SLOTFATHER", "lanefather": "LANEFATHER", "junkfather": "JUNKFATHER"}
NAMES = {"godfather": "Godfather", "slotfather": "Slotfather", "lanefather": "Lanefather", "junkfather": "Junkfather"}

Focus = tuple[str, str]  # (tool, key)
CheckKey = tuple[str, str, str, str]  # (tool, key, the value as given, the row's revision)


@dataclass(frozen=True)
class Section:
    """One tool's section: its last `settings --json` reply, why it cannot be read now (`problem`, shown as one
    dimmed row), and its reads. `readable` is False for a tool that has no settings contract this Godfather knows:
    never read, its problem says why. `generation` goes up with each write, so a read started before one is
    dropped when it lands."""
    tool: str
    reply: Optional[dict[str, Any]] = None
    problem: Optional[str] = None
    readable: bool = True
    read_at: float = -math.inf
    reading: bool = False
    generation: int = 0


class Row(NamedTuple):
    """One setting as shown, from its tool's schema (or this dashboard's own)."""
    tool: str
    key: str
    label: str
    type: str
    value: Any
    writable: bool
    source: str
    revision: Optional[str]
    description: str
    schema: dict[str, Any]

    @property
    def focus(self) -> Focus:
        return self.tool, self.key

    @property
    def editable(self) -> bool:
        return self.writable and self.type in EDITABLE


# Value formatting, parsing and stepping

def number(value: Any) -> str:
    return f"{float(value):g}" if isinstance(value, (int, float)) and not isinstance(value, bool) else str(value)


def argument(value: Any) -> str:
    """A value as `settings set` takes it: 1800, 0.05, fixed."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def choices(row: Row) -> list[dict[str, Any]]:
    if row.type == "theme":
        return [{"value": mode, "label": _theme.MODE_LABELS[mode]} for mode in _theme.MODES]
    found = row.schema.get("choices")
    return [cast(dict[str, Any], item) for item in cast(list[Any], found) if isinstance(item, dict)] \
        if isinstance(found, list) else []


def value_text(row: Row, value: Any) -> str:
    """A value as shown, its unit beside it: 3×, 0.1 cores, 4 GiB, 1h30m, never."""
    kind = row.type
    if kind == "theme":
        return _theme.MODE_LABELS.get(str(value), str(value))
    if kind == "interval":
        return span(value, compact=True)
    if value is None:
        return "–"
    if kind == "choice":
        labels = {str(choice.get("value")): str(choice.get("label") or choice.get("value"))
                  for choice in choices(row)}
        return labels.get(str(value), str(value).capitalize())
    if kind == "boolean":
        return "On" if value else "Off"
    if kind in NUMBERS and isinstance(value, (int, float)) and not isinstance(value, bool):
        if not value and row.schema.get("zero"):
            return str(row.schema["zero"])
        if kind == "duration":
            return span(value, compact=True)
        unit = str(row.schema.get("unit") or "")
        if not unit or unit.lower() == row.label.lower():
            return number(value)
        return number(value) + ("" if unit == "×" else " ") + unit
    return tilde(str(value))


def parse_typed(row: Row, text: str) -> tuple[Any, str]:
    """(value, "") for typed text this dashboard can read for `row`, else (None, why not)."""
    try:
        value = float(text)
    except ValueError:
        return None, "Enter a number."
    if not math.isfinite(value):
        return None, "Enter a number."
    if row.type == "interval":
        return (value, "") if 0.1 <= value <= 60 else (None, "Enter seconds from 0.1 to 60.")
    if row.type == "integer":
        return (int(value), "") if value.is_integer() else (None, "Enter a whole number.")
    return value, ""


def step_value(row: Row, current: Any, direction: int) -> tuple[Any, bool]:
    """The next or previous value of `row` from `current`, and whether it changed."""
    value: Any
    if row.type in ("choice", "theme"):
        values = [item.get("value") for item in choices(row)]
        if not values:
            return current, False
        value = values[(values.index(current) + direction) % len(values)] if current in values else values[0]
    elif row.type == "boolean":
        value = not bool(current)
    elif row.type == "interval":  # 0.1, 1, 2, 3 ... 60
        value = (1.0 if current < 1 else min(60.0, math.floor(current) + 1.0)) if direction > 0 else \
            (max(1.0, math.ceil(current) - 1.0) if current > 1 else 0.1)
    elif isinstance(current, (int, float)) and not isinstance(current, bool):
        size = row.schema.get("step") or 1
        least, most = row.schema.get("min"), row.schema.get("max")
        value = round(float(current) + direction * float(size), 3)
        if isinstance(least, (int, float)):
            floor = float(least) + (float(size) if row.schema.get("min_exclusive") else 0.0)
            value = max(floor, value)
        if isinstance(most, (int, float)):
            value = min(float(most), value)
        if row.type == "integer":
            value = int(value)
    else:
        return current, False
    return value, value != current


# Sections and rows

def section(sections: Sequence[Section], tool: str) -> Optional[Section]:
    return next((found for found in sections if found.tool == tool), None)


def local_rows(interval: float, theme: str) -> list[Row]:
    return [Row(GODFATHER, "interval", "Refresh every", "interval", interval, True, "saved", None,
                LOCAL_DESCRIPTIONS["interval"], {}),
            Row(GODFATHER, "theme", "Colour theme", "theme", theme, True, "saved", None,
                LOCAL_DESCRIPTIONS["theme"], {})]


def schema_rows(found: Section) -> list[dict[str, Any]]:
    """Every setting of a reply, in its order: one flat list (owner, 2026-11-11)."""
    reply = found.reply or {}
    rows: list[dict[str, Any]] = []
    for part in cast(list[Any], reply.get("sections") or []):
        if isinstance(part, dict):
            items = cast(list[Any], cast(dict[str, Any], part).get("settings") or [])
            rows.extend(cast(dict[str, Any], item) for item in items
                        if isinstance(item, dict) and cast(dict[str, Any], item).get("key"))
    return rows


def tool_row(tool: str, item: dict[str, Any], reply: dict[str, Any]) -> Row:
    writable = item.get("writable", True) is not False and reply.get("writable", True) is not False
    revision = item.get("revision")
    return Row(tool, str(item["key"]), str(item.get("label") or item["key"]), str(item.get("type") or "text"),
               item.get("value"), writable, str(item.get("source") or ""),
               str(revision) if revision is not None else None, str(item.get("description") or ""), item)


def visible_rows(
    sections: Sequence[Section],
    interval: float,
    theme: str,
    tool: str,
    shown_override: Optional[Callable[[str, str, Any], Any]] = None,
) -> list[Row]:
    """A section's rows as shown now: a row whose `when` names another row's values shows only while that row
    shows one of them (Slotfather's Slots in Fixed, GiB per slot in the memory modes)."""
    if tool == GODFATHER:
        return local_rows(interval, theme)
    found = section(sections, tool)
    if found is None or found.reply is None:
        return []
    items = schema_rows(found)
    values = {
        str(item["key"]): (
            shown_override(tool, str(item["key"]), item.get("value"))
            if shown_override is not None
            else item.get("value")
        )
        for item in items
    }
    shown_rows: list[Row] = []
    for item in items:
        when = item.get("when")
        if isinstance(when, dict) and not all(values.get(str(key)) in (allowed if isinstance(allowed, list) else [])
                                              for key, allowed in cast(dict[str, Any], when).items()):
            continue
        shown_rows.append(tool_row(tool, item, found.reply))
    return shown_rows


def tools(sections: Sequence[Section]) -> list[str]:
    """The sections drawn, in ORDER: this dashboard's, then each present tool's."""
    present = {found.tool for found in sections}
    return [tool for tool in ORDER if tool == GODFATHER or tool in present]


def basis_of(reply: Optional[dict[str, Any]]) -> dict[str, Any]:
    return cast(dict[str, Any], (reply or {}).get("basis") or {})


def revision_of(sections: Sequence[Section], tool: str, key: str) -> Optional[str]:
    found = section(sections, tool)
    item = next((item for item in schema_rows(found) if item["key"] == key), None) if found else None
    revision = item.get("revision") if item else None
    return str(revision) if revision is not None else None


def revisions_of(reply: Optional[dict[str, Any]]) -> dict[str, str]:
    """Each setting key's revision string (`""` when `None`) in `reply`."""
    if reply is None:
        return {}
    found = Section("", reply=reply)
    return {
        str(item["key"]): str(item["revision"]) if item.get("revision") is not None else ""
        for item in schema_rows(found)
    }


def saved_value(sections: Sequence[Section], interval: float, theme: str, tool: str, key: str) -> Any:
    if tool == GODFATHER:
        return interval if key == "interval" else theme
    found = section(sections, tool)
    item = next((item for item in schema_rows(found) if item["key"] == key), None) if found else None
    return item.get("value") if item else None


def saved_text(row: Optional[Row], sections: Sequence[Section], interval: float, theme: str, key: str) -> str:
    if row is None:
        return f"Saved · {key}"
    if row.tool == "slotfather" and row.key in CAPACITY:
        found = section(sections, "slotfather")
        basis = basis_of(found.reply if found else None)
        return f"Saved · capacity {basis.get('slots', '?')} slots"
    return f"Saved · {row.label} {value_text(row, saved_value(sections, interval, theme, row.tool, row.key))}"


def conflict_text(row: Optional[Row], sections: Sequence[Section], interval: float, theme: str, tool: str, key: str,
                  ending: str) -> str:
    if tool == "slotfather" and key in CAPACITY:
        found = section(sections, tool)
        basis = basis_of(found.reply if found else None)
        return f"Capacity changed elsewhere: now {basis.get('slots', '?')} slots · {ending}"
    now_text = value_text(row, saved_value(sections, interval, theme, tool, key)) if row else "?"
    return f"Changed by another client: now {now_text} · {ending}"
