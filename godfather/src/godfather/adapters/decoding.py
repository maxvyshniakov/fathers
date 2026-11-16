"""Reading one field of a tool's JSON reply, shared by the adapters (fathers#142 review): each takes whatever the
JSON held and gives the documented type or the documented empty value, never raising. A field the model requires
is checked by its adapter, which raises the source's notice."""
from __future__ import annotations

from typing import Any, Optional, cast


def as_object(value: Any) -> dict[str, Any]:
    """An object, else {}."""
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    """An array, else []."""
    return cast(list[Any], value) if isinstance(value, list) else []


def is_number(value: Any) -> bool:
    """A JSON number (true and false are not)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_whole(value: Any) -> bool:
    """A JSON whole number (true and false are not)."""
    return isinstance(value, int) and not isinstance(value, bool)


def number(value: Any) -> Optional[float]:
    return float(value) if is_number(value) else None


def whole(value: Any) -> int:
    """A whole number, else 0."""
    return int(value) if is_whole(value) else 0


def pid(value: Any) -> Optional[int]:
    """A process id: a positive whole number, else None."""
    return int(value) if is_whole(value) and value > 0 else None


def flag(value: Any) -> Optional[bool]:
    return value if isinstance(value, bool) else None


def text(value: Any) -> Optional[str]:
    """A string that is not empty, else None."""
    return value if isinstance(value, str) and value else None


def string(value: Any, default: str = "") -> str:
    """A string, empty or not, else `default`."""
    return value if isinstance(value, str) else default


def strings(value: Any) -> list[str]:
    """The strings of an array, else []."""
    return [item for item in as_list(value) if isinstance(item, str)]


def str_or_none(value: Any) -> Optional[str]:
    """Any value but null as its string."""
    return str(value) if value is not None else None
