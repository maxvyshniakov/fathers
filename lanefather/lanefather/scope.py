"""A lane's accepted scope (fathers#192): the tickets it completes, the tickets it does only part of, and the owner
decisions and specs it answers to, in the order the coordinator gave them.

The record keeps `ticket`, the primary ticket every older reader uses: the first issue the lane completes, else its
first issue, else `none`. The optional `scope` key holds the whole list:

    "scope": [{"issue": "161"}, {"issue": "64", "partial": true},
              {"decision": "https://github.com/o/r/issues/161#issuecomment-2541"}, {"spec": "docs/specs/lanes.md"}]

A record without `scope` (every record before fathers#192, and a lane that completes one ticket and names nothing
else, which therefore stays byte-identical to them) reads as `[{"issue": ticket}]`, or as no scope for `none`. A
malformed `scope` warns and reads the same way; it never fails a command.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
import re
import sys
from typing import Any, cast

from .errors import LaneError
from .state import Record

NONE = "none"
# A ticket is an issue number as start always took it: GitHub's digits, or a project's own token.
TICKET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
DECISION = re.compile(
    r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/issues/(\d+)#issuecomment-(\d+)$")
_WARNED: set[tuple[str, str]] = set()


@dataclass(frozen=True)
class Issue:
    """A ticket of the lane's scope; a partial one the lane does only part of, so its merge says `Refs`."""
    number: str
    partial: bool = False


@dataclass(frozen=True)
class Decision:
    """An owner or coordinator decision: one GitHub issue comment, never an implementer's report."""
    url: str
    repo: str
    issue: str
    comment: str


@dataclass(frozen=True)
class Spec:
    """A spec file, relative to the repository."""
    path: str


Item = Issue | Decision | Spec


def ticket_number(value: str) -> str:
    """An issue number as given (`#12` or `12`), or a refusal."""
    number = str(value).strip().lstrip("#")
    if not TICKET.match(number) or number.lower() == NONE:
        raise LaneError(f"ticket {value!r}: give an issue number")
    return number


def parse_decision(url: str) -> Decision:
    """https://github.com/<owner>/<repo>/issues/<n>#issuecomment-<id>, else a refusal."""
    found = DECISION.match(str(url).strip())
    if not found:
        raise LaneError(f"decision {url!r}: give the URL of one issue comment, "
                        "https://github.com/<owner>/<repo>/issues/<n>#issuecomment-<id>")
    owner, name, issue, comment = found.groups()
    return Decision(url=found.group(0), repo=f"{owner}/{name}", issue=issue, comment=comment)


def parse_spec(path: str) -> Spec:
    """A spec path relative to the repository, without `..`, else a refusal."""
    given = str(path).strip()
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in given):
        raise LaneError(f"spec {path!r}: give a file path relative to the repository, without '..'")
    cleaned = given
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    parts = cleaned.split("/")
    if not cleaned or cleaned.startswith("/") or "\\" in cleaned or ".." in parts or "." in parts or "" in parts:
        raise LaneError(f"spec {path!r}: give a file path relative to the repository, without '..'")
    return Spec(path=cleaned)


def validate(items: Sequence[Item]) -> None:
    """Refuse an issue, decision or spec named twice."""
    seen: set[tuple[str, str]] = set()
    for item in items:
        key = (("issue", item.number) if isinstance(item, Issue)
               else ("decision", item.url) if isinstance(item, Decision) else ("spec", item.path))
        if key in seen:
            raise LaneError(f"scope names {key[0]} {'#' if key[0] == 'issue' else ''}{key[1]} twice")
        seen.add(key)


def from_options(issues: Iterable[tuple[str, bool]], decisions: Iterable[str] = (),
                 specs: Iterable[str] = ()) -> tuple[Item, ...]:
    """start's --ticket, --partial (in the order given), --decision and --spec options as a scope."""
    given = list(issues)
    for value, partial in given:
        if partial and str(value).strip().lstrip("#").lower() == NONE:
            raise LaneError("--partial none is invalid: use --ticket none for a lane without a ticket")
    named = [(value, partial) for value, partial in given if str(value).strip().lstrip("#").lower() != NONE]
    if named and len(named) != len(given):
        raise LaneError("--ticket none names no ticket: drop it, or drop the other tickets")
    items: list[Item] = [Issue(ticket_number(value), partial) for value, partial in named]
    items += [parse_decision(url) for url in decisions]
    items += [parse_spec(path) for path in specs]
    validate(items)
    return tuple(items)


def from_ticket(ticket: str) -> tuple[Item, ...]:
    """The scope of a record without `scope`: its one ticket, or nothing for `none`."""
    number = str(ticket).strip().lstrip("#")
    return () if number.lower() == NONE else (Issue(number),)


def primary(items: Iterable[Item]) -> str:
    """The ticket older readers see: the first issue the lane completes, else its first issue, else `none`."""
    found = [item for item in items if isinstance(item, Issue)]
    first = next((item for item in found if not item.partial), found[0] if found else None)
    return first.number if first else NONE


def to_json(item: Item) -> dict[str, Any]:
    if isinstance(item, Issue):
        return {"issue": item.number, "partial": True} if item.partial else {"issue": item.number}
    if isinstance(item, Decision):
        return {"decision": item.url}
    return {"spec": item.path}


def from_json(raw: Any) -> tuple[Item, ...]:
    """The scope a record's `scope` key holds; ValueError or LaneError when it is malformed."""
    if not isinstance(raw, list):
        raise ValueError("scope is not a list")
    items: list[Item] = []
    for entry in cast(list[Any], raw):
        if not isinstance(entry, dict):
            raise ValueError(f"scope entry {entry!r} is not an object")
        fields = cast(dict[str, Any], entry)
        if "issue" in fields and set(fields) <= {"issue", "partial"}:
            if not isinstance(fields["issue"], str):
                raise ValueError(f"scope entry {fields!r}: issue is not a string")
            partial = fields.get("partial", False)
            if not isinstance(partial, bool):
                raise ValueError(f"scope entry {fields!r}: partial is not a boolean")
            items.append(Issue(ticket_number(fields["issue"]), partial))
        elif set(fields) == {"decision"}:
            if not isinstance(fields["decision"], str):
                raise ValueError(f"scope entry {fields!r}: decision is not a string")
            items.append(parse_decision(fields["decision"]))
        elif set(fields) == {"spec"}:
            if not isinstance(fields["spec"], str):
                raise ValueError(f"scope entry {fields!r}: spec is not a string")
            items.append(parse_spec(fields["spec"]))
        else:
            raise ValueError(f"scope entry {fields!r} is not an issue, decision or spec")
    validate(items)
    return tuple(items)


def of(record: Record) -> tuple[Item, ...]:
    """The lane's scope: `scope` when it is valid, else the legacy reading of `ticket` (with one warning)."""
    raw = record.get("scope")
    if raw is None:
        return from_ticket(str(record.get("ticket", NONE)))
    try:
        return from_json(raw)
    except (ValueError, LaneError) as error:
        key = (str(record.get("id")), repr(raw))
        if key not in _WARNED:
            _WARNED.add(key)
            print(f"lane: warning: lane {record.get('id')} has a malformed scope ({error}); reading its ticket "
                  f"#{record.get('ticket')} alone", file=sys.stderr)
        return from_ticket(str(record.get("ticket", NONE)))


def issues(record: Record) -> list[Issue]:
    """The lane's tickets, in scope order."""
    return [item for item in of(record) if isinstance(item, Issue)]


def explicit(items: Sequence[Item]) -> bool:
    """Whether the scope needs the `scope` key: anything but no ticket or one ticket the lane completes."""
    return not (len(items) == 0 or (len(items) == 1 and isinstance(items[0], Issue) and not items[0].partial))


def write(record: Record, items: Sequence[Item]) -> None:
    """Set the record's primary `ticket`, and `scope` only when the scope is more than that one ticket."""
    validate(items)
    record["ticket"] = primary(items)
    if explicit(items):
        record["scope"] = [to_json(item) for item in items]
    else:
        record.pop("scope", None)


def as_partial(items: Iterable[Item]) -> tuple[Item, ...]:
    """The scope with every ticket partial: a fix lane's, whose merge refers to its parent's tickets and closes
    none of them again."""
    return tuple(replace(item, partial=True) if isinstance(item, Issue) else item for item in items)


def issue_label(issue: Issue) -> str:
    return f"#{issue.number}" + (" (partial)" if issue.partial else "")


def describe(items: Sequence[Item]) -> str:
    """One line: `#161, #64 (partial), 1 decision, 1 spec`, or `none`."""
    words = [issue_label(item) for item in items if isinstance(item, Issue)]
    for kind, name in ((Decision, "decision"), (Spec, "spec")):
        count = sum(1 for item in items if isinstance(item, kind))
        if count:
            words.append(f"{count} {name}{'s' if count > 1 else ''}")
    return ", ".join(words) or NONE


def entry_line(item: Item) -> str:
    if isinstance(item, Issue):
        return f"ticket #{item.number} ({'partial: its merge says Refs' if item.partial else 'completes: Fixes'})"
    if isinstance(item, Decision):
        return f"decision {item.url}"
    return f"spec {item.path}"
