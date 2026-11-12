"""slotfather: one queue of requests for the machine's capacity, shared by every project and agent (#599).

  slotfather run [-n N|all] [--label L] [--meta K=V]... [--priority P] [--ticket ID] [--yieldable] [--wait S] [--] COMMAND...
                                                                     run COMMAND holding N slots (default 1)
  slotfather yield                                                   inside a yieldable lease: yield if asked
  slotfather status [--json [--pid PID | --full]]                    running and waiting requests, in queue order
  slotfather capabilities --json                                     what a dashboard (Godfather) can read and run here
  slotfather settings [check|set|reset ...] --json                   the shared settings as data, and changes to them
  slotfather reprioritize PID|LABEL interactive|normal|batch [--start START]  move a request to another class, keeping its place
  slotfather ask-yield PID --start START                             ask a yieldable lease to yield for a higher class
  slotfather procs --json [--cwd PATH] [--pid PID]                  read-only process, lease and session ancestry
  slotfather lane-admission --json                                    stateless lifetime lane policy, stdin JSON
  slotfather version [--json]                                        identify this runtime source
  slotfather history [N]                                             the last N requests that left the queue
  slotfather stats [--since W] [--label L] [--by B] [--json]         request resource use and machine occupancy
  slotfather slots [N|memory G|lower G|reset]                        show, save or reset shared capacity
  slotfather available [--label L] [-- COMMAND...]                   queue space before memory checks
  slotfather advise [--max N] [--exclusive] [--label L] [-- CMD...]  a recommended worker budget, as JSON
  slotfather order [backfill|window] [--safety X] [--limit S] ...    show, save or reset the queue's order
  slotfather sweep [--live [--dev-servers]] [--json] | config | --plist  find (and stop) processes left behind
  slotfather [dashboard [ARGS...]]                                   the live view: runs `godfather ARGS...`

Run heavy builds and tests through `slotfather run -n <k> -- ...`: a test suite or build that keeps the
machine busy takes 1 to 2 slots, a full gate that must not share the machine takes `-n all`.

Slots: one per three cores, at least one, and no more than one per 4 GiB of memory, so
`total = min(max(1, cores // 3), max(1, memory_GiB // 4))`: 4 on a 12-core M2 Max with 32 GiB, 2 on a 6-core VM with
12 GiB (on Linux the memory is the limit of the queue's cgroup where it has one: SLOTFATHER_CGROUP, the one
install.py --cgroup wrote in, the one saved with the capacity, or the caller's outermost limited one). A request
above a capacity that shrank takes every slot at its grant, and -n all the capacity then. `slotfather slots N` saves shared capacity,
which applies at the next grant while requests run or wait: growing it starts waiters at once, shrinking it lets running
leases keep their slots and starts nothing until no more than the new capacity is held (draining); `slots memory G` one slot per G GiB of memory, `slots lower G` the
lower of that and the CPU budget (#653); `slotfather slots reset` restores the automatic default. SLOTFATHER_CORES
replaces the core count, SLOTFATHER_MEMORY_GIB the memory. A request above the total takes every slot.

Requests are served in arrival order. A waiter that does not fit lets later requests that fit start before it until
it has waited SLOTFATHER_OVERTAKE_WINDOW seconds (default 600). Past that it is the head (#643, the backfill order):
its start is reserved for when the held leases are expected to end, and that reservation only ever moves earlier.
A later request that fits still starts first if, by the run times of earlier requests of its kind in the history,
three times its expected run ends before that reserved start, or if it takes only slots the head will not need
then; requests that came after the head pass it only so, whatever their class. So short work keeps flowing beside
a waiting all-slot request, and the request starts once the leases it waited for end, plus the overrun of what
passed it. Classes (`--priority interactive|normal|batch`, normal by default): an interactive waiter queues as if
it came 1800 s earlier, a batch one 1800 s later, and a waiting request moves up a class as it waits (ageing): a
batch one is served as normal after 1800 s, and a normal one as interactive after 3600 s more, keeping its
arrival. A batch request never holds back a waiting normal or interactive one: while such work waits it is no
head, and a batch all-slot request does not start, until it has aged. A waiter whose `--wait` ends before the held
leases are expected to free its slots, counting the overrun of those past their estimate, holds nobody back
either. `slotfather order` shows and saves the order and these numbers for every client (order.json beside the
queue); a client's environment can only tighten them, and SLOTFATHER_QUEUE=window falls back to the earlier order,
where the head holds back everyone behind it. `slotfather reprioritize PID|LABEL CLASS` moves a request (yours) to
another class without losing its place. A request that has waited 1800 s (`order --warn-wait`) says so on its
stderr every 15 minutes, with its expected start, and in queue.log; `status` flags it. An interactive waiter that
has waited 120 s asks holders of a lower class that took `--yieldable` to yield, by a marker file their client
checks between steps (SLOTFATHER_YIELD) before it runs
`slotfather yield`; nothing ever stops a holder. A process
that already runs under a lease (SLOTFATHER_LEASE, or a holder among its ancestors) runs without a second lease, so
nested wrappers neither deadlock nor count twice. A lease whose holder died (its pid gone, or reused by a process
with another start time) is reaped by the next queue operation. While memory is short (free swap under SLOTFATHER_MIN_FREE_SWAP_MB, default 512, where swap is on; on
macOS the kernel's memory-pressure level at critical; on Linux MemAvailable under SLOTFATHER_MIN_AVAILABLE_MB,
default 1024, or the queue's cgroup's memory.max less its working set under that, or PSI memory full avg10, the
cgroup's or else the machine's, at SLOTFATHER_MAX_PSI_FULL percent, default 5), new leases wait for running ones.
Under a memory-pressure warning (macOS level 2; Linux PSI some avg10 at SLOTFATHER_MAX_PSI_SOME percent, default
10) one 1-slot lease at a time starts beside them: another waits until it ends or the pressure is normal again,
and larger requests wait (`order --warning-admits 0`: none start). With nothing running a request starts.
A waiter checks its own entry every SLOTFATHER_POLL seconds (default 2) but looks over the queue, which runs
ps, less often the longer nothing changes, up to SLOTFATHER_POLL_MAX (default 10); a release hands its slots on at
once. `--wait S` gives up after S seconds with exit 75 and leaves the queue. `run` exits with the command's status
(128 + the signal when a signal ended it).

`advise` sizes work for processes that take no lease: with each slot `cores / total` cores, the current load,
min(load1, load5), less what the held slots account for, max(0, load - held x cores per slot), is load without a
lease. Not load15: it still holds work whose leases ended minutes ago, and min() ignores a one-minute spike.
The worker budget is the slots left after that load (and after the held slots, unless --exclusive: an all-slot
request waits for them), a slot counting while at least half its cores are idle, never above --max (default
the total) nor, without --exclusive, the free slots, and at least 1. SLOTFATHER_LOADAVG replaces the load
averages: "load1 load5 load15", or one number for all three.

State: ~/.local/state/slotfather (SLOTFATHER_DIR replaces it; another user's queue is read only: changes are
refused and reading creates nothing; queue.log records grants, releases and
reaps; history.jsonl, which `history` lists, has a line per request that left the queue: its command, directory,
wait, run and outcome, and for `run` what its process tree used; machine.jsonl samples the machine while leases
run; `stats` reads both). While its command runs, the holder looks at the command's process tree every
SLOTFATHER_SAMPLE_MAX seconds at most (default 10; the first look comes sooner): CPU time is the command's own
rusage, which holds every descendant reaped inside the tree, plus the sampled CPU of processes that left the tree
(orphans, and launchd children carrying the lease's SLOTFATHER_LEASE). Held leases in the queues listed in
SLOTFATHER_LEGACY_DIRS (colon-separated; default Straylight's earlier ~/.local/state/straylight-verification/slots;
empty to ignore) count as used, scaled from their 4 slots, until their holders exit. SLOTFATHER_POLICY: normal
(default), background (low QoS), or bypass (no lease; for a person who knows the machine is idle). Standard library
only, Python 3.9 or later: install.py puts thin entry points in ~/.local/bin and preserves their complete package in an immutable release."""
from __future__ import annotations

import json
import math
import os
import sys
from typing import Any

from . import config as _config


GODFATHER_MISSING = ("the dashboard is part of godfather now; install it from "
                     "https://github.com/maxvyshniakov/fathers/tree/main/godfather (python3 install.py godfather)")


def companion(name: str, argv: list[str], title: str) -> int:
    """Load only the requested view; admission never imports optional cleanup or statistics code."""
    from importlib import import_module
    try:
        module = import_module(f"slotfather.{name}")
    except ModuleNotFoundError as error:
        if error.name != f"slotfather.{name}":
            raise
        raise _config.SlotError(f"{title} is not installed") from None
    return module.main(argv)


def dashboard(argv: list[str]) -> int:
    """`slotfather dashboard ARGS`, and a bare `slotfather` in a terminal: the dashboard is Godfather's (#28), so
    this process becomes `godfather ARGS` (its arguments and exit code are Godfather's); 2 when it is not on PATH."""
    import shutil
    found = shutil.which("godfather")
    if found is None:
        print(f"slotfather: {GODFATHER_MISSING}", file=sys.stderr)
        return 2
    sys.stdout.flush()
    sys.stderr.flush()
    import signal
    for name in ("SIGPIPE", "SIGXFSZ"):  # Python ignores these; an exec would hand that on to godfather
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), signal.SIG_DFL)
    try:
        os.execv(found, ["godfather", *argv])
    except OSError as error:
        print(f"slotfather: cannot run {found}: {error.strerror or error}", file=sys.stderr)
        return 126


USAGE = ("usage: slotfather                     (in a terminal: the live dashboard, godfather)\n"
         "       slotfather run [-n N|all] [--label L] [--meta KEY=VALUE]... [--priority interactive|normal|batch] [--ticket ID]\n"
         "                      [--yieldable] [--wait SECONDS] [--] COMMAND...      (inside a lease: slotfather yield)\n"
         "       slotfather status [--json [--pid PID | --full]] | history [N] | available [--label L] [-- COMMAND...]\n"
         "       slotfather reprioritize PID|LABEL interactive|normal|batch [--start START] | ask-yield PID --start START\n"
         "       slotfather lane-admission --json                         (one lifetime request on stdin)\n"
         "       slotfather advise [--max N] [--exclusive] [--label L] [-- COMMAND...]\n"
         "       slotfather order [backfill|window] [--safety X] [--limit S] [--fallback S] [--overrun X]\n"
         "                        [--interactive S] [--batch S] [--yield-after S] [--idle-cpu C] [--idle-after S]\n"
         "                        [--aging-batch S] [--aging-normal S] [--warn-wait S] [--warning-admits 1|0]\n"
         "                        | reset   (S: 90, 90s, 30m, 1.5h)\n"
         "       slotfather stats [--since WHEN] [--label GLOB] [--by both|label|kind] [--top N] [--json]\n"
         "       slotfather slots [N|memory GIB|lower GIB|reset]\n"
         "       slotfather settings --json | settings check KEY VALUE --json | settings set KEY VALUE [--revision R] --json\n"
         "                           | settings reset (KEY|--all) [--revision R] --json\n"
         "       slotfather procs --json [--cwd PATH] [--pid PID] | version [--json] | capabilities --json\n"
         "       slotfather sweep [--live [--dev-servers]] [--json] [--quiet] [--pid PID]... | sweep config [KEY VALUE]...\n"
         "                        | sweep --plist [--live [--dev-servers]] [--interval SECONDS]\n"
         "       slotfather dashboard [ARGS...]                          (runs godfather ARGS...)")


def parse_count(value: str) -> int:
    if value == "all":
        return _config.total()
    if not value.isdigit() or int(value) < 1:
        raise _config.SlotError(f"-n takes a positive count or all, not {value!r}")
    return int(value)


def run_command(argv: list[str]) -> int:
    from . import leases as _leases
    count, label, wait, pairs, yieldable = 1, os.path.basename(os.getcwd()), None, [], False
    priority: str | None = None
    ticket: str | None = None
    while argv:
        if argv[0] in ("-n", "--slots") and len(argv) > 1:
            count, argv = ("all" if argv[1] == "all" else parse_count(argv[1])), argv[2:]
        elif argv[0] in ("-l", "--label") and len(argv) > 1:
            label, argv = argv[1], argv[2:]
        elif argv[0] == "--meta" and len(argv) > 1:
            pairs, argv = pairs + [argv[1]], argv[2:]
        elif argv[0] == "--priority" and len(argv) > 1:
            if argv[1] not in _config.PRIORITIES:
                raise _config.SlotError(f"--priority takes {', '.join(_config.PRIORITIES)}, not {argv[1]!r}")
            priority, argv = argv[1], argv[2:]
        elif argv[0] == "--ticket" and len(argv) > 1:
            ticket, argv = argv[1], argv[2:]
        elif argv[0] == "--yieldable":
            yieldable, argv = True, argv[1:]
        elif argv[0] == "--wait" and len(argv) > 1:
            try:
                wait = float(argv[1])
            except ValueError:
                raise _config.SlotError(f"--wait takes seconds, not {argv[1]!r}") from None
            argv = argv[2:]
        elif argv[0] == "--":
            argv = argv[1:]
            break
        elif argv[0].startswith("-"):
            raise _config.SlotError(f"unknown option {argv[0]}")
        else:
            break
    if not argv:
        raise _config.SlotError("no command to run")
    return _leases.run(count, label, argv, wait, _config.parse_meta(pairs), yieldable, priority=priority, ticket=ticket)


def request_options(argv: list[str], what: str, flags: dict) -> tuple[dict, str, str]:
    """`flags` (option -> takes a value) and then `[--label L] [-- COMMAND...]`: (options, label, command). The
    label defaults to the directory's name, as for run."""
    found, label, command = {}, os.path.basename(os.getcwd()), ""
    while argv:
        if argv[0] == "--":
            command = " ".join(argv[1:])
            break
        if argv[0] in ("-l", "--label") and len(argv) > 1:
            label, argv = argv[1], argv[2:]
        elif argv[0] in flags and (not flags[argv[0]] or len(argv) > 1):
            found[argv[0]] = argv[1] if flags[argv[0]] else True
            argv = argv[2:] if flags[argv[0]] else argv[1:]
        else:
            raise _config.SlotError(f"unknown {what} option {argv[0]}")
    return found, label, command


def yield_command(argv: list[str]) -> int:
    """`slotfather yield`, from inside a yieldable lease between two steps: yields if asked and returns once the
    lease is held again; returns at once when nothing asked. Exit 0 either way."""
    from . import leases as _leases
    from . import platform as _platform
    from . import state as _state
    if argv:
        raise _config.SlotError("yield takes no arguments")
    _state.require_own(_config.state_dir(), "yield")
    found = _leases.inherited_lease(_platform.processes())
    if found is None or found[0].parent.parent != _config.state_dir():
        raise _config.SlotError("yield runs inside a lease of this queue (SLOTFATHER_LEASE, or a holder among its ancestors)")
    if not _leases.yield_lease(found[0]):
        print("slot: no yield requested", file=sys.stderr)
    return 0


def status_command(argv: list[str]) -> int:
    """`status --json [--pid PID | --full]`."""
    from . import query as _query
    pid = None
    if argv == ["--full"]:
        from . import board as _board
        print(json.dumps(_board.full_status(), sort_keys=True))
        return 0
    if argv[:1] == ["--pid"] and len(argv) == 2 and argv[1].isdigit():
        pid = int(argv[1])
    elif argv:
        raise _config.SlotError("status takes --json and then optionally --pid PID or --full")
    print(json.dumps(_query.status_json(pid=pid), sort_keys=True))
    return 0


def start_option(argv: list[str]) -> tuple[list[str], str | None]:
    """`--start START` (a process start time as `status --json` gives it) taken out of argv."""
    if "--start" not in argv:
        return argv, None
    at = argv.index("--start")
    if at + 1 >= len(argv) or not argv[at + 1].strip():
        raise _config.SlotError("--start takes the process's start time, as status --json gives it")
    return argv[:at] + argv[at + 2:], argv[at + 1]


def reprioritize_command(argv: list[str]) -> int:
    """`slotfather reprioritize PID|LABEL interactive|normal|batch [--start START]`: the request keeps its
    arrival."""
    from . import leases as _leases
    argv, start = start_option(argv)
    if len(argv) != 2 or argv[1] not in _config.PRIORITIES:
        raise _config.SlotError(f"reprioritize takes a pid or label and then one of {', '.join(_config.PRIORITIES)}")
    for entry in _leases.reprioritize(argv[0], argv[1], start=start):
        first = entry.get("reprioritized")
        print(f"{entry.get('label')} (pid {entry.get('pid')}, {entry.get('state')}): {argv[1]}" +
              (f", asked {first} first" if first and first != argv[1] else "") +
              f"; queued since {entry.get('since')}")
    return 0


def ask_yield_command(argv: list[str]) -> int:
    """`slotfather ask-yield PID --start START`: ask a yieldable lease to yield for a waiter of a higher class."""
    from . import leases as _leases
    argv, start = start_option(argv)
    if len(argv) != 1 or not argv[0].isdigit() or start is None:
        raise _config.SlotError("ask-yield takes the holder's pid and --start START")
    entry, request, before = _leases.ask_yield(int(argv[0]), start)
    print(f"{entry.get('label')} (pid {entry.get('pid')}): " +
          (f"already asked to yield for {request.get('label')}" if before else
           f"asked to yield {entry.get('count')} slot(s) for {request.get('label')}; it yields at its next check"))
    return 0


def advise_command(argv: list[str]) -> int:
    from . import query as _query
    found, label, command = request_options(argv, "advise", {"--max": True, "--exclusive": False})
    maximum = parse_count(found["--max"]) if "--max" in found else None
    print(json.dumps(_query.advise(maximum, "--exclusive" in found, label=label, command=command), sort_keys=True))
    return 0


def available_command(argv: list[str]) -> int:
    from . import query as _query
    _, label, command = request_options(argv, "available", {})
    print(_query.available(label=label, command=command))
    return 0


def order_line(found: dict) -> str:
    def ages(key: str) -> str:
        return f"after {found[key]:g} s" if found[key] else "never"
    return (f"{found['order']}: safety {found['safety']:g}, limit {found['limit']:g} s, fallback "
            f"{found['fallback']:g} s, overrun {found['overrun']:g}; interactive {found['interactive']:g} s ahead, "
            f"batch {found['batch']:g} s behind, yield after {found['yield_after']:g} s; idle below "
            f"{found['idle_cpu']:g} cores for {found['idle_after']:g} s; batch ages {ages('aging_batch')}, normal "
            f"{ages('aging_normal')}; warn after {found['warn_wait']:g} s waiting; memory pressure warning admits "
            f"{'one 1-slot lease at a time' if found['warning_admits'] >= 1 else 'nothing'} "
            f"({'saved' if found['saved'] else 'default'})")


def order_command(argv: list[str]) -> int:
    """`slotfather order [backfill|window] [--safety X] [--limit S] [--fallback S] [--overrun X] [--idle-cpu C]
    [--idle-after S] [--aging-batch S] [--aging-normal S] [--warn-wait S] [--warning-admits 1|0] | reset`; S is
    seconds or 30m, 1.5h."""
    from . import schedule as _schedule
    from . import state as _state
    directory = _config.state_dir()
    changes: dict[str, Any] = {}
    reset = False
    while argv:
        word = argv.pop(0)
        if word in _config.QUEUE_ORDERS:
            changes["order"] = word
        elif word == "reset" and not changes:
            reset = True
        elif word.startswith("--") and word[2:].replace("-", "_") in _config.ORDER_KNOBS and argv:
            key = word[2:].replace("-", "_")
            try:
                changes[key] = _config.parse_duration(argv.pop(0)) if key in _config.DURATION_KNOBS else \
                    float(argv.pop(0))
            except ValueError:
                raise _config.SlotError(f"{word} takes " + ("seconds, or a duration such as 30m or 1.5h"
                                                            if key in _config.DURATION_KNOBS else "a number")) from None
        else:
            raise _config.SlotError(f"order takes backfill or window, --safety, --limit, --fallback, --overrun, "
                            f"--interactive, --batch, --yield-after, --idle-cpu, --idle-after, --aging-batch, "
                            f"--aging-normal, --warn-wait, --warning-admits, or reset; not {word!r}")
    if reset and changes:
        raise _config.SlotError("order reset takes nothing else")
    if changes or reset:
        _state.configure({"order.json": None if reset else changes})
    saved = _schedule.order_settings(directory, environment=False)
    moved = [k for k in _config.ORDER_DEFAULTS if k not in _config.ORDER_BASE + ("limit",)
             and saved[k] != _config.ORDER_DEFAULTS[k]]  # every slotfather knows `limit`
    if (changes or reset) and moved:
        print(f"slotfather: note: order.json now holds {', '.join(moved)}, which an older slotfather may not know "
              f"(3e3a621 and older know no classes, those before ageing no aging or warn settings, those before "
              f"the memory warning allowance no warning_admits): their looks fall "
              f"back to the window order until they are updated", file=sys.stderr)
    print(order_line(_schedule.order_settings(directory)))
    return 0


def slots_value(argv: list[str]) -> dict[str, Any] | None:
    """What `slots` saves: {"slots": N}, {"mode", "gib_per_slot", "slots"} or None for reset. The memory modes also
    save the count they give now as "slots", which slotfather 0af7546 and older read alone."""
    from . import platform as _platform
    if len(argv) == 1 and argv[0] == "reset":
        return None
    if len(argv) == 1 and argv[0].isascii() and argv[0].isdigit() and int(argv[0]) >= 1:
        return {"slots": int(argv[0])}
    if len(argv) == 2 and argv[0] in _config.CAPACITY_MODES:
        try:
            per_slot = float(argv[1])
        except ValueError:
            per_slot = 0.0
        if math.isfinite(per_slot) and per_slot > 0:
            per_slot = int(per_slot) if per_slot.is_integer() else per_slot
            # The cgroup is the saver's (SLOTFATHER_CGROUP, or its outermost limited one), not the one saved before.
            group = (_config.setting("CGROUP") or _config.DEFAULT_CGROUP or _platform.limited_cgroup()) if _platform.linux() else None
            if _platform.linux() and _platform.cgroup_limit(group) is None:
                print("slotfather: note: no cgroup with a memory limit here or in SLOTFATHER_CGROUP: the count "
                      "follows MemTotal, which a VM's balloon moves and which differs from what clients inside a "
                      "limited cgroup see; name the cgroup the leases share (SLOTFATHER_CGROUP=/a.slice/b.slice, "
                      "or install.py --cgroup)", file=sys.stderr)
            by_memory = _config.memory_slots(_platform.memory_gib(group or ""), per_slot)
            if by_memory is None:
                raise _config.SlotError("the machine's memory is unknown here; save a count instead (slotfather slots N)")
            cpu = max(1, _config.cores() // _config.CORES_PER_SLOT)
            found = {"slots": by_memory if argv[0] == "memory" else min(cpu, by_memory), "mode": argv[0],
                     "gib_per_slot": per_slot}
            if _platform.cgroup_limit(group) is not None:
                found["cgroup"] = group
            return found
    raise _config.SlotError("slots takes a positive integer, memory GIB or lower GIB (GiB per slot), or reset")


def slots_command(argv: list[str]) -> int:
    from . import state as _state
    directory = _config.state_dir()
    from . import query as _query
    if argv:
        _state.require_own(directory, "a capacity change")
        _state.configure({"capacity.json": slots_value(argv)})  # applies at the next grant, busy or not (#7)
    live, legacy, _, found = _query.locked_snapshot(directory)
    held = _query.held_slots(live, legacy, int(found["slots"]))
    drain = _query.draining(held, found["slots"])
    if drain:
        print(f"slotfather: note: {held} slots held: draining {drain}; running work keeps its slots and nothing "
              f"more starts until at most {found['slots']} are held", file=sys.stderr)
    print(_config.capacity_text(found))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    command, rest = (argv[0], argv[1:]) if argv else ("", [])
    # A person typing just `slotfather` wants to see the queue; a script or agent without a terminal gets the usage.
    if not argv and sys.stdin.isatty() and sys.stdout.isatty():
        command = "dashboard"
    try:
        if command == "lane-admission":
            from .lane_admission import command as lane_admission_command
            return lane_admission_command(rest)
        if command == "version":
            from .version import info, text
            if rest == ["--json"]:
                print(json.dumps(info(), sort_keys=True))
            elif not rest:
                print(text())
            else:
                raise _config.SlotError("version takes only --json")
            return 0
        if command == "capabilities":
            if rest != ["--json"]:
                raise _config.SlotError("capabilities takes --json")
            from .capabilities import capabilities
            print(json.dumps(capabilities(), sort_keys=True))
            return 0
        if command == "settings":
            from .settings import command as settings_command
            return settings_command(rest)
        if command == "procs":
            from .procs import command as procs_command
            return procs_command(rest)
        if command == "run":
            return run_command(rest)
        if command == "slots":
            return slots_command(rest)
        if command == "status" and not rest:
            from . import query as _query
            print("\n".join(_query.status_lines()))
            return 0
        if command == "status" and rest[:1] == ["--json"]:
            return status_command(rest[1:])
        if command == "yield":
            return yield_command(rest)
        if command == "reprioritize":
            return reprioritize_command(rest)
        if command == "ask-yield":
            return ask_yield_command(rest)
        if command == "history" and (not rest or (len(rest) == 1 and rest[0].isdigit())):
            from . import state as _state
            print("\n".join(_state.history_lines(int(rest[0]) if rest else 20)))
            return 0
        if command == "available":
            return available_command(rest)
        if command == "order":
            return order_command(rest)
        if command == "advise":
            return advise_command(rest)
        if command == "dashboard":
            return dashboard(rest)
        if command == "stats":
            return companion("stats", rest, "slotfather-stats")
        if command == "sweep":
            return companion("sweep", rest, "slotfather-sweep")
        if command in ("-h", "--help", "help"):
            print(__doc__)
            return 0
    except _config.SlotError as error:
        print(f"slotfather: {error}\n{USAGE}", file=sys.stderr)
        return 2
    print(USAGE, file=sys.stderr)
    return 2
