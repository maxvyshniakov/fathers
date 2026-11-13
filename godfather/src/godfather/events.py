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
import zlib
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
SKIPS = ("invalid", "version", "host", "source")  # why a line was skipped
SKIP_WORDS = {"invalid": "invalid", "version": "unknown version", "host": "other host", "source": "other source"}
READ_MAX = 1 << 20  # bytes one poll reads from a log at most; the rest waits for the next poll
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


def stamp(moment: datetime.datetime) -> str:
    """An aware time as an event's `time`: RFC 3339 UTC with milliseconds."""
    utc = moment.astimezone(datetime.timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


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
    host: Optional[str] = None  # None: this machine's name, looked up again on every read (it follows the network)
    skipped: dict[str, int] = field(default_factory=lambda: dict.fromkeys(SKIPS, 0))
    handle: Optional[BinaryIO] = None
    inode: Optional[int] = None
    pending: bytes = b""  # a last line not whole yet
    discarding: bool = False  # inside a line longer than LINE_MAX: skipped up to its newline
    order: int = 0
    reading_for: str = ""  # the host name lines are read for in this read

    def close(self) -> None:
        if self.handle is not None:
            with contextlib.suppress(OSError):
                self.handle.close()
        self.handle, self.inode, self.pending, self.discarding = None, None, b"", False

    def read(self) -> list[Event]:
        """The events appended since the last read, at most READ_MAX bytes of them, oldest first; on the first read,
        the start window."""
        self.reading_for = self.host if self.host is not None else local_host()
        if self.handle is None:
            return self.start()
        data = self.drain()
        if len(data) >= READ_MAX:  # more of this file waits: a rotation is looked at once it is read
            return self.lines(data)
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
            return self.handle.read(READ_MAX)
        except OSError:
            return b""

    def open(self, at: int) -> bool:
        try:
            handle = open(self.path, "rb")  # noqa: SIM115 - kept open between reads: it follows the file by inode
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
        if found.get("host") != self.reading_for:
            self.skipped["host"] += 1
            return None
        if found.get("source") != self.source:  # a line of another tool's in this tool's log
            self.skipped["source"] += 1
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
            self.tails[source] = Tail(source, Path(path), self.host)
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
        """Lines skipped per source and why (invalid, version, host, source): EVENTS' border and Settings' STATUS."""
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


# EVENTS' rows (fathers#84): each source's formatter turns a kind and its details into the event word, the detail
# (text, style) parts and, for Slotfather, the slot cells. Only structured fields are read; Lanefather's `message`,
# prose, is shown whole and only for a kind that has no other field. A kind a formatter does not know shows the kind
# itself and a compact `key=value` detail.

Part = tuple[str, str]
RUN_MATCH = 2.0  # seconds between a release and a run event of the same moment, where no lease id joins them
LEVEL_STYLE = {"error": "bad", "warn": "warn"}


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _span(value: Any) -> str:
    from .tui import span

    return span(float(value)) if _number(value) else ""


def _minutes(value: Any) -> str:
    from .tui import minutes

    return minutes(float(value)) if _number(value) else ""


def _words(*parts: Any) -> str:
    """The parts that say something, joined by ` · `."""
    return " · ".join(str(part) for part in parts if part not in (None, "", [], {}))


def _field(details: dict[str, Any], *path: str) -> Any:
    found: Any = details
    for key in path:
        found = cast(dict[str, Any], found).get(key) if isinstance(found, dict) else None
    return found


def _count(details: dict[str, Any]) -> int:
    count = details.get("count")
    return count if isinstance(count, int) and not isinstance(count, bool) and count > 0 else 0


@dataclass(frozen=True)
class Shown:
    """What a formatter makes of an event: the event word, the detail parts, and slot cells (count, filled)."""
    event: str
    detail: tuple[Part, ...] = ()
    count: int = 0
    filled: bool = False
    label: Optional[str] = None  # where the subject's label or id says less (an unowned process: its command)


def slotfather_shown(kind: str, details: dict[str, Any]) -> Optional[Shown]:
    """Slotfather's kinds as today's EVENTS shows them: `wait 32s` or `no wait`, `ok 3m25s` or `exit 1 · 26s`,
    `gave up 5m`, `owner died · 12m` (red for a lease, yellow for a waiter), `batch → interactive`, `waiting 45m`."""
    count = _count(details)
    if kind == "lease.granted":
        waited: Any = details.get("waited")
        if _number(waited):
            return Shown("granted", ((f"wait {_span(waited)}", "text") if float(waited) >= 1 else ("no wait", "dim"),),
                         count, True)
        return Shown("granted", (("resumed" if details.get("resumed") else "", "dim"),), count, True)
    if kind == "lease.released":
        status, ran = details.get("exit"), details.get("ran")
        if isinstance(status, int) and not isinstance(status, bool):
            text = (_words(f"ok {_span(ran)}".strip()), "dim") if status == 0 else \
                (_words(f"exit {status}", _span(ran)), "bad")
            return Shown("released", (text,), count)
        return Shown("released", ((f"ended {_span(ran)}".strip(), "dim"),), count)
    if kind == "request.left":
        return Shown("left", ((f"gave up {_minutes(details.get('gave_up_after'))}".strip(), "dim"),), count)
    if kind in ("lease.reaped", "request.reaped"):
        reason = details.get("reason")
        words = "owner died" if reason in (None, "owner exited") else str(reason)
        held = kind == "lease.reaped"
        took = details.get("ran") if held or not _number(details.get("waited")) else details.get("waited")
        return Shown("reaped", ((_words(words, _minutes(took)), "bad" if held else "warn"),), count)
    if kind == "request.reprioritized":
        return Shown("priority", ((f"{details.get('from')} → {details.get('to')}", "dim"),))
    if kind == "request.waiting_long":
        return Shown("waiting", ((f"waiting {_minutes(details.get('waited'))}".strip(), "warn"),))
    if kind == "lease.yield_asked":
        return Shown("asked", ((_words("for " + str(_field(details, "for", "label") or "?"),
                                      "by hand" if details.get("by_hand") else ""), "dim"),))
    if kind == "lease.yield_withdrawn":
        return Shown("withdrawn", (("yield not needed", "dim"),))
    if kind == "lease.yielded":
        return Shown("yielded", ((f"to {_field(details, 'to', 'label') or '?'}", "dim"),), count)
    if kind == "capacity.changed":
        return Shown("capacity", ((_words(f"{details.get('slots')} slots" if details.get("slots") else "",
                                          details.get("mode")), "dim"),))
    if kind == "order.changed":
        saved = details.get("saved")
        return Shown("order", (("reset" if details.get("reset") else
                                compact(cast(dict[str, Any], saved)) if isinstance(saved, dict) else "saved", "dim"),))
    if kind == "reap.skipped":
        return Shown("skipped", ((str(details.get("reason") or "no process table"), "warn"),))
    return None


def lanefather_shown(kind: str, details: dict[str, Any]) -> Optional[Shown]:
    """Lanefather's kinds (lanefather/events.py KINDS) from their structured fields."""
    status = details.get("status")
    judged = "bad" if status in ("red", "failed") else "dim"

    def listed(name: str) -> int:
        found = details.get(name)
        return len(cast(list[Any], found)) if isinstance(found, list) else 0

    def counted(name: str, word: str) -> str:
        return f"{listed(name)} {word}{'s' if listed(name) != 1 else ''}" if listed(name) else ""

    if kind == "lane.started":
        return Shown("started", ((_words(f"on {details['base']}" if details.get("base") else "",
                                         counted("claims", "claim")), "dim"),))
    if kind == "lane.adopted":
        return Shown("adopted", ((str(details.get("branch") or ""), "dim"),))
    if kind == "claims.added":
        overlaps = listed("overlaps")
        return Shown("claims", ((_words(f"+{listed('added')}", counted("overlaps", "overlap")),
                                 "warn" if overlaps else "dim"),))
    if kind == "lane.checked":
        return Shown("checked", ((_words(status, counted("suites", "suite")), judged),))
    if kind == "lane.conflict":
        return Shown("conflict", ((counted("paths", "path"), "bad"),))
    if kind == "lane.landed":
        return Shown("landed", ((f"onto {details['onto']}" if details.get("onto") else "", "dim"),))
    if kind == "lane.post_merge":
        return Shown("post-merge", ((_words(status, details.get("scope")), judged),))
    if kind == "lane.released":
        return Shown("released", (("claims freed", "dim"),))
    if kind == "lane.done":
        freed: Any = details.get("freed_bytes")
        from .tui import megabytes

        return Shown("done", ((f"freed {megabytes(float(freed) / 2**20)}" if _number(freed) else "", "dim"),))
    if kind == "review.started":
        return Shown("review", (("started", "dim"),))
    if kind == "review.finished":
        findings = details.get("findings")
        found = f"{findings} finding{'s' if findings != 1 else ''}" if isinstance(findings, int) else ""
        return Shown("reviewed", ((_words(found if status == "finished" else status, _minutes(details.get("seconds"))),
                                   "warn" if status not in (None, "finished") else "dim"),))
    if kind == "finding.marked":
        return Shown("finding", ((_words(f"#{details.get('finding')}", details.get("mark")), "dim"),))
    if kind == "review.marked":
        return Shown("marked", ((f"{details.get('fixed', 0)} fixed · {details.get('rejected', 0)} rejected", "dim"),))
    if kind in ("gate.finished", "perf.finished"):
        return Shown(kind.split(".")[0], ((_words(status, _minutes(details.get("seconds"))), judged),))
    if kind == "promote.step":
        code = details.get("exit")
        return Shown("promote", ((_words(details.get("step"), f"exit {code}" if code else ""),
                                  "bad" if code else "dim"),))
    if kind == "promote.finished":
        branches = details.get("branches")
        moved = "+".join(str(branch) for branch in cast(list[Any], branches)) if isinstance(branches, list) else ""
        return Shown("promoted", ((_words(moved, f"{listed('failed')} failed" if listed("failed") else ""),
                                   "warn" if listed("failed") else "dim"),))
    if kind == "sync.finished":
        return Shown("synced", ((_words(f"{listed('moved')} moved", f"{listed('failed')} failed"
                                        if listed("failed") else ""), "warn" if listed("failed") else "dim"),))
    if kind == "load.waited":
        return Shown("load wait", ((_words(_minutes(details.get("seconds")), "gave up" if details.get("gave_up")
                                           else ""), "warn" if details.get("gave_up") else "dim"),))
    if kind == "landing.lock_taken":
        return Shown("lock", ((f"from {details.get('from') or '?'}", "warn"),))
    if kind == "ticket.marked":
        done = details.get("done")
        return Shown("ticket", ((_words(f"#{details.get('issue')}", ", ".join(cast(list[str], done))
                                        if isinstance(done, list) else ""), "dim"),))
    return None


def junkfather_shown(kind: str, details: dict[str, Any]) -> Optional[Shown]:
    """junkfather's kinds (junkfather/events.py KINDS) from their structured fields."""
    ttl = f"ttl {_minutes(details.get('ttl'))}" if _number(details.get("ttl")) else ""
    if kind in ("entry.owned", "entry.adopted", "entry.extended"):
        return Shown(kind.split(".")[1], ((ttl, "dim"),))
    if kind == "entry.exited":
        code = details.get("exit")
        return Shown("exited", ((f"exit {code}" if code is not None else "", "warn" if code else "dim"),))
    if kind == "entry.handed":
        to: Any = details.get("to")
        owner = cast(dict[str, Any], to) if isinstance(to, dict) else {}
        return Shown("handed", ((f"to {owner.get('lane') or owner.get('kind') or '?'}", "dim"),))
    if kind in ("entry.reaped", "unowned.reaped", "resource.stopped"):
        result = details.get("result")
        return Shown("reaped" if kind != "resource.stopped" else "stopped",
                     ((_words(details.get("reason"), result if result not in (None, "stopped") else ""), "dim"),),
                     label=str(details["cmd"]) if kind == "unowned.reaped" and details.get("cmd") else None)
    if kind in ("entry.reap_failed", "resource.stop_failed"):
        return Shown("reap fail" if kind == "entry.reap_failed" else "stop fail",
                     ((_words(details.get("reason"), details.get("error")), "bad"),))
    if kind == "resource.owned":
        resource = details.get("resource")
        what = cast(dict[str, Any], resource).get("kind") if isinstance(resource, dict) else resource
        return Shown("owned", ((_words(what, ttl), "dim"),))
    if kind == "unowned.reported":
        return Shown("unowned", ((str(details.get("why") or ""), "warn"),),
                     label=str(details["cmd"]) if details.get("cmd") else None)
    if kind == "hook.failed":
        return Shown("hook fail", ((str(details.get("error") or ""), "bad"),))
    return None


FORMATTERS = {"slotfather": slotfather_shown, "lanefather": lanefather_shown, "junkfather": junkfather_shown}


def shown(event: Event) -> Shown:
    """The event word and detail for any event: its source's formatter, else the kind and a compact detail."""
    formatter = FORMATTERS.get(event.source)
    found = formatter(event.kind, event.details) if formatter is not None else None
    if found is not None:
        return found
    style = LEVEL_STYLE.get(str(event.data.get("level")), "dim")
    return Shown(event.kind, ((compact(event.details), style),))


def subject_label(event: Event) -> str:
    """The subject as EVENTS names it: its label, else its id; a commit by its project and short hash."""
    subject = event.subject
    if subject.get("label"):
        return str(subject["label"])
    ident = str(subject.get("id") or "?")
    if subject.get("type") == "commit":
        return _words(subject.get("project"), ident[:10]).replace(" · ", " ")
    return ident


def row(event: Event, tag: str) -> dict[str, Any]:
    """One EVENTS row: when, source and tag, kind, event word, slot cells, label, subject id, pid and the detail
    parts; `key` is its identity in the feed (identity())."""
    found = shown(event)
    pid = event.subject.get("pid")
    return {"key": identity(event), "when": event.when, "source": event.source, "tag": tag,
            "kind": event.kind, "event": found.event, "count": found.count, "filled": found.filled,
            "label": found.label or subject_label(event), "id": str(event.subject.get("id") or ""),
            "pid": pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
            "detail": list(found.detail), "lease": event.subject.get("lease_id"), "details": event.details}


def identity(event: Event) -> tuple[str, str, int]:
    """A row's identity, the same however often its log is read again (a Tail made anew numbers its lines from one
    again): its source, its `time` as written and a checksum of its line."""
    return event.source, str(event.data.get("time")), zlib.crc32(event.line.encode("utf-8"))


def rows(events: list[Event], tags: dict[str, str]) -> list[dict[str, Any]]:
    """EVENTS' rows, oldest first, from the merged feed. Slotfather's run events (fathers#108) are marks, not rows:
    the release a `run.outlier` names (by lease id, else by its end within RUN_MATCH) gets `run: "outlier"`, the
    release a slower `run.trend` came with `run: "slower"`; each trend since the oldest other row is a row of its
    own (kind `run.trend`, its run type as the label, `trend` its details)."""
    found: list[dict[str, Any]] = []
    outliers: list[Event] = []
    for event in events:
        if event.source == "slotfather" and event.kind == "run.outlier":
            outliers.append(event)
            continue
        found.append(row(event, tags.get(event.source, DEFAULT_TAGS.get(event.source, event.source[:4]))))
    releases = [item for item in found if item["source"] == "slotfather" and item["kind"] == "lease.released"]

    def nearest(moment: Optional[datetime.datetime], before: bool = False) -> Optional[dict[str, Any]]:
        if moment is None:
            return None
        close = [item for item in releases if abs((item["when"] - moment).total_seconds()) <= RUN_MATCH
                 and (not before or item["when"] <= moment)]
        return close[0] if len(close) == 1 else None

    for outlier in outliers:
        lease = outlier.details.get("lease_id")
        target = next((item for item in releases if lease and item["lease"] == lease), None) \
            or nearest(_parse_any_time(outlier.details.get("ended")))
        if target is not None:
            target["run"] = "outlier"
    others = [item for item in found if item["kind"] != "run.trend"]
    since = min((item["when"] for item in others), default=None)
    kept: list[dict[str, Any]] = []
    for item in found:
        if item["kind"] == "run.trend" and item["source"] == "slotfather":
            if since is None or item["when"] < since:
                continue
            item["trend"] = item["details"]
            if item["details"].get("verdict") == "slower":
                target = nearest(item["when"], before=True)
                if target is not None and target.get("run") != "outlier":
                    target["run"] = "slower"
        kept.append(item)
    return kept


def _parse_any_time(value: Any) -> Optional[datetime.datetime]:
    """An RFC 3339 time with or without fractions (a run's `ended`)."""
    from .adapters.slotfather import parse_time as rfc3339

    return rfc3339(value)


def text_of(item: dict[str, Any]) -> str:
    """What the text filter matches in a row: its kind, subject id, label and detail."""
    detail = cast(list[Part], item.get("detail") or [])
    return " ".join([str(item.get("kind") or ""), str(item.get("id") or ""), str(item.get("label") or ""),
                     *(text for text, _ in detail)]).lower()
