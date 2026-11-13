"""The live dashboard in a PTY against the fake Slotfather: a snapshot builder and a terminal driver.

The fake prints state["snapshot"] for `status --json --full`; snapshot() builds one in the contract's encoding
(docs/contracts/slotfather.md), times as RFC 3339 strings around now so ages tick.
"""
from __future__ import annotations

import datetime
import fcntl
import json
import os
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import time
import unittest
from typing import Any, Callable

from helpers import ROOT, FakeSlotfather

PLAIN = re.compile(r"\x1b\[[0-9;?<>]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
ENTRY = ROOT / "godfather"


def stamp(moment: datetime.datetime) -> str:
    return moment.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(seconds: float) -> str:
    return stamp(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=seconds))


def item(label: str, pid: int, kind: str = "held", count: int = 1, **extra: Any) -> dict[str, Any]:
    """One request of the dashboard block: a running lease (held) or a waiting request."""
    since = ago(600 + pid % 100)
    found: dict[str, Any] = {
        "key": [pid, since], "kind": kind, "count": count, "pid": pid, "procs": 1 if kind != "waiting" else 0,
        "yields": 0, "cpu": 50.0 if kind != "waiting" else None, "rss": 20480 if kind != "waiting" else 0,
        "tree": [[pid, 50.0, 20480, "python3"]] if kind != "waiting" else [], "queue": "", "label": label,
        "cmd": f"run {label}", "meta": "", "workdir": "", "asked": "", "context": {}, "priority": "normal",
        "ticket": None, "served": None, "reprioritized": None, "yieldable": False, "passable": False,
        "all": False, "since": since, "granted": ago(300 + pid % 100) if kind != "waiting" else None,
        "asked_at": None, "idle_since": None, "est_start": None}
    found.update(extra)
    return found


def event(n: int, verb: str = "granted") -> dict[str, Any]:
    return {"when": ago(3000 - n), "verb": verb, "count": 1, "label": f"job{n}", "pid": 1000 + n,
            "delta": 1 if verb == "granted" else -1, "detail": "no wait"}


def log_line(found: dict[str, Any]) -> str:
    """A snapshot-style event as a line of Slotfather's event log on this host."""
    from godfather import events

    granted = found["verb"] == "granted"
    when = datetime.datetime.fromisoformat(str(found["when"]).replace("Z", "+00:00"))
    details: dict[str, Any] = {"count": found["count"], "waited": 0} if granted else \
        {"count": found["count"], "exit": 0, "ran": 60}
    return json.dumps({"v": 1, "time": events.stamp(when), "host": events.local_host(), "source": "slotfather",
                       "kind": "lease.granted" if granted else "lease.released",
                       "subject": {"type": "request", "id": f"r-{found['pid']}", "label": found["label"],
                                   "pid": found["pid"]}, "actor": {"type": "process"}, "details": details})


def snapshot(holders: list[dict[str, Any]] | None = None, waiters: list[dict[str, Any]] | None = None,
             events: list[dict[str, Any]] | None = None, slots: int = 4) -> dict[str, Any]:
    holders, waiters = holders or [], waiters or []
    used = sum(int(entry["count"]) for entry in holders)
    now = ago(0)
    basis = {"saved": True, "cores": 12, "cpu_slots": 4, "memory_gib": 64, "gib_per_slot": None,
             "memory_slots": 16, "slots": slots, "mode": "fixed"}
    return {"v": 2, "host": "fake", "time": now, "slots": slots, "free": max(0, slots - used), "held": used,
            "draining": 0, "dashboard": {
                "v": 1, "directory": "/fake/state", "slots": slots, "saved": True, "basis": basis, "cores": 12,
                "averages": [1.0, 1.0, 1.0], "draining": 0, "advice": {"unleased_load": 1.0, "jobs": 1},
                "holders": holders, "waiters": waiters, "used": used, "free": max(0, slots - used),
                "memory_block": None, "admission": max(0, slots - used), "order": "backfill", "start": None,
                "reserved": None, "limit": 0,
                "memory": {"total": 65536, "used": 20000, "swap_total": 0, "swap_free": 0, "low": False,
                           "swap_low": False},
                "history": [[ago(3600), now, used]], "events": events or [], "updated": now,
                "overtake_window": 600}}


def environ(fake: FakeSlotfather, **extra: str) -> dict[str, str]:
    """The inherited environment without the dashboard's own settings, the fake Slotfather on PATH."""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("SLOTFATHER_", "GODFATHER_")) and key not in ("COLORFGBG", "NO_COLOR")}
    env.update(fake.environ())
    env.update(extra)
    return env


class Terminal:
    """`godfather` (or another argv) running in a PTY of rows x columns; what it wrote, raw and plain."""

    def __init__(self, case: unittest.TestCase, env: dict[str, str], rows: int = 40, columns: int = 120,
                 argv: list[str] | None = None) -> None:
        self.case = case
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
        command = argv if argv is not None else [sys.executable, str(ENTRY), "--interval", "60"]
        self.process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, env=env,
                                        start_new_session=True)
        os.close(slave)
        self.raw = bytearray()
        case.addCleanup(self.stop)

    def read(self, seconds: float = 0.05) -> None:
        if select.select([self.master], [], [], seconds)[0]:
            try:
                self.raw.extend(os.read(self.master, 65536))
            except OSError:
                pass

    def plain(self, since: int = 0) -> str:
        return PLAIN.sub("", bytes(self.raw[since:]).decode("utf-8", "replace"))

    def until(self, check: Callable[[str], object], seconds: float = 8, what: str = "", since: int = 0) -> str:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.read()
            text = self.plain(since)
            if check(text):
                return text
        self.case.fail(f"terminal did not show {what}: {self.plain(since)[-3000:]!r}")

    def receive(self, *fragments: str, seconds: float = 8, since: int = 0) -> str:
        return self.until(lambda text: all(fragment in text for fragment in fragments), seconds,
                          " and ".join(repr(fragment) for fragment in fragments), since)

    def raw_until(self, check: Callable[[bytes], object], seconds: float = 8, what: str = "") -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.read()
            if check(bytes(self.raw)):
                return
        self.case.fail(f"terminal did not show {what}: {bytes(self.raw)[-2000:]!r}")

    def write(self, data: bytes | str) -> None:
        os.write(self.master, data.encode() if isinstance(data, str) else data)

    def mark(self) -> int:
        return len(self.raw)

    def rows(self, since: int = 0) -> dict[int, str]:
        """The screen's rows, 1-based, as last written since `since` (the dashboard writes whole rows after CSI
        row;1H)."""
        found: dict[int, str] = {}
        raw = bytes(self.raw[since:]).decode("utf-8", "replace")
        for match in re.finditer(r"\x1b\[(\d+);1H(.*?)(?=\x1b\[\d+;1H|$)", raw, re.S):
            found[int(match[1])] = PLAIN.sub("", match[2])
        return found

    def click(self, x: int, y: int) -> None:
        """A left click at 1-based column x, row y (SGR)."""
        self.write(f"\x1b[<0;{x};{y}M\x1b[<0;{x};{y}m")

    def exit_code(self, seconds: float = 8) -> int:
        deadline = time.monotonic() + seconds
        while self.process.poll() is None and time.monotonic() < deadline:
            self.read(0.1)  # read on, so its last frame never blocks it
        return self.process.wait(timeout=1)

    def quit(self, key: bytes = b"q") -> int:
        self.write(key)
        return self.exit_code()

    def stop(self) -> None:
        if self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            self.process.wait(timeout=3)
        os.close(self.master)


class LiveCase(unittest.TestCase):
    """A fake Slotfather per test; start() runs godfather against it."""

    def setUp(self) -> None:
        self.fake = FakeSlotfather()
        self.addCleanup(self.fake.close)

    def queue(self, **parts: Any) -> None:
        """The fake's snapshot; its `events` go to the fake's event log (fathers#84: EVENTS reads the log), each
        written once, in order, as Slotfather logs a grant or a release."""
        self.logged = getattr(self, "logged", 0)
        events: list[dict[str, Any]] = parts.pop("events", None) or []
        with open(self.fake.root / "events.jsonl", "a", encoding="utf-8") as handle:
            for found in events[self.logged:]:
                handle.write(log_line(found) + "\n")
        self.logged = max(self.logged, len(events))
        self.fake.update(snapshot=snapshot(**parts))

    def start(self, rows: int = 40, columns: int = 120, argv: list[str] | None = None, **extra: str) -> Terminal:
        return Terminal(self, environ(self.fake, **extra), rows, columns, argv)

    def writes(self) -> list[list[str]]:
        return [argv for argv in self.fake.argv() if argv[:2] in (["settings", "set"], ["settings", "reset"])]
