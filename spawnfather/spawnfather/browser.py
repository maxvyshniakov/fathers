"""agent-browser's session leftovers and its detached daemons (fathers#115).

agent-browser keeps one set of files per session in `~/.agent-browser` (`$SPAWNFATHER_AGENT_BROWSER` moves it):
`NAME.config`, `.target`, `.engine` and, while its daemon runs, `.pid` (the daemon's pid), `.sock`, `.stream` and
`.version`. A session's NAME often ends in the pid of the process that started it (`menu-dismissal-10752`). Nothing
ever removes them. Its subdirectories (downloaded browsers, saved auth and sessions, screenshots) and dotfiles are
not session files and are never touched.

Leftovers (part A): the sweep removes a session's files when its daemon runs no more (no process holds the pid its
`.pid` names as an agent-browser program, and no daemon holds its socket), the pid its name carries runs no more,
and none of its files changed for a day. When the process table cannot be read nothing is removed; when a daemon's
session cannot be told (no `.pid` names it and its sockets cannot be listed) nothing is removed either.

Daemons (part B): an agent-browser daemon detaches and launchd adopts it. It is matched to its session by the
`.pid` file that names it, else by the session socket it holds (lsof), and so to the process that started the
session (the pid in the session's name, started no later than the daemon). A daemon whose starting process belongs
to a spawnfather entry joins that entry (`daemons`) and is reaped with it; one whose starting process is in a
Slotfather lease's tree is that lease's in the CPU report (cpu.py), and spawnfather never stops anything for a lease.
A daemon no entry or lease owns, idle (under 1% CPU, its session unchanged) for an hour, is reported, never stopped.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat
import time
from typing import Any, Callable, Dict, Iterable, List, Mapping, NamedTuple, Optional, Sequence, Set

from . import cpu as _cpu
from . import procs as _procs
from .defaults import BROWSER_DAEMON_IDLE, BROWSER_IDLE_CPU, BROWSER_LEFTOVER_AGE
from .state import as_dict, as_list

SUFFIXES = (".config", ".target", ".engine", ".pid", ".sock", ".stream", ".version")
STARTER = re.compile(r"-(\d+)\Z")
SHOWN = 50  # session names a report lists


def directory() -> Path:
    configured = os.environ.get("SPAWNFATHER_AGENT_BROWSER") or os.environ.get("JUNKFATHER_AGENT_BROWSER")
    return Path(configured or Path.home() / ".agent-browser").expanduser()


class Session(NamedTuple):
    name: str
    files: List[Path]
    newest: float  # the latest change of any of its files
    size: int  # bytes
    daemon: Optional[int]  # the pid its .pid file names

    @property
    def starter(self) -> Optional[int]:
        found = STARTER.search(self.name)
        return int(found[1]) if found and int(found[1]) > 1 else None  # pid 1 is launchd, never a starter

    @property
    def socket(self) -> str:
        return str(next((f for f in self.files if f.suffix == ".sock"), self.files[0].with_suffix(".sock")))


_CACHE: Dict[str, Any] = {}


def sessions(where: Path) -> Dict[str, Session]:
    """The session files at the top of `where`, by session name, read again only when the folder changed (a file
    added or removed changes its mtime; the supervisor asks every 15 s); {} when it does not exist."""
    try:
        stamp = os.stat(where).st_mtime_ns
    except OSError:
        return {}
    cached = _CACHE.get(str(where))
    if cached is not None and cached[0] == stamp and time.time() - cached[2] < 60:
        return dict(cached[1])
    found = read_sessions(where)
    _CACHE[str(where)] = (stamp, found, time.time())
    return dict(found)


def read_sessions(where: Path) -> Dict[str, Session]:
    """The session files at the top of `where`, by session name; {} when it does not exist. Regular files and
    sockets only, never a symbolic link, a directory or a dotfile."""
    grouped: Dict[str, List[Path]] = {}
    sizes: Dict[str, int] = {}
    newest: Dict[str, float] = {}
    try:
        listing = list(os.scandir(where))
    except OSError:
        return {}
    for item in listing:
        name, suffix = os.path.splitext(item.name)
        if item.name.startswith(".") or suffix not in SUFFIXES or not name:
            continue
        try:
            info = item.stat(follow_symlinks=False)
        except OSError:
            continue
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISSOCK(info.st_mode)):
            continue
        grouped.setdefault(name, []).append(Path(item.path))
        sizes[name] = sizes.get(name, 0) + info.st_size
        newest[name] = max(newest.get(name, 0.0), info.st_mtime)
    found: Dict[str, Session] = {}
    for name, files in grouped.items():
        daemon = None
        pid_file = next((f for f in files if f.suffix == ".pid"), None)
        if pid_file is not None:
            try:
                text = pid_file.read_text().strip()
                daemon = int(text) if text.isdigit() else None
            except OSError:
                pass
        found[name] = Session(name, sorted(files), newest[name], sizes[name], daemon)
    return found


def is_browser(proc: _procs.Proc) -> bool:
    """An agent-browser program (its daemon, `agent-browser-darwin-arm64`, or its command line client)."""
    return proc.program.startswith("agent-browser")


def sockets(pids: Sequence[int]) -> Optional[Dict[int, List[str]]]:
    """pid -> the Unix socket paths it holds (one lsof call); None when lsof cannot be asked."""
    if not pids:
        return {}
    try:
        text = _procs.run(["lsof", "-nP", "-a", "-p", ",".join(map(str, pids)), "-U", "-Fpn"], ok=(0, 1))
    except _procs.ProcError:
        return None
    found: Dict[int, List[str]] = {}
    pid = None
    for line in text.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
            found.setdefault(pid, [])
        elif line.startswith("n/") and pid is not None:
            found[pid].append(line[1:])
    return found


class Daemon(NamedTuple):
    proc: _procs.Proc
    session: Optional[Session]
    how: str  # "pid file", "socket", or "" when unmatched

    @property
    def starter(self) -> Optional[int]:
        return self.session.starter if self.session is not None else None


class Survey:
    """One look at the session directory and the daemons in a process table."""

    def __init__(self, table: _procs.Table, where: Optional[Path] = None,
                 socket_reader: Optional[Callable[[Sequence[int]], Optional[Dict[int, List[str]]]]] = None,
                 uid: Optional[int] = None, cached: bool = False):
        """`cached`: the folder as last read while unchanged (the supervisor's and a reap's look for daemons);
        the sweep reads it afresh."""
        self.table = table
        self.where = where or directory()
        self.sessions = sessions(self.where) if cached else read_sessions(self.where)
        uid = os.getuid() if uid is None else uid
        named = {s.daemon: s for s in self.sessions.values() if s.daemon is not None}
        candidates = [p for p in table.procs.values() if p.uid == uid and is_browser(p)
                      and (p.pid in named or p.ppid == 1)]
        self.daemons: List[Daemon] = []
        self.unresolved = False  # a daemon whose session could not be told
        unmatched: List[_procs.Proc] = []
        for proc in candidates:
            session = named.get(proc.pid)
            if session is not None:
                self.daemons.append(Daemon(proc, session, "pid file"))
            else:
                unmatched.append(proc)
        if unmatched and self.sessions:
            held = (socket_reader or sockets)([p.pid for p in unmatched])
            by_socket = {s.socket: s for s in self.sessions.values()}
            for proc in unmatched:
                session = None
                if held is None:
                    self.unresolved = True
                else:
                    session = next((by_socket[path] for path in held.get(proc.pid, []) if path in by_socket), None)
                self.daemons.append(Daemon(proc, session, "socket" if session else ""))
        else:
            self.daemons += [Daemon(p, None, "") for p in unmatched]

    def starter_of(self, daemon: Daemon) -> Optional[_procs.Proc]:
        """The process that started the daemon's session, while it runs: the pid in the session's name, started no
        later than the daemon (a later process has taken the pid)."""
        found = self.table.get(daemon.starter)
        if found is None or found.started is None or daemon.proc.started is None:
            return None
        return found if found.started <= daemon.proc.started else None

    def held_sessions(self) -> Set[str]:
        return {d.session.name for d in self.daemons if d.session is not None}

    # Part A

    def leftovers(self, now: float) -> List[Session]:
        """Sessions whose daemon and starting pid are gone and whose files are older than a day."""
        if self.unresolved:
            return []
        held = self.held_sessions()
        found: List[Session] = []
        for session in self.sessions.values():
            if session.name in held or now - session.newest < BROWSER_LEFTOVER_AGE:
                continue
            daemon = self.table.get(session.daemon)
            if daemon is not None and is_browser(daemon):
                continue
            if session.starter is not None and session.starter in self.table.procs:
                continue
            found.append(session)
        return sorted(found, key=lambda s: s.name)

    # Part B

    def daemons_below(self, members: Iterable[_procs.Proc]) -> List[_procs.Proc]:
        """The daemons whose starting process is one of `members` (an entry's processes)."""
        pids = {p.pid for p in members}
        found: List[_procs.Proc] = []
        for daemon in self.daemons:
            starter = self.starter_of(daemon)
            if starter is not None and starter.pid in pids and daemon.proc.pid not in pids:
                found.append(daemon.proc)
        return found

    def rule(self, leases: _cpu.Rule) -> _cpu.Rule:
        """The CPU report's daemon step: a daemon whose starting process a lease claims is that lease's."""
        claims: Dict[int, _cpu.Owner] = {}
        for daemon in self.daemons:
            starter = self.starter_of(daemon)
            if starter is None:
                continue
            owner = _cpu.owner_of(starter.pid, self.table, [leases])
            if owner is not None:
                claims[daemon.proc.pid] = {**owner, "via": f"agent-browser daemon {daemon.proc.pid}"}
        return _cpu.Rule("lease", claims)

    def idle(self, owned: Set[int], leases: _cpu.Rule, now: float) -> List[Dict[str, Any]]:
        """Daemons no entry or lease owns, idle for an hour: what the sweep reports."""
        found: List[Dict[str, Any]] = []
        for daemon in self.daemons:
            proc = daemon.proc
            if proc.pid in owned:
                continue
            starter = self.starter_of(daemon)
            if starter is not None and _cpu.owner_of(starter.pid, self.table, [leases]) is not None:
                continue
            tree = [proc, *self.table.descendants(proc.pid)]
            busy = sum(p.cpu for p in tree)
            age = self.table.age(proc)
            changed = now - daemon.session.newest if daemon.session is not None else age
            if busy >= BROWSER_IDLE_CPU or age is None or age < BROWSER_DAEMON_IDLE \
                    or changed is None or changed < BROWSER_DAEMON_IDLE:
                continue
            found.append({"pid": proc.pid, "start": proc.start, "cmd": proc.cmd[:_cpu.CMD_MAX],
                          "session": daemon.session.name if daemon.session else None, "matched": daemon.how or None,
                          "starter": daemon.starter, "age": age, "idle": changed, "cpu": round(busy, 1),
                          "processes": len(tree)})
        return found


def still_leftover(session: Session, table: Optional[_procs.Table], now: float) -> Optional[str]:
    """Read again right before its files go: why the session is no longer a leftover, or None. Any of its files
    (any suffix) changed within the day, its `.pid` now names a running agent-browser program, or its starting pid
    runs again: a daemon that started during the sweep keeps its socket and pid file."""
    if table is None:
        return "the process table cannot be read"
    base = session.files[0].with_suffix("")
    for suffix in SUFFIXES:
        path = Path(f"{base}{suffix}")
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            return f"{path.name}: {error.strerror or error}"
        if now - info.st_mtime < BROWSER_LEFTOVER_AGE:
            return f"{path.name} changed within the day"
        if suffix == ".pid":
            try:
                text = path.read_text().strip()
            except OSError:
                return f"{path.name} cannot be read"
            daemon = table.get(int(text)) if text.isdigit() else None
            if daemon is not None and is_browser(daemon):
                return f"its daemon {daemon.pid} runs"
    if session.starter is not None and session.starter in table.procs:
        return f"its starting pid {session.starter} runs"
    return None


def clean(survey: Survey, now: float, dry_run: bool,
          reader: Optional[Callable[[], Optional[_procs.Table]]] = None) -> Dict[str, Any]:
    """Remove (or, in a dry run, list) the leftover sessions' files. The process table is read again before the
    removals start, and each session is checked again right before its files go (still_leftover). Returns the
    report part."""
    found = survey.leftovers(now)
    report: Dict[str, Any] = {"dir": str(survey.where), "sessions": len(survey.sessions), "removed": 0,
                              "files": 0, "bytes": 0, "names": [], "notes": []}
    if survey.unresolved:
        report["notes"].append("an agent-browser daemon's session cannot be told (lsof failed): nothing removed")
    table = (reader or _procs.read_or_none)() if found else None  # looked up now: a test can stand in for it
    for session in found:
        why = still_leftover(session, table, now)
        if why:
            report["notes"].append(f"{session.name} kept: {why}")
            continue
        removed = 0
        for path in session.files:
            try:
                info = path.lstat()
                if not (stat.S_ISREG(info.st_mode) or stat.S_ISSOCK(info.st_mode)) \
                        or now - info.st_mtime < BROWSER_LEFTOVER_AGE:
                    continue
                if not dry_run:
                    path.unlink()
                removed += 1
                report["bytes"] += info.st_size
            except OSError as error:
                report["notes"].append(f"{path.name}: {error.strerror or error}")
        if removed:
            report["removed"] += 1
            report["files"] += removed
            if len(report["names"]) < SHOWN:
                report["names"].append(session.name)
    return report


def text(report: Mapping[str, Any], dry_run: bool) -> List[str]:
    """The browser part of the sweep's text."""
    leftovers = as_dict(report.get("leftovers"))
    names = [str(name) for name in as_list(leftovers.get("names"))]
    removed = int(leftovers.get("removed") or 0)
    lines: List[str] = []
    if removed:
        verb = "would remove" if dry_run else "removed"
        more = removed - len(names)
        lines.append(f"agent-browser: {verb} {removed} leftover session(s), {leftovers.get('files')} files, "
                     f"{leftovers.get('bytes')} bytes: {', '.join(names)}" + (f" and {more} more" if more > 0 else ""))
    for item in map(as_dict, as_list(report.get("idle"))):
        lines.append(f"idle agent-browser daemon pid {item.get('pid')} (session {item.get('session') or '?'}, idle "
                     f"{float(item.get('idle') or 0) / 3600:.1f} h), no entry or lease owns it: {item.get('cmd')}")
    lines += [f"agent-browser note: {note}" for note in as_list(leftovers.get("notes"))]
    return lines
