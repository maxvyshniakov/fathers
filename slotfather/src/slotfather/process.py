"""Process observation: the one module that runs `ps` (fathers#38).

It holds the process-table readers with their named column sets (run and parsed by the shared reader,
`fathers_common.proc`, fathers#171), the reader of SLOTFATHER_LEASE markers in other processes' environments, and
lease_membership(), the one rule for which live lease a process belongs to (`status --json --pid`, `procs --json`
and the sweep's protection).

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
from typing import Any, Callable, Iterable, Mapping, NamedTuple, Sequence, cast

from fathers_common import proc as _proc

from . import config as _config
from . import lineage as _lineage
from . import platform as _platform

# The column sets each reader asks ps for; every one starts with the pid.
IDENTITY = "pid=,ppid=,stat=,lstart="  # processes(): the queue's identities
USAGE = "pid=,ppid=,uid=,rss=,time=,lstart="  # usage_table(): the lease's sampler
MEMBERS = "pid=,ppid=,uid=,tty=,stat=,lstart=,command="  # procs: identities, sessions and commands
SWEEP = "pid=,ppid=,uid=,rss=,tty=,lstart=,command="  # the sweep's snapshot
CPU = "pid=,ppid=,%cpu=,rss=,comm="  # the dashboard block's CPU sample
HELPER_SNAPSHOT = "pid=,ppid=,uid=,lstart=,command="  # macOS detached browser/XPC correlation snapshot
OUTSIDE = "pid=,ppid=,pgid=,uid=,pcpu=,lstart=,command="  # outside_snapshot(): who uses the machine outside leases
STARTS = "pid=,stat=,lstart="  # the sweep's check before a signal; running()
OWNER = "uid="  # process_uid()
NAMES = "pid=,comm="  # names(): the program of a process (the lock wait's helpers)
ENVIRONMENT = "pid=,command="  # with -wwE: the command and, where shown, the environment
ARGUMENTS = "pid=,command="  # the full arguments, without the environment

MARKER = "SLOTFATHER_LEASE="


class PsError(_config.ProcessTableError, _proc.ProcTableError):
    """ps gave no usable answer; `detail` is the cause without the "ps" prefix some callers word themselves."""

    def __init__(self, message: str, detail: str) -> None:
        super().__init__(message)
        self.detail = detail


def ps_environment() -> dict[str, str] | None:
    """The environment ps runs in for processes(), whose lstart a new entry writes as its `start`: on Linux the C
    locale (procps writes lstart in the locale's words; every client since #653 writes C there); macOS keeps the
    caller's locale and time zone, as every older client does, so that an older client still reads a current
    client's `start` exactly as it read its own (fathers#122). Current clients compare `started` instead."""
    return _platform.c_locale() if _platform.linux() else None


def run(arguments: Sequence[str], *, env: Mapping[str, str] | None = None, timeout: float | None = None,
        executable: str = "ps") -> subprocess.CompletedProcess[str]:
    """One ps run; raises PsError when ps cannot run or takes longer than `timeout` (default PS_TIMEOUT). Its exit
    status is the caller's to judge: `ps -p` exits 1 when some of the pids are gone."""
    try:
        return _proc.run([executable, *arguments], env=env, timeout=_config.PS_TIMEOUT if timeout is None else timeout,
                         runner=subprocess.run)  # this module's binding: tests replace it
    except _proc.ProcTableError as error:
        raise PsError(f"ps: {error.detail}", error.detail) from None


def table(arguments: Sequence[str], *, caller: bool = True, env: Mapping[str, str] | None = None,
          timeout: float | None = None) -> list[str]:
    """The rows (lines whose first word is a pid) of a ps over the machine. Raises PsError for a ps that fails or
    times out, a table without a row, and, with `caller`, a table that leaves out this process: a filtered or cut
    table would read the holders it hides as dead (fathers#121)."""
    result = run(arguments, env=env, timeout=timeout)
    try:
        return _proc.table_rows(result, caller=caller)
    except _proc.ProcTableError as error:
        raise PsError(str(error), error.detail) from None


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
    for line in table(["-axo", CPU], env=_platform.c_locale()):  # it lists the caller (table(): fathers#121)
        parts = line.split(None, 4)
        if len(parts) == 5 and parts[1].isdigit():
            with contextlib.suppress(ValueError):
                own[int(parts[0])] = (float(parts[2].replace(",", ".")), int(parts[3]), os.path.basename(parts[4]))
                children.setdefault(int(parts[1]), []).append(int(parts[0]))
    return own, children


class Process(NamedTuple):
    """Locale-stable process row shared by `procs` and native macOS process reads."""
    pid: int
    ppid: int
    uid: int
    tty: str
    start: str
    cmd: str


Cwd = _proc.Cwd  # observed working directory and whether the kernel/filesystem marks it removed (None: unknown)


def identity_table(required: set[int] | None = None) -> dict[int, tuple[int, str]] | None:
    """Native macOS identity table for `status`, or None when unavailable or off macOS."""
    if not _platform.macos() or required is None:
        return None
    from . import macos_process
    try:
        return macos_process.basic_snapshot(required)
    except OSError:
        return None


def member_processes(
    pid: int | None = None,
    required: set[int] | None = None,
    timeout: float = 5,
) -> dict[int, Process]:
    """One locale-stable process table (native macOS fast path for `--pid`, else `ps`); failure is never empty."""
    if _platform.macos() and pid is not None:
        try:
            from .macos_process import snapshot
            return {number: Process(*proc) for number, proc in snapshot(pid, required or set()).items()}
        except (OSError, ValueError, AttributeError):
            pass  # Unsupported native ABI or required identity: use the full ps adapter.
    rows = table(["-ax", "-ww", "-o", MEMBERS], caller=False, env=_platform.c_locale(), timeout=timeout)
    found: dict[int, Process] = {
        number: Process(number, proc.ppid, proc.uid, proc.tty, proc.start, proc.cmd)
        for number, proc in _proc.parse(rows, MEMBERS, "local").items() if not proc.zombie
    }
    if not found:
        raise PsError("ps listed no live processes", "ps listed no live processes")
    return found


def working_dirs(
    uids_by_pid: Mapping[int, int],
    uid: int,
    *,
    timeout: float = 10,
    check_removed: Callable[[str, int | None], bool | None] | None = None,
    on_denied: Callable[[int], None] | None = None,
    strict: bool = False,
    use_proc: bool | None = None,
) -> tuple[dict[int, Cwd], set[int], list[str]]:
    """Read working directories for `uid`'s processes via `/proc/PID/cwd` (Linux) or `lsof` (macOS), through the
    shared reader (fathers_common.proc.working_dirs, under SLOTFATHER_PROC's /proc).

    Returns `(cwds, foreign_mount_ns_pids, notes)`. With `strict=True` (used by `procs`), failures raise `PsError`
    and paths are normalized with `os.path.realpath`. With `strict=False` (used by `sweep_os`), failures return
    warning notes and removed-directory state is populated via `/proc` or `check_removed`.
    """
    try:
        found = _proc.working_dirs(uids_by_pid, uid, timeout=timeout, check_removed=check_removed, on_denied=on_denied,
                                   strict=strict, use_proc=_platform.linux() if use_proc is None else use_proc,
                                   proc_root=str(_platform.proc_path()), runner=subprocess.run)
    except _proc.ProcTableError as error:
        raise PsError(str(error), error.detail) from None
    return found.paths, found.foreign, found.notes


def adopted_by_manager(ppid: int, parent_cmd: str | None, *, is_linux: bool | None = None) -> bool:
    """Whether launchd/init (or, on Linux, a user's systemd manager) adopted a process."""
    if ppid == 1:
        return True
    linux_mode = _platform.linux() if is_linux is None else is_linux
    return linux_mode and parent_cmd is not None and os.path.basename(_lineage.program(parent_cmd)) == "systemd"


def is_systemd_service_main(
    pid: int,
    ppid: int,
    cmd: str,
    parent_cmd: str | None,
    uid: int,
    units: dict[tuple[bool, str], int],
    *,
    fail_closed: bool = True,
    timeout: float = 5,
    run_main_pid: Callable[[bool, str], int] | None = None,
) -> bool:
    """Whether a same-user Linux process is a managed systemd service main process (or systemd --user / (sd-pam)
    when `fail_closed=True`). `units` caches `(is_user, unit) -> MainPID` across calls."""
    if not adopted_by_manager(ppid, parent_cmd, is_linux=True):
        return False
    try:
        lines = _platform.proc_path(str(pid), "cgroup").read_text().splitlines()
    except OSError:
        return False
    if fail_closed:
        cgroup = next((line.split(":", 2)[2].strip() for line in lines if line.startswith("0::")), "").rstrip("/")
        manager_unit = f"/user@{uid}.service"
        if cgroup.endswith((manager_unit, f"{manager_unit}/init.scope")):
            exe = os.path.basename(_lineage.program(cmd))
            if exe == "systemd" or cmd.strip().startswith("(sd-pam)"):
                return True
    key = _platform.service_of(lines)
    if key is None:
        return False
    if key not in units:
        units[key] = (
            run_main_pid(*key)
            if run_main_pid is not None
            else _platform.main_pid(*key, subprocess.run, timeout=timeout)
        )
    return units[key] == pid if fail_closed else units[key] in (pid, -1)


def launchd_pids(text: str, how: str) -> set[int]:
    """The pids of running jobs in `launchctl list` (PID first on each line) or `launchctl print gui/UID` (its
    services block: pid, status, label) output."""
    found: set[int] = set()
    if how == "list":
        for line in text.splitlines()[1:]:
            first = line.split("\t", 1)[0].strip()
            if first.isdigit() and int(first) > 0:
                found.add(int(first))
        return found
    inside = False
    for line in text.splitlines():
        if line.strip() == "services = {":
            inside = True
        elif inside and line.strip() == "}":
            inside = False
        elif inside:
            first = (line.split() or [""])[0]
            if first.isdigit() and int(first) > 0:
                found.add(int(first))
    return found


def launchd_managed_pids(uid: int, timeout: float = 10) -> set[int]:
    """PIDs of running launchd jobs for `uid` in both the caller's domain and `gui/<uid>`."""
    found: set[int] = set()
    for command in (["launchctl", "list"], ["launchctl", "print", f"gui/{uid}"]):
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
            found |= launchd_pids(result.stdout, command[1])
    return found


class ProcessContext(NamedTuple):
    """Extra per-process metadata for detached macOS browser/XPC helper attribution."""
    parents: dict[int, int]
    uids: dict[int, int]
    starts: dict[int, float | None]
    raw_starts: dict[int, str]
    commands: dict[int, str]


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


def snapshot(spec: str, timeout: float = 5) -> dict[int, _proc.Proc] | None:
    """One full-table ps of the columns `spec` names, with whole commands, as pid -> Proc in local time. None when ps
    fails, times out, or omits its caller (unavailable inspection, never an empty result)."""
    try:
        rows = table(["-axww", "-o", spec], env=_platform.c_locale(), timeout=timeout)
    except PsError:
        return None
    return _proc.parse(rows, spec, "local")


def outside_snapshot(timeout: float = 5) -> dict[int, _proc.Proc] | None:
    """The machine's processes with their group, CPU share and whole command, for what a machine sample stores of
    the use outside leases (outside.py, fathers#239); None when ps cannot give them."""
    return snapshot(OUTSIDE, timeout)


def helper_snapshot(timeout: float = 5) -> ProcessContext | None:
    """One full-table snapshot of pid, ppid, uid, lstart and command for detached browser helper correlation.
    Returns None when ps fails, times out, or omits its caller (unavailable inspection, never an empty result)."""
    found = snapshot(HELPER_SNAPSHOT, timeout)
    if found is None:
        return None
    parents: dict[int, int] = {}
    uids: dict[int, int] = {}
    starts_map: dict[int, float | None] = {}
    raw_starts: dict[int, str] = {}
    cmds: dict[int, str] = {}
    for pid, proc in found.items():
        parents[pid] = proc.ppid
        uids[pid] = proc.uid
        raw_starts[pid] = proc.start
        starts_map[pid] = proc.started
        cmds[pid] = proc.cmd
    return ProcessContext(parents, uids, starts_map, raw_starts, cmds)


def lease_started_epoch(entry: Mapping[str, Any]) -> float | None:
    """Epoch seconds when a held lease began running work (for filtering out older pre-existing detached helpers)."""
    raw = entry.get("granted") or entry.get("since")
    if not isinstance(raw, str) or not raw:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            return datetime.datetime.strptime(raw, fmt).replace(tzinfo=datetime.UTC).timestamp()
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
    return {pid: (proc.start, proc.zombie) for pid, proc in _proc.parse(result.stdout, STARTS, "local").items()}


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


# `markers` receives the queried pids needing environment checks plus their live ancestors (above PID 1) so
# env-cleared descendants of a detached daemon can inherit its SLOTFATHER_LEASE marker.
Markers = Callable[[Sequence[int], Iterable[str]], "tuple[dict[int, set[str]], list[int]]"]


def lease_membership(parents: Mapping[int, int], leases: Sequence[Lease], pids: Iterable[int],
                     markers: Markers | None = lease_markers, detached_only: bool = False,
                     commands: Mapping[int, str] | None = None,
                     uids: Mapping[int, int] | None = None,
                     starts: Mapping[int, float | None] | None = None,
                     lease_since: Sequence[float | None] | None = None) -> Membership:
    """The one rule for which live leases each of `pids` belongs to (fathers#38): a lease's holder or one of its
    descendants is in it ("tree"); a process outside a lease's tree is in it when its environment or a live
    ancestor's environment carries the lease's SLOTFATHER_LEASE ("env": it left the tree by a double fork or setsid,
    or descended from a detached daemon whose child cleared its own environment), or when `commands` identifies it
    unambiguously as a detached macOS browser/XPC helper spawned by that lease ("helper", fathers#202). A pid that is
    not live (not in `parents`) belongs to none. A pid outside some lease's tree whose own environment cannot be read
    and that is not attributed via a readable ancestor's marker or as a detached helper is unknown (an unreadable
    ancestor alone does not mark a readable descendant unknown, since managers like `systemd --user` or root shells
    often hide their environments).
    `markers` reads the environments of all the pids that need it at once; None reads none (the tree alone). With
    `detached_only` only a pid in no lease's tree is looked for by its environment (`procs` and the sweep); without
    it, a pid in one lease's tree may carry another's marker (`status --json --pid`)."""
    found: dict[int, list[tuple[int, str]]] = {}
    outside: dict[int, list[int]] = {}
    chains: dict[int, list[int]] = {}
    for pid in pids:
        if pid not in parents:
            continue
        anc = _lineage.ancestors(parents, pid)
        chain = {pid, *anc}
        found[pid] = [(index, "tree") for index, lease in enumerate(leases) if lease.pid in chain]
        rest = [index for index, lease in enumerate(leases) if lease.pid not in chain and lease.by_environment]
        if rest and not (detached_only and found[pid]):
            outside[pid] = rest
            chains[pid] = [pid, *anc]
    unknown: list[int] = []
    if markers is not None and outside:
        probe = list(dict.fromkeys(member for chain_pids in chains.values() for member in chain_pids))
        marked, hidden = markers(probe, sorted({leases[i].path for rest in outside.values() for i in rest}))
        hidden_set = set(hidden)
        resolved_env: set[int] = set()
        for pid, rest in outside.items():
            carried: set[str] = set()
            for member in chains[pid]:
                carried.update(marked.get(member, ()))
            env_matches = [(index, "env") for index in rest if leases[index].path in carried]
            if env_matches:
                found[pid] = sorted(found[pid] + env_matches)
                resolved_env.add(pid)
        unknown = sorted(pid for pid in hidden_set if pid in outside and pid not in resolved_env)
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


def resolve_pid_helper_membership(
    pid: int,
    pid_start: str,
    parents: Mapping[int, int],
    lease_specs: Sequence[Lease],
    holder_starts: Mapping[int, str],
    lease_since: Sequence[float | None],
    membership: Membership,
    *,
    pid_cmd: str | None = None,
) -> Membership:
    """Targeted macOS detached browser/XPC helper fallback for a single-PID query (`status --pid`, `procs --pid`)."""
    if not _platform.macos() or not lease_specs or parents.get(pid) != 1 or membership.leases.get(pid):
        return membership
    cmd = commands([pid]).get(pid, "") if pid_cmd is None else pid_cmd
    if not cmd or not is_detached_browser_helper(cmd):
        return membership
    helper_ctx = helper_snapshot()
    if helper_ctx is None or pid not in helper_ctx.raw_starts or not _platform.same_holder(
        pid_start, None, pid, helper_ctx.raw_starts[pid]
    ):
        return membership
    valid_holders: list[object] = [
        lease.pid if (
            type(lease.pid) is int
            and lease.pid in helper_ctx.raw_starts
            and lease.pid in holder_starts
            and _platform.same_holder(holder_starts[lease.pid], None, lease.pid, helper_ctx.raw_starts[lease.pid])
        )
        else None
        for lease in lease_specs
    ]
    attributed = detached_browser_helpers(
        helper_ctx.parents,
        helper_ctx.commands,
        valid_holders,
        uids=helper_ctx.uids,
        starts=helper_ctx.starts,
        lease_since=lease_since,
    )
    matched = [
        (index, "helper")
        for index, helper_pids in sorted(attributed.items())
        if lease_specs[index].by_environment and pid in helper_pids
    ]
    if not matched:
        return membership
    return Membership(
        {**membership.leases, pid: matched},
        [item_pid for item_pid in membership.unknown if item_pid != pid],
    )
