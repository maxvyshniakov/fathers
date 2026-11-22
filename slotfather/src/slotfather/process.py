"""Process observation: the one module that runs `ps` (fathers#38).

It holds the process-table readers with their named column sets, the reader
of SLOTFATHER_LEASE markers in other processes' environments, and lease_membership(), the one rule for which live
lease a process belongs to (`status --json --pid`, `procs --json` and the sweep's protection).

Failure policy (fathers#121): a ps that cannot run, times out or exits non-zero gives no table, and neither does an
empty one; a table that reaps or protects leases must also list its caller. Such a table raises
config.ProcessTableError (PsError), never an empty answer that would read every holder as dead.

An environment that cannot be read (another user's process, an Apple platform binary on macOS, /proc mounted with
hidepid) is unknown, never "not carrying a lease" (owner decision on fathers#38).
"""
from __future__ import annotations

import contextlib
import datetime
import os
import re
import subprocess
import sys
from typing import Any, Callable, Iterable, Mapping, NamedTuple, Sequence, cast

from . import config as _config
from . import lineage as _lineage
from . import platform as _platform

# The column sets each reader asks ps for; every one starts with the pid.
IDENTITY = "pid=,ppid=,stat=,lstart="  # processes(): the queue's identities
USAGE = "pid=,ppid=,uid=,rss=,time=,lstart="  # usage_table(): the lease's sampler
MEMBERS = "pid=,ppid=,uid=,tty=,stat=,lstart=,command="  # procs: identities, sessions and commands
SWEEP = "pid=,ppid=,uid=,rss=,tty=,lstart=,command="  # the sweep's snapshot
CPU = "pid=,ppid=,%cpu=,rss=,comm="  # the dashboard block's CPU sample
CPU_FULL = "pid=,ppid=,uid=,%cpu=,rss=,lstart=,command="  # macOS dashboard sample with full commands
HELPER_SNAPSHOT = "pid=,ppid=,uid=,lstart=,command="  # macOS detached browser/XPC correlation snapshot
STARTS = "pid=,stat=,lstart="  # the sweep's check before a signal; running()
OWNER = "uid="  # process_uid()
NAMES = "pid=,comm="  # names(): the program of a process (the lock wait's helpers)
ENVIRONMENT = "pid=,command="  # with -wwE: the command and, where shown, the environment
ARGUMENTS = "pid=,command="  # the full arguments, without the environment

MARKER = "SLOTFATHER_LEASE="


class PsError(_config.ProcessTableError):
    """ps gave no usable answer; `detail` is the cause without the "ps" prefix some callers word themselves."""

    def __init__(self, message: str, detail: str) -> None:
        super().__init__(message)
        self.detail = detail


def ps_environment() -> dict[str, str] | None:
    """The environment ps runs in for processes(), whose lstart a new entry writes as its `start`: on Linux the C
    locale (procps writes lstart in the locale's words; every client since #653 writes C there); macOS keeps the
    caller's locale and time zone, as every older client does, so that an older client still reads a current
    client's `start` exactly as it read its own (fathers#122). Current clients compare `started` instead."""
    return _platform.c_locale() if sys.platform.startswith("linux") else None


def run(arguments: Sequence[str], *, env: Mapping[str, str] | None = None, timeout: float | None = None,
        executable: str = "ps") -> subprocess.CompletedProcess[str]:
    """One ps run; raises PsError when ps cannot run or takes longer than `timeout` (default PS_TIMEOUT). Its exit
    status is the caller's to judge: `ps -p` exits 1 when some of the pids are gone."""
    try:
        return subprocess.run([executable, *arguments], capture_output=True, text=True,
                              env=None if env is None else dict(env),
                              timeout=_config.PS_TIMEOUT if timeout is None else timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PsError(f"ps: {error}", str(error)) from None


def table(arguments: Sequence[str], *, caller: bool = True, env: Mapping[str, str] | None = None,
          timeout: float | None = None) -> list[str]:
    """The rows (lines whose first word is a pid) of a ps over the machine. Raises PsError for a ps that fails or
    times out, a table without a row, and, with `caller`, a table that leaves out this process: a filtered or cut
    table would read the holders it hides as dead (fathers#121)."""
    result = run(arguments, env=env, timeout=timeout)
    if result.returncode:
        raise PsError(f"ps exited {result.returncode}: {result.stderr.strip()[:200]}", f"ps exited {result.returncode}")
    rows = [line for line in result.stdout.splitlines() if (line.split() or [""])[0].isdigit()]
    if caller:
        me = str(os.getpid())
        if not any(line.split(None, 1)[0] == me for line in rows):
            message = f"ps listed {len(rows)} processes and not this one"
            raise PsError(f"{message} ({os.getpid()})", message)
    elif not rows:
        raise PsError("ps listed no live processes", "ps listed no live processes")
    return rows


# The machine's tables

def processes() -> dict[int, tuple[int, str]]:
    """Live pid -> (ppid, start time); the start time tells a reused pid from the holder. Zombies are dead. The same
    ps flags serve macOS and Linux (procps-ng). Raises ProcessTableError for a ps that fails, takes longer than
    PS_TIMEOUT, or gives a table without this process in it: such a table is no table, and reaping by it would
    take every lease from commands that still run."""
    found: dict[int, tuple[int, str]] = {}
    for line in table(["-axo", IDENTITY], env=ps_environment()):  # it lists the caller (table(): fathers#121)
        parts = line.split(None, 3)
        if len(parts) == 4 and parts[1].isdigit() and not parts[2].startswith("Z"):
            found[int(parts[0])] = (int(parts[1]), parts[3].strip())
    return found


def usage_table() -> dict[int, tuple[int, str, int, int, float]]:
    """Live pid -> (ppid, start, uid, resident KB, CPU seconds of its own), from one ps over the machine (on Linux
    the CPU from /proc/PID/stat where it can be read). Raises SlotError when ps fails or takes longer than
    PS_TIMEOUT. An unusable answer gives the rows it has: the sampler only adds up what it sees."""
    try:
        result = run(["-axo", USAGE], env=_platform.c_locale())
    except PsError as error:
        raise _config.SlotError(str(error)) from None
    found: dict[int, tuple[int, str, int, int, float]] = {}
    for line in result.stdout.splitlines():
        parts = line.split(None, 5)
        if len(parts) == 6 and all(p.isdigit() for p in parts[:4]):
            cpu = _platform.cpu_seconds(parts[4])
            if _platform.linux():
                cpu = _platform.proc_cpu_seconds(int(parts[0])) or cpu
            if cpu is not None:
                found[int(parts[0])] = (int(parts[1]), parts[5].strip(), int(parts[2]), int(parts[3]), cpu)
    return found


def cpu_table() -> tuple[dict[int, tuple[float, int, str]], dict[int, list[int]]]:
    """pid -> (CPU percent, resident KB, command name) for every process, and ppid -> its child pids. Raises PsError
    for a ps that fails or times out and for a table without this process (fathers#121)."""
    own: dict[int, tuple[float, int, str]] = {}
    children: dict[int, list[int]] = {}
    for line in table(["-axo", CPU]):  # it lists the caller (table(): fathers#121)
        parts = line.split(None, 4)
        if len(parts) == 5 and parts[1].isdigit():
            with contextlib.suppress(ValueError):
                own[int(parts[0])] = (float(parts[2].replace(",", ".")), int(parts[3]), os.path.basename(parts[4]))
                children.setdefault(int(parts[1]), []).append(int(parts[0]))
    return own, children


class ProcessContext(NamedTuple):
    """Extra per-process metadata for detached macOS browser/XPC helper attribution."""
    parents: dict[int, int]
    uids: dict[int, int]
    starts: dict[int, float | None]
    raw_starts: dict[int, str]
    commands: dict[int, str]


_BUNDLE_EXEC_RE = re.compile(r"^(/[^\n]*?\.(?:app|xpc)/Contents/MacOS/[^/\n]+?)(?=\s+-|\s+/|$)")

_SYSTEM_BROWSER_PREFIXES = (
    "/System/",
    "/Library/Apple/",
    "/Applications/",
    "/usr/",
    "/sbin/",
    "/bin/",
)

_WEBKIT_XPC_RE = re.compile(
    r"^(?P<root>/[^\n]*?)"
    r"(?:/Playwright\.app/Contents/Frameworks)?"
    r"(?:/WebKit\.framework/(?:Versions/[^/]+/)?XPCServices)?"
    r"/com\.apple\.WebKit\.[^/\s]+\.xpc/Contents/MacOS/com\.apple\.WebKit\.[^/\s]+(?:\s|$)"
)

_AUTOMATED_CACHE_ROOT_RE = re.compile(
    r"^(?P<root>/[^\n]*?/(?:ms-playwright/[^/\s]+|\.cache/puppeteer/[^/\s]+/[^/\s]+))(?:/|$)"
)

_WEBKIT_DRIVER_RE = re.compile(
    r"^(?P<root>/[^\n]*?)/(?:Playwright\.app/Contents/MacOS/Playwright|pw_run\.sh|MiniBrowser\.app/Contents/MacOS/MiniBrowser)(?:\s|$)"
)

_SHELL_PW_RUN_RE = re.compile(
    r"^(?:/usr/bin/env\s+)?(?:/[^\s]*/)?(?:ba|z|da)?sh\s+(?P<root>/[^\s\n]*?)/pw_run\.sh(?:\s|$)"
)

_BROWSER_HELPER_MARKERS = (
    "--type=",
    "Helper (",
    "Helper.app/",
    "plugin-container",
    "-contentproc",
    "chrome_crashpad_handler",
    "crashpad_handler",
    ".xpc/Contents/MacOS/",
)

_PROFILE_FLAGS = ("--user-data-dir", "--disk-cache-dir", "--data-path", "-profile", "--profile")

_ISOLATED_PROFILE_TOKENS = (
    "playwright",
    "puppeteer",
    "selenium",
    "chromiumdev_profile",
    "firefoxdev_profile",
    "webkit_profile",
)

_USER_BROWSER_PROFILE_SUBSTRINGS = (
    "/library/application support/google/chrome",
    "/library/application support/firefox",
    "/library/application support/chromium",
    "/library/application support/brave",
    "/library/application support/microsoft edge",
    "/library/application support/arc",
    "/library/safari",
)


def _command_name(cmd: str) -> str:
    """Executable basename from a full command line, keeping macOS .app/.xpc bundle names intact."""
    stripped = cmd.strip()
    if not stripped:
        return ""
    matched = _BUNDLE_EXEC_RE.match(stripped)
    if matched:
        return os.path.basename(matched.group(1))
    return os.path.basename(stripped.split(None, 1)[0])


def cpu_snapshot() -> tuple[dict[int, tuple[float, int, str]], dict[int, list[int]], ProcessContext | None]:
    """One ps sample for the dashboard: (own, children, context). On macOS, reads full commands, uids and start
    times in the same ps call so detached browser/XPC helpers can be attributed without a second scan; on other
    platforms returns cpu_table() with context=None."""
    if sys.platform != "darwin":
        own, children = cpu_table()
        return own, children, None
    own: dict[int, tuple[float, int, str]] = {}
    children: dict[int, list[int]] = {}
    parents: dict[int, int] = {}
    uids: dict[int, int] = {}
    starts_map: dict[int, float | None] = {}
    raw_starts: dict[int, str] = {}
    cmds: dict[int, str] = {}
    for line in table(["-axww", "-o", CPU_FULL], env=_platform.c_locale()):
        parts = line.split(None, 10)
        if len(parts) < 10 or not (parts[0].isdigit() and parts[1].isdigit() and parts[2].isdigit()):
            continue
        with contextlib.suppress(ValueError):
            pid, ppid, uid = int(parts[0]), int(parts[1]), int(parts[2])
            cpu_pct = float(parts[3].replace(",", "."))
            rss = int(parts[4])
            start_str = " ".join(parts[5:10])
            cmd = parts[10].strip() if len(parts) > 10 else ""
            own[pid] = (cpu_pct, rss, _command_name(cmd))
            children.setdefault(ppid, []).append(pid)
            parents[pid] = ppid
            uids[pid] = uid
            raw_starts[pid] = start_str
            starts_map[pid] = _platform.started_at(start_str)
            cmds[pid] = cmd
    return own, children, ProcessContext(parents, uids, starts_map, raw_starts, cmds)


def helper_snapshot(timeout: float = 5) -> ProcessContext | None:
    """One full-table snapshot of pid, ppid, uid, lstart and command for detached browser helper correlation.
    Returns None when ps fails, times out, or omits its caller (unavailable inspection, never an empty result)."""
    try:
        rows = table(["-axww", "-o", HELPER_SNAPSHOT], env=_platform.c_locale(), timeout=timeout)
    except PsError:
        return None
    parents: dict[int, int] = {}
    uids: dict[int, int] = {}
    starts_map: dict[int, float | None] = {}
    raw_starts: dict[int, str] = {}
    cmds: dict[int, str] = {}
    for line in rows:
        parts = line.split(None, 8)
        if len(parts) < 8 or not (parts[0].isdigit() and parts[1].isdigit() and parts[2].isdigit()):
            continue
        pid, ppid, uid = int(parts[0]), int(parts[1]), int(parts[2])
        start_str = " ".join(parts[3:8])
        cmd = parts[8].strip() if len(parts) > 8 else ""
        parents[pid] = ppid
        uids[pid] = uid
        raw_starts[pid] = start_str
        starts_map[pid] = _platform.started_at(start_str)
        cmds[pid] = cmd
    return ProcessContext(parents, uids, starts_map, raw_starts, cmds)


def lease_started_epoch(entry: Mapping[str, Any]) -> float | None:
    """Epoch seconds when a held lease began running work (for filtering out older pre-existing detached helpers)."""
    raw = entry.get("granted") or entry.get("since")
    if not isinstance(raw, str) or not raw:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            return datetime.datetime.strptime(raw, fmt).replace(tzinfo=datetime.timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def _normalize_path(path: str) -> str:
    cleaned = path.strip().strip("\"'")
    if cleaned.startswith(("/private/var/", "/private/tmp/")):
        cleaned = cleaned[len("/private"):]
    return cleaned.rstrip("/")


def _is_isolated_profile_path(path: str) -> bool:
    norm = _normalize_path(path)
    if not norm.startswith("/") or norm in ("/", "/tmp", "/var/tmp", "/var/folders"):
        return False
    lower = norm.lower()
    if any(sub in lower for sub in _USER_BROWSER_PROFILE_SUBSTRINGS):
        return False
    parts = [part for part in norm.split("/") if part]
    if (norm.startswith("/tmp/") and len(parts) >= 2) \
            or (norm.startswith("/var/tmp/") and len(parts) >= 3) \
            or (norm.startswith("/var/folders/") and len(parts) >= 6 and parts[4] == "T"):
        return True
    return any(token in lower for token in _ISOLATED_PROFILE_TOKENS)


def _isolated_profile_paths(cmd: str) -> set[str]:
    tokens = cmd.split()
    found: set[str] = set()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        value: str | None = None
        for flag in _PROFILE_FLAGS:
            if token.startswith(flag + "="):
                value = token[len(flag) + 1:]
                break
            if token == flag and index + 1 < len(tokens) and not tokens[index + 1].startswith("-"):
                value = tokens[index + 1]
                index += 1
                break
        if value and _is_isolated_profile_path(value):
            found.add(_normalize_path(value))
        index += 1
    return found


def _webkit_xpc_root(cmd: str) -> str | None:
    stripped = cmd.strip()
    if not stripped or stripped.startswith(_SYSTEM_BROWSER_PREFIXES):
        return None
    matched = _WEBKIT_XPC_RE.match(stripped)
    if not matched:
        return None
    root = _normalize_path(matched.group("root"))
    return root if root and not root.startswith(_SYSTEM_BROWSER_PREFIXES) else None


def _automated_cache_root(cmd: str) -> str | None:
    stripped = cmd.strip()
    if not stripped or stripped.startswith(_SYSTEM_BROWSER_PREFIXES):
        return None
    matched = _AUTOMATED_CACHE_ROOT_RE.match(stripped)
    if not matched:
        return None
    return _normalize_path(matched.group("root"))


def _has_helper_marker(cmd: str) -> bool:
    return any(marker in cmd for marker in _BROWSER_HELPER_MARKERS)


def _helper_browser_root(cmd: str) -> str | None:
    """Automated browser bundle root if `cmd` is a detached browser or WebKit XPC helper, else None."""
    webkit_root = _webkit_xpc_root(cmd)
    if webkit_root is not None:
        return webkit_root
    cache_root = _automated_cache_root(cmd)
    if cache_root is not None and _has_helper_marker(cmd):
        return cache_root
    return None


def is_detached_browser_helper(cmd: str) -> bool:
    """Whether `cmd` is a detached macOS browser or WebKit XPC helper eligible for lease attribution."""
    if _helper_browser_root(cmd) is not None:
        return True
    return bool(_isolated_profile_paths(cmd) and _has_helper_marker(cmd))


def _driver_browser_root(cmd: str) -> str | None:
    """Automated browser bundle root if `cmd` is a main browser/Playwright driver (never a helper), else None."""
    stripped = cmd.strip()
    if not stripped or _helper_browser_root(stripped) is not None or _has_helper_marker(stripped):
        return None
    shell_pw = _SHELL_PW_RUN_RE.match(stripped)
    if shell_pw:
        root = _normalize_path(shell_pw.group("root"))
        return root if root and not root.startswith(_SYSTEM_BROWSER_PREFIXES) else None
    if stripped.startswith(_SYSTEM_BROWSER_PREFIXES):
        return None
    webkit_driver = _WEBKIT_DRIVER_RE.match(stripped)
    if webkit_driver:
        return _normalize_path(webkit_driver.group("root"))
    return _automated_cache_root(stripped)


def detached_browser_helpers(
    parents: Mapping[int, int],
    commands: Mapping[int, str],
    holders: Sequence[object],
    *,
    uids: Mapping[int, int] | None = None,
    starts: Mapping[int, float | None] | None = None,
    lease_since: Sequence[float | None] | None = None,
    extra_owned: Mapping[int, Iterable[int]] | None = None,
) -> dict[int, set[int]]:
    """Attribute detached macOS browser and WebKit XPC helper processes (`PPID=1`) to their owning live lease.

    Conservative rules (fathers#202):
    - Only processes outside every lease's tree whose parent is PID 1 and whose command is an automated browser/XPC
      helper (e.g. non-system `com.apple.WebKit.*.xpc/...`, `ms-playwright/<browser>/...` helpers, `.cache/puppeteer`
      helpers, or browser helpers carrying an isolated temp profile directory) are considered.
    - Unrelated user/system browsers (`/System/Library/Frameworks/WebKit...`, `/Applications/Safari.app`, Chrome,
      Firefox) are never matched.
    - When a helper carries an isolated temp profile path (`--user-data-dir`, `-profile`, `--disk-cache-dir`,
      `--data-path`), it matches only when that exact profile path appears exclusively in a single live lease's tree.
    - Otherwise it matches by automated browser bundle/cache root only when a single live lease owns all live driver
      processes for that root (and no competing lease or unleased process runs a driver from the same root).
    - When `uids` is supplied, the helper must match the lease holder's UID; when `starts` and `lease_since` are
      supplied, helpers started more than 2 s before the lease are excluded.
    - Each attributed PID (and its live descendants) belongs to at most one lease (never double-counted).
    """
    if not holders or not parents or not commands:
        return {}
    children = _lineage.children_of(parents)
    pid_owners: dict[int, set[int]] = {}
    holder_uid: dict[int, int | None] = {}
    for index, holder in enumerate(holders):
        roots: list[object] = [holder, *(extra_owned.get(index, ()) if extra_owned else ())]
        for member in _lineage.tree(children, roots, parents.__contains__):
            pid_owners.setdefault(member, set()).add(index)
        holder_uid[index] = uids.get(holder) if uids is not None and type(holder) is int else None
    all_owned = set(pid_owners)

    candidates = sorted(
        pid for pid, ppid in parents.items()
        if ppid == 1 and pid not in all_owned and is_detached_browser_helper(commands.get(pid, ""))
    )
    if not candidates:
        return {}
    helper_subtrees: set[int] = set()
    for pid in candidates:
        for member in _lineage.tree(children, [pid], parents.__contains__):
            if member not in all_owned:
                helper_subtrees.add(member)

    root_owners: dict[str, set[int | None]] = {}
    profile_owners: dict[str, set[int | None]] = {}
    for pid in parents:
        if pid in helper_subtrees:
            continue
        cmd = commands.get(pid, "")
        if not cmd:
            continue
        owners: set[int | None] = set(pid_owners[pid]) if pid in pid_owners else {None}
        driver_root = _driver_browser_root(cmd)
        if driver_root is not None:
            root_owners.setdefault(driver_root, set()).update(owners)
        for profile in _isolated_profile_paths(cmd):
            profile_owners.setdefault(profile, set()).update(owners)

    attributed: dict[int, set[int]] = {}
    claimed: set[int] = set()
    for pid in candidates:
        cmd = commands.get(pid, "")
        helper_profiles = _isolated_profile_paths(cmd)
        matched_index: int | None = None
        if helper_profiles:
            seen_owners: set[int | None] = set()
            for profile in helper_profiles:
                if profile in profile_owners:
                    seen_owners.update(profile_owners[profile])
            if len(seen_owners) == 1:
                sole = next(iter(seen_owners))
                if sole is not None:
                    matched_index = sole
        else:
            helper_root = _helper_browser_root(cmd)
            if helper_root is not None:
                owners = root_owners.get(helper_root, set())
                if len(owners) == 1:
                    sole = next(iter(owners))
                    if sole is not None:
                        matched_index = sole
        if matched_index is None:
            continue
        if uids is not None:
            expected_uid = holder_uid.get(matched_index)
            actual_uid = uids.get(pid)
            if expected_uid is None or actual_uid is None or actual_uid != expected_uid:
                continue
        if starts is not None and lease_since is not None and matched_index < len(lease_since):
            since = lease_since[matched_index]
            begun = starts.get(pid)
            if since is not None and begun is not None and begun < since - 2.0:
                continue
        members = [
            member for member in _lineage.tree(children, [pid], parents.__contains__)
            if member not in all_owned and member not in claimed
        ]
        if members:
            claimed.update(members)
            attributed.setdefault(matched_index, set()).update(members)
    return attributed


def attributed_browser_helpers(
    pids: Sequence[int],
    usage_rows: Mapping[int, tuple[Any, ...]],
    root: int,
    since: float,
) -> set[int]:
    """Resolve detached macOS browser/XPC helpers among `pids` for a running command `root` (TreeUsage/Watch).
    Avoids a full command scan unless at least one candidate in `pids` is a detached browser/XPC helper, and validates
    process start times between `usage_rows` and the fresh command snapshot before attributing."""
    if not pids or root not in usage_rows:
        return set()
    targeted = commands(pids)
    if not any(is_detached_browser_helper(cmd) for cmd in targeted.values()):
        return set()
    ctx = helper_snapshot()
    if ctx is None:
        return set()
    root_start = str(usage_rows[root][1])
    if root not in ctx.raw_starts or not _platform.same_holder(root_start, None, root, ctx.raw_starts[root]):
        return set()
    found = detached_browser_helpers(
        ctx.parents, ctx.commands, [root], uids=ctx.uids, starts=ctx.starts, lease_since=[since],
    ).get(0, set())
    wanted = set(pids)
    return {
        pid for pid in found
        if pid in wanted and pid in usage_rows and pid in ctx.raw_starts
        and _platform.same_holder(str(usage_rows[pid][1]), None, pid, ctx.raw_starts[pid])
    }


def starts(pids: Iterable[int], timeout: float | None = None, strict: bool = False) -> dict[int, tuple[str, bool]]:
    """pid -> (lstart in the C locale, a zombie) for those of `pids` still in the process table, from one targeted
    ps. When ps gives no answer: {} (the sweep's check before a signal), or with `strict` PsError (running(): a pid
    that cannot be re-checked is never read as gone)."""
    wanted = list(pids)
    if not wanted:
        return {}
    try:
        result = run(["-o", STARTS, "-p", ",".join(map(str, wanted))], env=_platform.c_locale(), timeout=timeout)
    except PsError:
        if strict:
            raise
        return {}
    if strict and (result.returncode not in (0, 1) or result.returncode and result.stderr.strip()):
        raise PsError(f"ps exited {result.returncode}: {result.stderr.strip()[:200]}", f"ps exited {result.returncode}")
    found: dict[int, tuple[str, bool]] = {}
    for line in result.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit():
            found[int(parts[0])] = (" ".join(parts[2].split()), parts[1].startswith("Z"))
    return found


def running(identities: Mapping[int, str], timeout: float | None = 5) -> set[int]:
    """Those of `identities` (pid -> start time as this client's ps or native table showed it) that still run as
    that same process (starts(), strict): one that exited, became a zombie or whose pid now names a process with
    another start time is not among them (fathers#158: a short-lived helper that exits between a scan and a read of
    its cwd or environment is gone, not unknown). Raises PsError when ps gives no answer."""
    found = starts(identities, timeout, strict=True)
    return {pid for pid, (start, zombie) in found.items()
            if pid in identities and not zombie and _platform.same_holder(identities[pid], None, pid, start)}


def process_uid(pid: object) -> int | None:
    """The real uid a live process runs as, or None when it is not running or cannot be inspected."""
    if type(pid) is not int or pid < 1:
        return None
    try:
        result = run(["-o", OWNER, "-p", str(pid)], timeout=5)
    except PsError:
        return None
    words = result.stdout.split()
    return int(words[0]) if result.returncode == 0 and len(words) == 1 and words[0].isdigit() else None


def names(pids: Iterable[int], timeout: float = 5) -> dict[int, str]:
    """pid -> the program name (ps `comm`, its path's last part) of those of `pids` still running; {} when ps gives
    no answer (a caller that needs to know reads that as "not known")."""
    wanted = sorted(set(pids))
    if not wanted:
        return {}
    try:
        result = run(["-o", NAMES, "-p", ",".join(map(str, wanted))], env=_platform.c_locale(), timeout=timeout)
    except PsError:
        return {}
    found: dict[int, str] = {}
    for line in result.stdout.splitlines():
        number, _, name = line.strip().partition(" ")
        if number.isdigit() and name.strip():
            found[int(number)] = os.path.basename(name.strip())
    return found


def commands(pids: Iterable[int], timeout: float = 5) -> dict[int, str]:
    """pid -> the command line (arguments, never the environment) of those of `pids` still running, from one
    targeted ps (arguments()); {} when ps gives no answer (a caller reads that as "not known")."""
    wanted = set(pids)
    if not wanted:
        return {}
    try:
        answer = arguments(wanted, timeout)
    except PsError:
        return {}
    found: dict[int, str] = {}
    for line in answer.stdout.splitlines():
        number, _, command = line.strip().partition(" ")
        if number.isdigit() and command.strip():
            found[int(number)] = command.strip()
    return found


def run_pid(key: str) -> int | None:
    try:
        pid = int(key)
    except ValueError:
        return None
    return pid if key.isdecimal() and pid > 1 else None


def present(pid: int) -> bool | None:
    """Whether the kernel still knows the pid, which may be a zombie."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OverflowError):
        return None
    return True


def run_alive(pid: int, start: object, started: object, table: Mapping[int, tuple[int, str]] | None) -> bool | None:
    """True for the same live run, False for an exit or proven reuse, None if it cannot be checked."""
    known = present(pid)
    if known is not True or table is None:
        return False if known is False else None
    current = table.get(pid)
    if current is None:
        # A live-only table omits zombies as well as processes it could not inspect.
        try:
            targeted = starts([pid], strict=True).get(pid)
        except PsError:
            return None
        return False if targeted is not None and targeted[1] else None
    return _platform.verified_holder(start, started, pid, current[1])


def other_work(table: dict[int, tuple[int, str]], root: object, caller: int) -> bool:
    """Whether a process tree has work beside the caller, its ancestors or its children."""
    parents = _lineage.parents_of(table)
    mine = {*_lineage.ancestors(parents, caller), *_lineage.descendants(table, caller)}
    others = [pid for pid in _lineage.descendants(table, root) if pid not in mine]
    return work_remains(others)


def work_remains(pids: list[int]) -> bool:
    programs = names(pids)
    return any(programs.get(pid) not in ("ps", "lsof") for pid in pids)


def session_lock_idle(active: object, caller: int, table: dict[int, tuple[int, str]]) -> bool:
    """Only the registered runner's direct command can prove it blocks that runner's foreground work."""
    current = table.get(caller)
    if current is None or not isinstance(active, dict):
        return False
    runner = current[0]
    if str(runner) not in active:
        return False
    running: list[int] = []
    for key, identity in cast(dict[str, object], active).items():
        pid = run_pid(key)
        if pid is None or not isinstance(identity, dict):
            return False
        found = cast(Mapping[str, object], identity)
        alive = run_alive(pid, found.get("start"), found.get("started"), table)
        if alive is None:
            return False
        if alive:
            running.append(pid)
    if running != [runner]:
        return False
    # A shell may have started work before exec'ing the lock. Such work is now descended from the lock caller.
    others = [pid for pid in _lineage.descendants(table, runner)
              if pid not in (runner, caller) and present(pid) is not False]
    return not work_remains(others)


def arguments(pids: Iterable[int], timeout: float = 5) -> subprocess.CompletedProcess[str]:
    """One targeted /bin/ps of the full arguments of `pids`, without any environment (macos_process.snapshot())."""
    return run(["-ww", "-p", ",".join(map(str, sorted(pids))), "-o", ARGUMENTS], env=_platform.c_locale(),
               timeout=timeout, executable="/bin/ps")


# Lease markers in environments

def lease_markers(pids: Sequence[int], leases: Iterable[str]) -> tuple[dict[int, set[str]], list[int]]:
    """Which of `leases` (the paths SLOTFATHER_LEASE holds) each of `pids` carries in its environment, for each pid
    whose environment could be read, and the pids whose environment could not be read (unknown, never "not
    carrying"). Only markers are read; no environment contents are kept or returned.
    - Linux: /proc/PID/environ (the user's own processes; every one for root).
    - macOS: one `ps -wwE`; a pid it shows a marker for is read. For the others ps cannot prove that it showed the
      environment (it prints nothing for a hidden one), so the kernel's copy (macos_process.environment()) decides:
      an Apple platform binary's or another user's is hidden, and stays unknown."""
    paths = set(leases)
    found: dict[int, set[str]] = {}
    if not pids or not paths:
        return found, []
    if _platform.linux():
        unknown: list[int] = []
        for number in pids:
            try:
                fields = _platform.proc_path(str(number), "environ").read_bytes().split(b"\0")
            except OSError:
                unknown.append(number)
                continue
            found[number] = {path for path in paths if os.fsencode(MARKER + path) in fields}
        return found, unknown
    try:
        result = run(["-wwE", "-o", ENVIRONMENT, "-p", ",".join(map(str, pids))], env=_platform.c_locale())
    except PsError:
        return found, list(pids)
    if result.returncode and not result.stdout.strip():
        return found, list(pids)
    wanted = set(pids)
    for line in result.stdout.splitlines():
        number, _, rest = line.strip().partition(" ")
        if number.isdigit() and int(number) in wanted:
            marked = {path for path in paths if f" {MARKER}{path} " in f" {rest} "}
            if marked:
                found[int(number)] = marked
    from . import macos_process
    for number in pids:
        if number in found:
            continue
        environment = macos_process.environment(number)
        if environment is not None:
            found[number] = {path for path in paths if os.fsencode(MARKER + path) in environment}
    return found, [number for number in pids if number not in found]


def carrying(pids: list[int], lease: str) -> set[int]:
    """Those of `pids` whose environment holds SLOTFATHER_LEASE=<lease> (lease_markers()); one whose environment
    cannot be read is not among them."""
    found, _ = lease_markers(pids, [lease])
    return {pid for pid, marked in found.items() if marked}


# Lease membership

class Lease(NamedTuple):
    """A live lease as membership needs it: the path its SLOTFATHER_LEASE holds, its holder's pid, whether a
    process outside its tree can belong to it by that marker (`status --json --pid` counts an earlier queue's
    lease by its tree alone), and optionally its start epoch for detached helper correlation."""
    path: str
    pid: object
    by_environment: bool = True
    since: float | None = None


class Membership(NamedTuple):
    """lease_membership()'s answer: pid -> [(index into the leases, "tree" | "env" | "helper")], in the leases' order,
    and the pids whose environment could not be read where it decides (unknown: neither in nor out of those leases)."""
    leases: dict[int, list[tuple[int, str]]]
    unknown: list[int]


Markers = Callable[[Sequence[int], Iterable[str]], "tuple[dict[int, set[str]], list[int]]"]


def lease_membership(parents: Mapping[int, int], leases: Sequence[Lease], pids: Iterable[int],
                     markers: Markers | None = lease_markers, detached_only: bool = False,
                     commands: Mapping[int, str] | None = None,
                     uids: Mapping[int, int] | None = None,
                     starts: Mapping[int, float | None] | None = None,
                     lease_since: Sequence[float | None] | None = None) -> Membership:
    """The one rule for which live leases each of `pids` belongs to (fathers#38): a lease's holder or one of its
    descendants is in it ("tree"); a process outside a lease's tree is in it when its environment carries the
    lease's SLOTFATHER_LEASE ("env": it left the tree by a double fork or setsid), or when `commands` identifies it
    unambiguously as a detached macOS browser/XPC helper spawned by that lease ("helper", fathers#202). A pid that is
    not live (not in `parents`) belongs to none. A pid outside some lease's tree whose environment cannot be read and
    that is not attributed as a detached helper is unknown.
    `markers` reads the environments of all the pids that need it at once; None reads none (the tree alone). With
    `detached_only` only a pid in no lease's tree is looked for by its environment (`procs` and the sweep); without
    it, a pid in one lease's tree may carry another's marker (`status --json --pid`)."""
    found: dict[int, list[tuple[int, str]]] = {}
    outside: dict[int, list[int]] = {}
    for pid in pids:
        if pid not in parents:
            continue
        chain = {pid, *_lineage.ancestors(parents, pid)}
        found[pid] = [(index, "tree") for index, lease in enumerate(leases) if lease.pid in chain]
        rest = [index for index, lease in enumerate(leases) if lease.pid not in chain and lease.by_environment]
        if rest and not (detached_only and found[pid]):
            outside[pid] = rest
    unknown: list[int] = []
    if markers is not None and outside:
        marked, hidden = markers(list(outside), sorted({leases[i].path for rest in outside.values() for i in rest}))
        unknown = sorted(pid for pid in hidden if pid in outside)
        for pid, rest in outside.items():
            carried = marked.get(pid, set())
            found[pid] = sorted(found[pid] + [(index, "env") for index in rest if leases[index].path in carried])
    if commands is not None and leases and any(not memberships for memberships in found.values()):
        effective_since = [lease.since for lease in leases] if lease_since is None else lease_since
        extra_owned: dict[int, set[int]] = {}
        for pid, memberships in found.items():
            for index, via in memberships:
                if via == "env":
                    extra_owned.setdefault(index, set()).add(pid)
        attributed = detached_browser_helpers(
            parents, commands, [lease.pid for lease in leases],
            uids=uids, starts=starts, lease_since=effective_since, extra_owned=extra_owned or None,
        )
        resolved: set[int] = set()
        for index, helper_pids in attributed.items():
            if not leases[index].by_environment:
                continue
            for pid in helper_pids:
                if pid in found and not found[pid]:
                    found[pid] = [(index, "helper")]
                    resolved.add(pid)
        if resolved and unknown:
            unknown = [pid for pid in unknown if pid not in resolved]
    return Membership(found, unknown)
