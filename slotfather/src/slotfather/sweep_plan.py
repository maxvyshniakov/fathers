"""The sweep's classifier and planner (fathers#38): what a process is, which ones are candidates, and what the
sweep does with each, decided on a Snapshot and the facts given to it. Nothing here runs a command or sends a
signal, and it does not import the ps module: the machine's facts come from sweep_os, lease membership arrives as
`protected` and `carriers`, and `slotfather sweep` (sweep.py) puts them together. What it still reads itself:
whether a directory is there (dir_state(), the default `isdir`, replaceable per Snapshot), the checkout a directory
belongs to (project_root()) and where temporary directories are (roots())."""
from __future__ import annotations

import datetime
import os
import re
import stat
import sys
import time
from typing import Any, Callable, Iterable, Mapping, NamedTuple

from . import lineage as _lineage
from .lineage import program

LINUX = sys.platform.startswith("linux")
STOPPING = ("removed", "orphan")  # what --live stops; dev-server only with --dev-servers

Item = dict[str, Any]  # one process (or container) the sweep found, with its class and action
Carriers = Callable[[list[int]], Mapping[int, "str | None"]]  # pid -> the lease it carries; None: unknown


# What a process is

class Proc(NamedTuple):
    pid: int
    ppid: int
    uid: int
    rss: int  # KB
    tty: str  # "" without a controlling terminal
    start: str  # ps lstart in the C locale: with the pid, the process's identity
    started: float | None  # epoch seconds
    cmd: str


# (kind, pattern on the command line) for the orphan class; the first that matches names it.
ORPHAN_KINDS = (
    ("vitest", re.compile(r"(^|[\s/])vitest(\.m?js)?(\s|$)")),
    ("jest", re.compile(r"(^|[\s/])jest(\.js)?(\s|$)")),
    ("storybook-test", re.compile(r"(^|[\s/])(test-storybook|storybook\s+test)(\s|$)")),
    ("agent-browser", re.compile(r"agent-browser")),
    ("playwright", re.compile(r"playwright")),
    ("chromium", re.compile(r"(?i)(chrom(e|ium)[^/]*|headless_shell)(\s.*)?\s--(headless|remote-debugging-p\w+)")),
    ("puppeteer", re.compile(r"puppeteer")),
    ("cargo-test", re.compile(r"(^|[\s/])cargo\s+(test|nextest)(\s|$)|/target/(debug|release)/deps/[\w-]+-[0-9a-f]{16}(\s|$)")),
    ("rustc", re.compile(r"(^|/)rustc(\s|$)")),
    ("swift-test", re.compile(r"(^|[\s/])swift[\s-](test|build)(\s|$)|swiftpm-testing-helper|(^|/)xctest(\s|$)")),
    ("pytest", re.compile(r"(^|[\s/])pytest(\s|$)|\s-m\s+(pytest|unittest)(\s|$)")),
    ("esbuild", re.compile(r"(^|/)esbuild\s.*--service")),
)
SHELLS = ("sh", "bash", "zsh", "dash")
RUNNERS = ("node", "bun", "deno", "npm", "npx", "pnpm", "yarn", "tsx", "python", "Python", "uv", "make") + SHELLS
RUNNER_WORD = re.compile(r"^(verify|test|tests|run-tests|smoke|e2e|heavy|run-browser-checks|browser-checks)$")
DEV_SERVERS = (
    ("storybook-dev", re.compile(r"(^|[\s/])(storybook\s+dev|start-storybook)(\s|$)")),
    ("vite-dev", re.compile(r"(^|[\s/])vite(\.m?js)?(\s+(dev|serve|preview))?(\s|$)")),
    ("next-dev", re.compile(r"(^|[\s/])next\s+dev(\s|$)")),
    ("astro-dev", re.compile(r"(^|[\s/])astro\s+dev(\s|$)")),
    ("webpack-serve", re.compile(r"webpack-dev-server|(^|[\s/])webpack\s+serve(\s|$)")),
)
BROWSERS = ("agent-browser", "chromium", "playwright", "puppeteer")  # daemons a live session may still drive
# GUI apps whose children outlive the conversation that started them: they are not a live session (#661's preview
# server ran 27 h under the desktop app).
AGENT_APPS = re.compile(r"/(Claude|ChatGPT|Codex)\.app/")
SYSTEM = ("/System/", "/usr/libexec/", "/usr/sbin/", "/sbin/", "/Library/Apple/", "/usr/lib/systemd/",
          "/lib/systemd/")


def words(cmd: str) -> list[str]:
    """The command's words; macOS ps writes a newline or tab inside an argument as \\012 or \\011."""
    return cmd.replace("\\012", " ").replace("\\011", " ").split()


def orphan_kind(cmd: str) -> str | None:
    for name, pattern in ORPHAN_KINDS:
        if pattern.search(cmd):
            return name
    parts = words(cmd)
    if not parts:
        return None
    base = os.path.basename(parts[0])
    if base == "tail" and any(re.fullmatch(r"-[A-Za-z0-9+]*[fF][A-Za-z0-9]*", w) for w in parts[1:]):
        return "tail-f"
    base = re.sub(r"[\d.]+$", "", base) or base
    if base in RUNNERS:
        script = runner_script(parts)
        if base in ("python", "Python") and len(script) == 2 and os.path.basename(script[0]) == "slotfather" \
                and script[1] == "run":
            return "runner"
        for n, word in enumerate(script):
            if word == "--test":
                return "runner"
            if base in SHELLS and ("/" not in word and "." not in word or n):
                continue  # a shell's `test` or `[` is no test runner; its script (./verify.sh) is
            stem = os.path.basename(word).split(".")[0]
            if RUNNER_WORD.match(stem) or re.search(r"\.(test|spec)\.[cm]?[jt]sx?$", word):
                return "runner"
    return None


VALUE_OPTIONS = {"--cwd", "-C", "--prefix", "--filter", "-F", "-r", "--require", "--import", "--loader", "-w",
                 "--workspace", "--dir", "--directory", "-p", "--project"}
SUBCOMMANDS = {"run", "exec", "x", "run-script"}


def runner_script(parts: list[str]) -> list[str]:
    """What a runner runs: its script (or `-m` module, or a shell's `-c` command) and the word after it when that
    is no option (`node cli.mjs verify`, `npm run test`), or ["--test"]. Options and their values are skipped, so
    `node server.js --root /srv/e2e` and `python3 -m http.server --directory ~/tests` name no test."""
    rest = [w.strip("'\";") for w in parts[1:]]
    at = 0
    while at < len(rest):
        word = rest[at]
        if word == "--test":
            return [word]
        if word in ("-m", "-c"):
            script = rest[at + 1:at + 2]
            after = rest[at + 2:at + 3]
            return script + ([after[0]] if script and after and not after[0].startswith("-") else [])
        if word.startswith("-"):
            at += 2 if word in VALUE_OPTIONS else 1
            continue
        if word in SUBCOMMANDS:
            at += 1
            continue
        after = rest[at + 1:at + 2]
        return [word] + ([after[0]] if after and not after[0].startswith("-") else [])
    return []


def dev_server_kind(cmd: str) -> str | None:
    if re.search(r"(^|[\s/])vitest", cmd):
        return None
    for name, pattern in DEV_SERVERS:
        if pattern.search(cmd):
            return name
    return None


def is_session(proc: Proc) -> bool:
    return _lineage.session_command(proc.cmd, proc.tty)


def is_app(proc: Proc) -> bool:
    return ".app/Contents/" in program(proc.cmd)


# Snapshots of the machine

class Cwd(NamedTuple):
    path: str
    removed: bool | None  # None: unknown


def dir_state(path: str) -> bool | None:
    """True: a directory is there; False: nothing is (or a file); None: unknown, as when the sweep may not look
    (launchd without Full Disk Access under ~/Documents, ~/Desktop, iCloud): unknown is never gone."""
    try:
        return stat.S_ISDIR(os.stat(path).st_mode)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return None


IsDir = Callable[[str], "bool | None"]


def removed_dir(path: str, inode: int | None, isdir: IsDir = dir_state) -> bool | None:
    """Whether a working directory is gone: nothing at its path, or another directory there now; None: unknown."""
    if not path.startswith("/"):
        return None
    state = isdir(path)
    if state is not True:
        return None if state is None else True
    if inode is not None:
        try:
            return os.stat(path).st_ino != inode
        except (FileNotFoundError, NotADirectoryError):
            return True
        except OSError:
            return None
    return False


class Snapshot:
    """The process table and what the sweep needs to know about it. Tests build one from fake tables."""

    def __init__(self, procs: dict[int, Proc], cwds: dict[int, Cwd] | None = None, managed: Iterable[int] = (),
                 moment: float | None = None, uid: int | None = None, me: Iterable[int] = (),
                 isdir: IsDir | None = None, notes: Iterable[str] = (), foreign: Iterable[int] = ()) -> None:
        self.procs, self.cwds, self.managed = procs, cwds or {}, set(managed)
        self.foreign = set(foreign)
        self.moment = time.time() if moment is None else moment
        self.uid = os.geteuid() if uid is None else uid
        self.me = set(me)
        self.isdir: IsDir = isdir or dir_state  # True, False (gone) or None (unknown)
        self.notes = list(notes)
        self.parents = {proc.pid: proc.ppid for proc in procs.values()}
        self.children = _lineage.children_of(self.parents)

    def age(self, proc: Proc) -> float | None:
        return None if proc.started is None else max(0.0, self.moment - proc.started)

    def ancestors(self, pid: int) -> list[int]:
        return _lineage.ancestors(self.parents, pid)

    def tree(self, pid: int) -> list[int]:
        return _lineage.tree(self.children, [pid], self.procs.__contains__)

    def adopted(self, proc: Proc) -> bool:
        """Whether launchd or init (or, on Linux, a user's systemd manager) adopted it: an orphan."""
        if proc.ppid == 1:
            return True
        parent = self.procs.get(proc.ppid)
        return LINUX and parent is not None and os.path.basename(words(parent.cmd)[0] if parent.cmd else "") == \
            "systemd"


# Deciding

def exempt(proc: Proc, snap: Snapshot) -> str | None:
    """Why a process is never a candidate, or None."""
    if proc.pid <= 1 or proc.pid in snap.me:
        return "the sweep itself" if proc.pid in snap.me else "init"
    if proc.uid != snap.uid:
        return "another user's"
    if proc.tty:
        return "has a terminal"
    if is_session(proc):
        return "a session"
    exe = program(proc.cmd)
    if exe.startswith(SYSTEM):
        return "a system process"
    if is_app(proc) and not orphan_kind(proc.cmd):
        return "an app"
    if proc.pid in snap.managed:
        return "a launchd or systemd job"
    if proc.pid in snap.foreign:
        return "in another mount namespace"
    return None


def roots() -> tuple[str, ...]:
    """Where a path in a command must lie to count as a removed directory: home and the temporary directories."""
    found = [os.path.expanduser("~"), "/tmp", "/private/tmp", "/var/folders", "/private/var/folders", "/var/tmp"]
    temp = os.environ.get("TMPDIR")
    if temp:
        found.append(temp.rstrip("/"))
    return tuple(p.rstrip("/") + "/" for p in found if p and p != "/")


def gone_paths(cmd: str, isdir: IsDir, within: tuple[str, ...]) -> list[str]:
    """Absolute paths in a command (also an option's =value and the items of a `:`, `,` or `;` list) under `within`
    whose directory is certainly gone (not merely unreadable); words with a glob or a shell variable are no paths."""
    found: list[str] = []
    for word in words(cmd):
        for part in re.split(r"[=:,;]", word):  # NODE_PATH=a:b, -cp a.jar:b.jar, --paths=a,b
            path = part.strip("'\"()[]")
            if not path.startswith("/") or len(path) < 2 or not path.startswith(within) or \
                    re.search(r"[*?\[\]{}$`]", path):
                continue  # a glob or a variable is no path the process is in
            parent = os.path.dirname(path.rstrip("/"))
            if parent and parent != "/" and parent.rstrip("/") + "/" not in within and isdir(parent) is False \
                    and path not in found:
                found.append(path)
    return found


def project_root(path: str) -> str:
    """The checkout a directory belongs to (the nearest one with .git), else the directory itself."""
    here = path.rstrip("/") or "/"
    home = os.path.expanduser("~")
    while here not in ("/", home) and here:
        if os.path.lexists(os.path.join(here, ".git")):
            return here
        here = os.path.dirname(here)
    return path.rstrip("/") or "/"


def session_roots(snap: Snapshot) -> set[str]:
    """The checkouts live sessions (a terminal, an agent) work in."""
    found: set[str] = set()
    for pid, proc in snap.procs.items():
        if proc.uid == snap.uid and is_session(proc) and pid in snap.cwds and not snap.cwds[pid].removed:
            path = snap.cwds[pid].path
            if path not in ("/", os.path.expanduser("~")):
                found.add(project_root(path))
    return found


def reaches_session(snap: Snapshot, pid: int) -> int | None:
    """The first ancestor that is a live session (a terminal, an agent CLI, a GUI app other than the agents'),
    or None."""
    for here in snap.ancestors(pid):
        proc = snap.procs[here]
        if is_session(proc) or (is_app(proc) and not AGENT_APPS.search(program(proc.cmd))):
            return here
    return None


def plan(snap: Snapshot, settings: Mapping[str, Any], protected: Mapping[int, str] | None = None,
         carriers: Carriers | None = None, only: set[int] | None = None, live: bool = False,
         dev_servers: bool = False, gone_since: Mapping[str, float] | None = None) -> list[Item]:
    """The sweep's items: each candidate process with its class, kind, tree and action (would-stop or stop, report,
    spared). `protected` maps pids in a live lease's tree to its label; `carriers(pids)` gives those of pids that
    carry a lease (pid -> label) and those whose environment cannot be read while leases are live (pid -> None:
    unknown, spared and reported, never stopped). `only` limits the candidates to those pids. `gone_since` maps
    "pid start" to when a sweep first saw that process's working directory gone (sweep-state.json); a process not
    in it counts as seen gone now."""
    protected = protected or {}
    gone_since = gone_since or {}
    enabled = set(settings["classes"])
    within = roots()
    session_dirs: set[str] | None = None
    scope = {p for root in only for p in ([root, *snap.tree(root)] if root in snap.procs else [root])} \
        if only is not None else None
    found: list[Item] = []
    for pid, proc in sorted(snap.procs.items()):
        if scope is not None and pid not in scope:
            continue
        if exempt(proc, snap):
            continue
        age = snap.age(proc)
        if age is None:
            continue
        item: Item | None = None
        cwd = snap.cwds.get(pid)
        if "removed" in enabled and age >= settings["removed_age"]:
            if cwd is not None and cwd.removed is True:
                first = gone_since.get(gone_key(proc))
                item = {"class": "removed", "reason": f"cwd {cwd.path} is gone", "cwd_gone": True,
                        "gone_for": round(max(0.0, snap.moment - first)) if first else 0}
            elif orphan_kind(proc.cmd) or dev_server_kind(proc.cmd):
                # A path in the command counts only for the dev kinds: any other program may name a directory it
                # is about to create (an output path) or a list the split above did not foresee.
                gone = gone_paths(proc.cmd, snap.isdir, within)
                if gone:
                    item = {"class": "removed", "reason": f"{gone[0]} is in a directory that is gone"}
        if item is None and "orphan" in enabled and snap.adopted(proc) and age >= settings["orphan_age"]:
            kind = orphan_kind(proc.cmd)
            if kind and not dev_server_kind(proc.cmd):
                item = {"class": "orphan", "kind": kind, "reason": f"{kind} adopted by pid {proc.ppid}"}
                if kind in BROWSERS and cwd and not cwd.removed:
                    session_dirs = session_roots(snap) if session_dirs is None else session_dirs
                    if project_root(cwd.path) in session_dirs:  # its browser may be in use
                        item.update(spare=f"a session works in its checkout {project_root(cwd.path)}")
        if item is None and "dev-server" in enabled and age >= settings["dev_server_age"]:
            kind = dev_server_kind(proc.cmd)
            if kind and not any(dev_server_kind(snap.procs[a].cmd) for a in snap.ancestors(pid)
                                if snap.procs[a].uid == snap.uid):
                if reaches_session(snap, pid) is None:
                    session_dirs = session_roots(snap) if session_dirs is None else session_dirs
                    if cwd is None or cwd.removed or project_root(cwd.path) not in session_dirs:
                        item = {"class": "dev-server", "kind": kind,
                                "reason": "no live session above it or in its checkout"}
        if item is None:
            continue
        item.setdefault("kind", orphan_kind(proc.cmd) or dev_server_kind(proc.cmd) or
                        os.path.basename(words(proc.cmd)[0]) if proc.cmd else "?")
        tree = [p for p in snap.tree(pid) if snap.procs[p].uid == snap.uid]
        item.update(pid=pid, ppid=proc.ppid, start=proc.start, age=round(age), cmd=proc.cmd[:300],
                    cwd=cwd.path if cwd else None, tree=sorted(tree),
                    rss_mb=round(sum(snap.procs[p].rss for p in tree) / 1024, 1))
        found.append(item)
    # Leases and sessions in the tree spare the whole tree, and so does a process whose lease is unknown.
    pending = sorted({p for item in found for p in item["tree"] if p not in protected})
    lease_env: Mapping[int, str | None] = carriers(pending) if carriers and pending else {}
    for item in found:
        held = next((protected.get(p) or lease_env.get(p) for p in item["tree"]
                     if p in protected or lease_env.get(p) is not None), None)
        hidden = [p for p in item["tree"] if p not in protected and p in lease_env and lease_env[p] is None]
        session = next((p for p in item["tree"] if p != item["pid"] and (
            is_session(snap.procs[p]) or p in snap.me)), None)
        above = reaches_session(snap, item["pid"])
        if above is not None:
            item["session"] = above
            item["reason"] += f"; under live session pid {above} ({os.path.basename(program(snap.procs[above].cmd))})"
        if held is not None:
            item.update(action="spared", reason=f"{item['reason']}; inside the live lease {held}")
        elif hidden:
            item.update(action="spared", lease_unknown=hidden,
                        reason=f"{item['reason']}; lease unknown: the environment of pid "
                               f"{', '.join(map(str, hidden))} cannot be read while leases are live")
        elif item.get("spare"):
            item.update(action="spared", reason=f"{item['reason']}; {item.pop('spare')}")
        elif session is not None:
            item.update(action="spared", reason=f"{item['reason']}; a session (pid {session}) in its tree")
        elif item["class"] in STOPPING or (item["class"] == "dev-server" and dev_servers):
            beside = above is not None and same_gone_dir(snap, above, item)
            if above is not None and (beside or not (
                    item.get("cwd_gone") and item.get("gone_for", 0) >= settings["session_removed_age"])):
                # A live session's child is its owner's to stop; only one whose working directory a sweep has
                # seen gone for session_removed_age waits on a worktree that will not come back, and not when the
                # session itself works in that removed directory (its MCP servers, language servers).
                item.update(action="spared", under_session=True,
                            reason=f"{item['reason']}; report only: a live session's child"
                                   + ("; the session works in the same removed directory" if beside else ""))
            else:
                item["action"] = "stop" if live else "would-stop"
        else:
            item["action"] = "report"
    # A spared descendant in a stopping candidate's tree spares the ancestor too so stopping the tree never kills it.
    spared = {item["pid"] for item in found if item["action"] == "spared"}
    for item in found:
        if item["action"] not in ("stop", "would-stop"):
            continue
        child = next((p for p in item["tree"] if p != item["pid"] and p in spared), None)
        if child is not None:
            item.update(action="spared",
                        reason=f"{item['reason']}; a spared process (pid {child}) in its tree")
    if only is not None:
        found = [item for item in found if item["pid"] in only]
    # A candidate inside another that will be stopped goes with it; one inside another reported under a session is
    # in that one's tree.
    stopping = {item["pid"] for item in found if item["action"] in ("stop", "would-stop")}
    reported = {item["pid"] for item in found if item.get("under_session")}
    kept: list[Item] = []
    for item in found:
        above = next((a for a in snap.ancestors(item["pid"]) if a in stopping), None)
        if above is not None and item["action"] != "spared":
            continue
        if item.get("under_session") and any(a in reported for a in snap.ancestors(item["pid"])):
            continue  # listed with the session's child above it, in whose tree it is
        kept.append(item)
    return kept


def gone_key(proc: Proc) -> str:
    return f"{proc.pid} {proc.start}"


def same_gone_dir(snap: Snapshot, session: int, item: Item) -> bool:
    """Whether the session itself sits in the removed checkout its child's working directory was in."""
    there = snap.cwds.get(session)
    if there is None or there.removed is not True or not item.get("cwd"):
        return False
    return project_root(there.path) == project_root(item["cwd"]) or \
        item["cwd"].startswith(there.path.rstrip("/") + "/")


def track_gone(snap: Snapshot, before: Mapping[str, float]) -> dict[str, float]:
    """sweep-state.json's "gone_since" after this look: every process of the user whose working directory is gone,
    with the moment a sweep first saw it so (kept from `before`), and nothing for processes no longer gone."""
    found: dict[str, float] = {}
    for pid, proc in snap.procs.items():
        cwd = snap.cwds.get(pid)
        if proc.uid == snap.uid and cwd is not None and cwd.removed is True:
            found[gone_key(proc)] = before.get(gone_key(proc), snap.moment)
    return found


# Waits that never end

PGREP = re.compile(r"\bpgrep\s+((?:-[A-Za-z]+\s+)*)(?:\"([^\"]+)\"|'([^']+)'|([^\s;|&'\"]+))")


def self_matching(cmd: str) -> str | None:
    """The pattern of a `pgrep -f PATTERN` loop (until/while) whose own command line PATTERN matches, else None.
    Linux's pgrep then finds the loop itself, which never ends; macOS's skips the loop (its ancestor) but finds every
    other process carrying the pattern, such as another wait for the same thing, and two such waits keep each other
    alive."""
    if not re.search(r"\b(until|while)\b", cmd):
        return None
    for found in PGREP.finditer(cmd):
        if "f" not in found[1].replace("-", ""):
            continue
        pattern = found[2] or found[3] or found[4]
        try:
            hit = re.search(pattern, cmd) is not None
        except re.error:
            hit = pattern in cmd
        if hit:
            return pattern
    return None


def self_matching_waits(snap: Snapshot, only: set[int] | None = None) -> list[Item]:
    """The user's processes that wait in a self-matching pgrep loop, outermost first (report only)."""
    found: list[Item] = []
    seen: set[int] = set()
    for pid, proc in sorted(snap.procs.items()):
        if proc.uid != snap.uid or (only is not None and pid not in only):
            continue
        pattern = self_matching(proc.cmd)
        if pattern is None or any(a in seen for a in snap.ancestors(pid)):
            continue
        seen.add(pid)
        age = snap.age(proc)
        found.append({"pid": pid, "pattern": pattern, "age": None if age is None else round(age),
                      "session": reaches_session(snap, pid), "cmd": loop_head(proc.cmd),
                      "matched_by": matching(snap, pid, pattern)})
    return found


def matching(snap: Snapshot, pid: int, pattern: str) -> list[int]:
    """Other processes `pgrep -f PATTERN` run by the wait `pid` finds: not the wait, its ancestors (macOS pgrep skips
    them; procps pgrep does not, so there the wait always finds itself) or its own children, nor pgrep runs."""
    def test(cmd: str) -> bool:
        try:
            return re.search(pattern, cmd) is not None
        except re.error:
            return pattern in cmd
    skip = {pid, *snap.ancestors(pid), *snap.tree(pid)}
    return sorted(p for p, proc in snap.procs.items()
                  if p not in skip and os.path.basename(words(proc.cmd)[0] if proc.cmd else "") != "pgrep"
                  and test(proc.cmd))


def loop_head(cmd: str) -> str:
    """The command from its loop on (an agent's shell prefixes it with a long setup), cut to 160 characters."""
    at = re.search(r"\b(until|while)\b", cmd)
    return (cmd[at.start():] if at else cmd)[:160]


# Containers

def docker_time(text: object) -> float | None:
    try:
        return datetime.datetime.strptime(str(text)[:19], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=datetime.UTC).timestamp()
    except ValueError:
        return None


def restart_loops(inspected: list[dict[str, Any]], settings: Mapping[str, Any], moment: float | None = None) -> list[Item]:
    """Containers that keep restarting: a restart policy, at least container_restarts restarts, created at least
    container_minutes ago, and restarting now or started again within that time."""
    moment = time.time() if moment is None else moment
    window = settings["container_minutes"] * 60
    found: list[Item] = []
    for item in inspected:
        state: dict[str, Any] = item.get("State") or {}
        host: dict[str, Any] = item.get("HostConfig") or {}
        restart: dict[str, Any] = host.get("RestartPolicy") or {}
        policy: str = restart.get("Name") or "no"
        restarts = int(item.get("RestartCount") or 0)
        created, started = docker_time(item.get("Created")), docker_time(state.get("StartedAt"))
        if policy == "no" or restarts < settings["container_restarts"] or created is None or \
                moment - created < window:
            continue
        if not (state.get("Restarting") or (started is not None and moment - started < window)):
            continue
        config: dict[str, Any] = item.get("Config") or {}
        labels: dict[str, Any] = config.get("Labels") or {}
        found.append({"class": "container", "action": "report", "name": str(item.get("Name") or "").lstrip("/"),
                      "id": str(item.get("Id") or "")[:12], "project": labels.get("com.docker.compose.project"),
                      "status": state.get("Status"), "restarts": restarts, "policy": policy,
                      "exit_code": state.get("ExitCode"), "started": state.get("StartedAt"),
                      "since_created": round(moment - created)})
    return found
