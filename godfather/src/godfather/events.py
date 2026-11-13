"""The tools' event logs (docs/events.md): one reader per source that follows its `events.jsonl`, and the merged
feed of every source.

A Tail follows one log by inode and offset. It reads the newest START_EVENTS events or START_BYTES bytes when it
starts, whichever is less (from `events.jsonl.1` too while the current log is shorter than that), then only what was
appended. After the writer renames the log to `events.jsonl.1` it reads the rest of the old file, then the new one
from its start; a log that shrank under the same inode is read again from its start. A last line without its newline
waits until it is whole. A line that fails the shared validator (eventcheck, lenient), names another schema version
or another host is skipped and counted by why; nothing is ever printed about it, and nothing stops the reader.

The Feed holds a Tail per source and the newest events each one read, merged by time, then source name, then file
order. Godfather never writes an event log; it reads only the path a tool's capabilities give (or, under
GODFATHER_FIXTURES, DIR/<tool>/events.jsonl)."""
from __future__ import annotations

import contextlib
import datetime
import json
import os
import re
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Optional, cast

from . import eventcheck as _eventcheck

SCHEMA = 1  # the event schema version this Godfather reads
START_EVENTS, START_BYTES = 600, 256 * 1024  # what a source's log gives on start, whichever is less
KEEP = START_EVENTS  # events kept per source for the panels
POLL = 0.25  # seconds between looks at a log's size (stat; no platform file-watch API)
LINE_MAX = _eventcheck.MAX_LINE_BYTES
TAG = re.compile(r"[a-z][a-z0-9]{0,3}")  # a source tag: four cells at most
SKIPS = ("invalid", "version", "host")  # why a line was skipped
CONTROL = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029]")
COMPACT_VALUE, COMPACT_MAX = 40, 160  # characters of one value, and of the whole, in a compact detail


def local_host() -> str:
    """This machine's short host name as the tools write it (lowercase, up to the first dot). Under
    GODFATHER_FIXTURES, DIR/host.json's `host` stands in for it where it names one."""
    from . import sources as _sources

    root = _sources.fixtures()
    if root is not None:
        with contextlib.suppress(OSError, ValueError, AttributeError):
            named = json.loads((root / "host.json").read_text(encoding="utf-8")).get("host")
            if isinstance(named, str) and named:
                return named
    name = re.sub(r"[^a-z0-9-]", "-", socket.gethostname().split(".")[0].lower()).strip("-")[:63]
    return name.strip("-") or "localhost"


def parse_time(value: Any) -> Optional[datetime.datetime]:
    """An event's `time` (`2026-11-12T02:24:16.204Z`) as an aware UTC datetime."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        return None
    return moment.replace(tzinfo=datetime.timezone.utc)


@dataclass(frozen=True)
class Event:
    """One event as read: its source (the tool's name), its place in that source's log (`order`, rising), its time,
    the decoded object and the line it came from."""
    source: str
    order: int
    when: datetime.datetime
    data: dict[str, Any]
    line: str

    @property
    def kind(self) -> str:
        return str(self.data.get("kind") or "")

    @property
    def subject(self) -> dict[str, Any]:
        found = self.data.get("subject")
        return cast(dict[str, Any], found) if isinstance(found, dict) else {}

    @property
    def details(self) -> dict[str, Any]:
        found = self.data.get("details")
        return cast(dict[str, Any], found) if isinstance(found, dict) else {}


def scalar(value: Any) -> Optional[str]:
    """A detail value in a few words: a number, a word, or a list of them joined by commas; None for an object."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, (int, str)):
        text = CONTROL.sub(" ", str(value))
        return text if len(text) <= COMPACT_VALUE else text[:COMPACT_VALUE - 1] + "…"
    if isinstance(value, list) and all(isinstance(item, (int, str, float)) for item in cast(list[Any], value)):
        return scalar(",".join(str(scalar(item)) for item in cast(list[Any], value)))
    return None


def compact(details: dict[str, Any]) -> str:
    """A kind's details as `key=value` pairs in their order, objects and nulls left out; Lanefather's `message` (its
    ledger line, prose) only when nothing else is there."""
    pairs = [f"{key}={scalar(value)}" for key, value in details.items()
             if key != "message" and value is not None and scalar(value) is not None]
    if not pairs and isinstance(details.get("message"), str):
        pairs = [str(scalar(details["message"]))]
    text = " ".join(pairs)
    return text if len(text) <= COMPACT_MAX else text[:COMPACT_MAX - 1] + "…"


def merged(events: list[Event]) -> list[Event]:
    """Oldest first: by time, then source name, then file order."""
    return sorted(events, key=lambda event: (event.when, event.source, event.order))


@dataclass
class Tail:
    """One source's log followed by inode and offset (the module's docstring has the rules)."""
    source: str
    path: Path
    host: str
    skipped: dict[str, int] = field(default_factory=lambda: dict.fromkeys(SKIPS, 0))
    handle: Optional[BinaryIO] = None
    inode: Optional[int] = None
    pending: bytes = b""  # a last line not whole yet
    discarding: bool = False  # inside a line longer than LINE_MAX: skipped up to its newline
    order: int = 0

    def close(self) -> None:
        if self.handle is not None:
            with contextlib.suppress(OSError):
                self.handle.close()
        self.handle, self.inode, self.pending, self.discarding = None, None, b"", False

    def read(self) -> list[Event]:
        """The events appended since the last read, oldest first; on the first read, the start window."""
        if self.handle is None:
            return self.start()
        data = self.drain()
        try:
            info = os.stat(self.path)
        except OSError:
            return self.lines(data)  # renamed and not created again yet: the old file until it is
        found: list[Event] = []
        if info.st_ino != self.inode:  # rotated: the rest of the old file, then the new one from its start
            with contextlib.suppress(OSError):
                data += self.handle.read()
            found = self.lines(data)
            self.close()
            if not self.open(0):
                return found
            data = self.drain()
        elif info.st_size < self.handle.tell():  # truncated in place: from its start
            self.handle.seek(0)
            self.pending, self.discarding = b"", False
            data = self.drain()
        return found + self.lines(data)

    def drain(self) -> bytes:
        assert self.handle is not None
        try:
            return self.handle.read()
        except OSError:
            return b""

    def open(self, at: int) -> bool:
        try:
            handle = open(self.path, "rb")
        except OSError:
            return False
        self.handle, self.inode = handle, os.fstat(handle.fileno()).st_ino
        handle.seek(at)
        return True

    def start(self) -> list[Event]:
        """The start window: the newest START_EVENTS events in the last START_BYTES bytes, the rotated log's end
        first where the current one is shorter."""
        if not self.open(0):
            return []
        assert self.handle is not None
        try:
            size = self.handle.seek(0, os.SEEK_END)
            begin = max(0, size - START_BYTES)
            self.handle.seek(max(0, begin - 1))
            data = self.handle.read()
        except OSError:
            self.close()
            return []
        older = b""
        if begin:  # the byte before the part read says whether its first line is whole
            data = data[1:] if data[:1] == b"\n" else data[data.find(b"\n") + 1:] if b"\n" in data else b""
        elif size < START_BYTES:
            older = self.rotated(START_BYTES - size)
        found = self.lines(older, partial=False) + self.lines(data)
        return found[-START_EVENTS:]

    def rotated(self, budget: int) -> bytes:
        """The whole lines in the last `budget` bytes of events.jsonl.1, b"" where there is none."""
        try:
            with open(self.path.with_name(self.path.name + ".1"), "rb") as handle:
                size = handle.seek(0, os.SEEK_END)
                begin = max(0, size - budget)
                handle.seek(begin)
                data = handle.read()
        except OSError:
            return b""
        if begin:
            data = data[data.find(b"\n") + 1:] if b"\n" in data else b""
        return data if data.endswith(b"\n") or not data else data[:data.rfind(b"\n") + 1]

    def lines(self, data: bytes, partial: bool = True) -> list[Event]:
        """The events in `data` after what was pending; a last line without its newline is kept for later
        (`partial`), or dropped (the rotated log's end, which no one writes to any more)."""
        data = self.pending + data if partial else data
        pieces = data.split(b"\n")
        rest = pieces.pop()
        found: list[Event] = []
        for piece in pieces:
            if self.discarding:  # the end of an oversized line
                self.discarding = False
                continue
            event = self.parse(piece)
            if event is not None:
                found.append(event)
        if partial:
            if self.discarding:  # still inside the oversized line
                rest = b""
            elif len(rest) >= LINE_MAX:  # never whole within the limit: skipped, and the rest of it up to its newline
                self.skipped["invalid"] += 1
                self.discarding, rest = True, b""
            self.pending = rest
        return found

    def parse(self, line: bytes) -> Optional[Event]:
        """One line as an event, or None, counted by why it was skipped. A hostile line (deeply nested, oversized,
        not UTF-8) is only ever counted."""
        if not line.strip():
            return None
        try:
            problems = _eventcheck.validate_line(line + b"\n")
            found: Any = json.loads(line.decode("utf-8"))
        except (ValueError, RecursionError):
            self.skipped["invalid"] += 1
            return None
        if problems or not isinstance(found, dict):
            version = isinstance(found, dict) and "v" in found and cast(dict[str, Any], found)["v"] != SCHEMA
            self.skipped["version" if version else "invalid"] += 1
            return None
        found = cast(dict[str, Any], found)
        if found.get("host") != self.host:
            self.skipped["host"] += 1
            return None
        when = parse_time(found.get("time"))
        if when is None:  # the validator checked it; a reader never trusts that alone
            self.skipped["invalid"] += 1
            return None
        self.order += 1
        return Event(self.source, self.order, when, found, line.decode("utf-8"))


class Feed:
    """Every source's Tail and the newest KEEP events each read; follow() names a source's log, poll() reads what
    each log gained."""

    def __init__(self, host: Optional[str] = None) -> None:
        self.host = host
        self.tails: dict[str, Tail] = {}
        self.kept: dict[str, list[Event]] = {}

    def follow(self, source: str, path: Optional[str]) -> None:
        """Follows `path` for `source` (a new path starts over); None stops following it and forgets its events."""
        tail = self.tails.get(source)
        if tail is not None and (path is None or Path(path) != tail.path):
            tail.close()
            del self.tails[source]
            self.kept.pop(source, None)
        if path is not None and source not in self.tails:
            self.tails[source] = Tail(source, Path(path), self.host if self.host is not None else local_host())
            self.kept[source] = []

    def poll(self) -> list[Event]:
        """What every followed log gained since the last poll, merged (oldest first)."""
        found: list[Event] = []
        for source, tail in self.tails.items():
            fresh = tail.read()
            if fresh:
                self.kept[source] = (self.kept[source] + fresh)[-KEEP:]
                found += fresh
        return merged(found)

    def events(self) -> list[Event]:
        """The events kept from every source, merged (oldest first)."""
        return merged([event for kept in self.kept.values() for event in kept])

    def skipped(self) -> dict[str, dict[str, int]]:
        """Lines skipped per source and why (invalid, version, host): for Settings' STATUS."""
        return {source: dict(tail.skipped) for source, tail in self.tails.items()}

    def close(self) -> None:
        for tail in self.tails.values():
            tail.close()


# What a tool's capabilities say of its event log (SPEC, Capabilities contract)

DEFAULT_TAGS = {"slotfather": "slot", "lanefather": "lane", "junkfather": "junk"}


def dashboard_block(capabilities: Optional[dict[str, Any]]) -> dict[str, Any]:
    block = (capabilities or {}).get("dashboard")
    return cast(dict[str, Any], block) if isinstance(block, dict) else {}


def log_path(capabilities: Optional[dict[str, Any]]) -> Optional[str]:
    """The event log's absolute path when the capabilities name one of a schema version this Godfather reads."""
    log: Any = dashboard_block(capabilities).get("events")
    if not isinstance(log, dict):
        return None
    versions: Any = cast(dict[str, Any], log).get("versions")
    if not isinstance(versions, list) or SCHEMA not in cast(list[Any], versions):
        return None
    path: Any = cast(dict[str, Any], log).get("path")
    return path if isinstance(path, str) and path.startswith("/") else None


def tag_of(capabilities: Optional[dict[str, Any]], name: str) -> str:
    """The source's tag as its capabilities give it (`slot`, `lane`, `junk`), else the one Godfather knows it by."""
    tag = dashboard_block(capabilities).get("tag")
    return tag if isinstance(tag, str) and TAG.fullmatch(tag) else DEFAULT_TAGS.get(name, name[:4])


class EventsContract:
    """A tool read only for its events (`godfather events`, and EVENTS' other sources until their pages come): an
    adapter for sources.Source that asks for a dashboard block with an event log of a known schema version."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.TAG = DEFAULT_TAGS.get(name, name[:4])

    def unsupported(self, capabilities: dict[str, Any]) -> Optional[str]:
        if capabilities.get("v") != 1 or not dashboard_block(capabilities):
            return f"no dashboard contract; update {self.name}"
        events: Any = dashboard_block(capabilities).get("events")
        if not isinstance(events, dict):
            return f"no event log; update {self.name}"
        if log_path(capabilities) is None:
            return "its event log needs a newer godfather"
        return None
