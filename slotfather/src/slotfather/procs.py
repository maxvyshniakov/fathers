"""Read-only process identities, session ancestry and lease membership.

A query is an observation, never a queue operation: no lock, directory creation,
reaping or history append is performed. Consumers must revalidate ``pid,start``
before acting on a process because an observation cannot prevent PID reuse.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, cast

from . import config, platform, state
from . import lineage as _lineage
from . import process as _process
from . import entry as _entry


class ProcsError(config.SlotError):
    """Process membership could not be established."""


Process = _process.Process


def read_processes(pid: int | None = None, required: set[int] | None = None) -> dict[int, Process]:
    """One locale-stable process table; failure is never an empty answer."""
    try:
        return _process.member_processes(pid, required)
    except _process.PsError as error:
        message = str(error) if error.detail.startswith("ps ") else f"cannot read processes: {error.detail}"
        raise ProcsError(message) from None


def ancestors(processes: dict[int, Process], pid: int) -> list[int]:
    """Nearest parent first; cycles and PID 1 end the chain."""
    return _lineage.ancestors({number: process.ppid for number, process in processes.items()}, pid)


def session(process: Process) -> bool:
    return _lineage.session_command(process.cmd, process.tty)


def lease_entries(directory: Path | None = None,
                  unknown: list[tuple[Path, dict[str, Any], bool]] | None = None) -> list[tuple[Path, dict[str, Any], bool]]:
    """Validate raw queue entries once before observing the process identities they require. An entry this client
    does not know (entry.unknown(), without the arrival) whose holder can be checked goes to `unknown`, where given, for the caller to
    check against the process table (fathers#19 review); any other invalid entry raises."""
    directory = directory or config.state_dir()
    sources = [(directory, False), *[(path, True) for path in config.legacy_dirs()]]
    found: list[tuple[Path, dict[str, Any], bool]] = []
    for source, old in sources:
        if old and source.is_file():
            continue  # Installers retire legacy queues with file guards.
        state.schema_version(source)
        try:
            with os.scandir(source / "queue") as queue:
                paths = sorted(Path(item.path) for item in queue if item.name.endswith(".json"))
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as error:
            raise ProcsError(f"cannot read queue {source}: {error}") from None
        for path in paths:
            try:
                raw = json.loads(path.read_text())
                entry = cast(dict[str, Any], raw) if isinstance(raw, dict) else None
            except FileNotFoundError:
                continue  # A holder released while this observation was being made.
            except (OSError, ValueError) as error:
                raise ProcsError(f"cannot read lease {path}: {error}") from None
            if entry is None or not _entry.valid_identity(entry):
                if unknown is not None and entry is not None and _entry.unknown(entry, _entry.IDENTITY) is not None \
                        and _entry.checkable(entry):
                    unknown.append((path, entry, old))
                    continue
                raise ProcsError(f"invalid lease identity at {path}")
            if old and not _entry.is_held(entry):
                continue
            found.append((path, entry, old))
    return found


def read_leases(processes: dict[int, Process], entries: list[tuple[Path, dict[str, Any], bool]]) -> list[dict[str, Any]]:
    """Join live identities without changing stale entries on disk."""
    leases: list[dict[str, Any]] = []
    for path, entry, old in entries:
        holder = processes.get(entry["pid"])
        start = str(entry["start"])
        if holder is None or not platform.same_holder(start, entry.get("started"), holder.pid, holder.start):
            continue
        meta = _entry.meta_of(entry)
        priority = _entry.priority_of(entry)
        leases.append({"id": path.stem, "lease": str(path), "state": entry["state"],
                       "count": entry["count"], "label": entry.get("label"), "pid": holder.pid,
                       "start": holder.start, "priority": priority, "ticket": _entry.ticket_of(entry),
                       "meta": meta, "legacy": old})
    return leases


def read_cwds(processes: dict[int, Process], uid: int, exempt: set[int] | None = None) -> dict[int, str]:
    """Read local working directories; inaccessible processes remain explicitly unknown unless they are non-dumpable
    long-lived managed services on Linux (`exempt` receives their PIDs; fathers#164)."""
    units: dict[tuple[bool, str], int] = {}

    def on_denied(denied_pid: int) -> None:
        if exempt is None:
            return
        proc = processes[denied_pid]
        parent = processes.get(proc.ppid)
        if _process.is_systemd_service_main(
            denied_pid,
            proc.ppid,
            proc.cmd,
            parent.cmd if parent is not None else None,
            uid,
            units,
            fail_closed=True,
            timeout=5,
        ):
            exempt.add(denied_pid)

    try:
        found, _, _ = _process.working_dirs(
            {number: proc.uid for number, proc in processes.items()},
            uid,
            timeout=10,
            on_denied=on_denied if exempt is not None else None,
            strict=True,
        )
    except _process.PsError as error:
        raise ProcsError(str(error)) from None
    return {number: item.path for number, item in found.items()}


def recheck(processes: dict[int, Process], cwd_unknown: list[int],
            lease_unknown: list[int]) -> tuple[list[int], list[int], list[dict[str, Any]]]:
    """Keep of the pids whose cwd or environment could not be read those that still run as the process the table
    showed (fathers#158): a short-lived helper (lsof, git, a shell) that exited between the scan and the read is
    gone, not unknown, and would otherwise refuse every `lanefather done` on a busy machine. Returns both lists and
    one record per unknown pid with its start and command, so a consumer can name it. A re-check that gets no
    answer fails the query: an unknown is never dropped unchecked."""
    wanted = set(cwd_unknown) | set(lease_unknown)
    try:
        live = _process.running({number: processes[number].start for number in wanted})
    except _process.PsError as error:
        message = str(error) if error.detail.startswith("ps ") else f"cannot re-check processes: {error.detail}"
        raise ProcsError(message) from None
    cwd = [number for number in cwd_unknown if number in live]
    lease = [number for number in lease_unknown if number in live]
    named = [{"pid": number, "start": processes[number].start, "cmd": processes[number].cmd,
              "cwd_unknown": number in cwd, "lease_unknown": number in lease} for number in sorted(live)]
    return cwd, lease, named


def query(pid: int | None = None, cwd: str | None = None, directory: Path | None = None) -> dict[str, Any]:
    """Observe processes; ``--pid`` is exact and does not scan working directories."""
    strange: list[tuple[Path, dict[str, Any], bool]] = []
    entries = lease_entries(directory, strange)
    processes = read_processes(pid, {entry["pid"] for _, entry, _ in [*entries, *strange]})
    leases = read_leases(processes, entries)
    warnings: list[str] = []
    for path, entry, _ in strange:
        holder = processes.get(entry["pid"])
        if holder is not None and platform.same_holder(entry["start"], entry.get("started"), holder.pid,
                                                       holder.start):
            raise ProcsError(f"invalid lease identity at {path}: an entry this Slotfather does not know "
                             f"({_entry.unknown(entry, _entry.IDENTITY)}) whose holder runs")
        # Its holder is gone: it protects nothing, so it is read as absent; nothing removes it (fathers#19)
        warnings.append(f"ignored {path}: an entry this Slotfather does not know "
                        f"({_entry.unknown(entry, _entry.IDENTITY)}) whose "
                        f"holder is not running; remove the file to clear it")
    selected = sorted(processes) if pid is None else ([pid] if pid in processes else [])
    exempt_cwds: set[int] = set()
    cwds = read_cwds(processes, os.geteuid(), exempt_cwds) if cwd is not None else {}
    unknown: list[int] = []
    if cwd is not None:
        cwd = os.path.realpath(os.path.expanduser(cwd))
        own = {os.getpid(), *ancestors(processes, os.getpid())}
        unknown = sorted(number for number, process in processes.items()
                         if process.uid == os.geteuid() and number not in cwds and number not in own
                         and number not in exempt_cwds)
        selected = [number for number in selected if processes[number].uid == os.geteuid() and number not in own
                    and number in cwds and (cwds[number] == cwd or cwds[number].startswith(cwd.rstrip("/") + "/"))]
    parents = {number: process.ppid for number, process in processes.items()}
    lease_specs = [_process.Lease(str(lease["lease"]), lease["pid"]) for lease in leases]
    entry_by_path = {str(path): entry for path, entry, _ in entries}
    lease_since = [_process.lease_started_epoch(entry_by_path.get(str(lease["lease"]), {})) for lease in leases]
    if platform.macos() and pid is None:
        membership = _process.lease_membership(
            parents, lease_specs, selected, detached_only=True,
            commands={number: process.cmd for number, process in processes.items()},
            uids={number: process.uid for number, process in processes.items()},
            starts={number: platform.started_at(process.start) for number, process in processes.items()},
            lease_since=lease_since,
        )
    else:
        membership = _process.lease_membership(parents, lease_specs, selected, detached_only=True)
        if pid is not None and pid in processes and leases:
            membership = _process.resolve_pid_helper_membership(
                pid,
                processes[pid].start,
                parents,
                lease_specs,
                {number: process.start for number, process in processes.items()},
                lease_since,
                membership,
                pid_cmd=processes[pid].cmd,
            )
    unknown, lease_unknown, named = recheck(processes, unknown, membership.unknown)
    records: list[dict[str, Any]] = []
    for number in selected:
        process = processes[number]
        chain = _lineage.ancestors(parents, number)
        records.append({**process._asdict(), "cwd": cwds.get(number), "ancestors": chain,
                        "sessions": [{"pid": item, "start": processes[item].start, "cmd": processes[item].cmd}
                                     for item in [number, *chain] if session(processes[item])],
                        "leases": [{**leases[index], "via": via} for index, via in membership.leases.get(number, [])]})
    return {"v": 2, "query": {"pid": pid, "cwd": cwd, "start": processes[pid].start if pid in processes else None},
            "cwd_unknown": unknown, "lease_unknown": lease_unknown, "unknown": named, "processes": records,
            "warnings": warnings}


USAGE = "usage: slotfather procs --json [--pid PID] [--cwd PATH]"


def command(argv: list[str]) -> int:
    """CLI adapter; only explicit JSON queries are accepted."""
    found: dict[str, str | bool] = {}
    at = 0
    while at < len(argv):
        option = argv[at]
        if option in found or option not in ("--json", "--pid", "--cwd"):
            raise ProcsError(USAGE)
        if option == "--json":
            found[option] = True
            at += 1
        elif at + 1 < len(argv) and argv[at + 1] and not argv[at + 1].startswith("--"):
            found[option] = argv[at + 1]
            at += 2
        else:
            raise ProcsError(USAGE)
    if "--json" not in found:
        raise ProcsError(USAGE)
    pid: int | None = None
    if "--pid" in found:
        value = str(found["--pid"])
        if not value.isascii() or not value.isdigit() or int(value) <= 0:
            raise ProcsError(USAGE)
        pid = int(value)
    cwd = str(found["--cwd"]) if "--cwd" in found else None
    print(json.dumps(query(pid=pid, cwd=cwd)))
    return 0
