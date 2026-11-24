"""slotfather: one queue of requests for the machine's capacity, shared by every project and agent (#599).

Slots: one per three cores, at least one, and no more than one per 4 GiB of memory, so
`total = min(max(1, cores // 3), max(1, memory_GiB // 4))`. `slotfather slots N` saves shared capacity,
`slots memory G` one slot per G GiB of memory, `slots lower G` the lower of that and the CPU budget (#653),
and `slotfather slots reset` restores the automatic default.

State: ~/.local/state/slotfather (SLOTFATHER_DIR replaces it; another user's queue is read only).
Standard library only, Python 3.11 or later: install.py puts thin entry points in ~/.local/bin and
preserves their complete package in an immutable release.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
import sys
from typing import Any, Callable, cast

from . import config as _config


GODFATHER_MISSING = ("the dashboard is part of godfather now; install it from "
                     "https://github.com/maxvyshniakov/fathers/tree/main/godfather (python3 install.py godfather)")


@dataclass(frozen=True)
class OptionSpec:
    names: tuple[str, ...]
    metavar: str | None = None
    multi: bool = False
    help: str = ""
    missing_value: str | None = None

    @property
    def key(self) -> str:
        return self.names[-1]


@dataclass(frozen=True)
class CommandSpec:
    name: str
    usage: str
    summary: str
    options: tuple[OptionSpec, ...] = ()
    stop_at_positional: bool = False
    passthrough: bool = False


class UsageError(_config.SlotError):
    """A command-line usage error; formatted with a one-line reason and a help pointer."""

    def __init__(self, message: str, command: str | None = None) -> None:
        super().__init__(message)
        self.command = command


def _order_options() -> tuple[OptionSpec, ...]:
    opts: list[OptionSpec] = [
        OptionSpec(("--json",), help="print queue order settings as JSON"),
    ]
    for knob in _config.ORDER_KNOBS:
        flag = "--" + knob.replace("_", "-")
        if knob in _config.DURATION_KNOBS:
            opts.append(OptionSpec((flag,), metavar="S", help="duration in seconds or e.g. 30m, 1.5h"))
        elif knob == "warning_admits":
            opts.append(OptionSpec((flag,), metavar="1|0", help="admit one 1-slot lease (1) or none (0) under memory warning"))
        elif knob == "load_gate":
            opts.append(OptionSpec((flag,), metavar="X", help="hold grants beside running leases at X times the "
                                                              "cores of sustained unleased load (0: off)"))
        elif knob == "memory_budget_mb":
            opts.append(OptionSpec((flag,), metavar="MB", help="explicit expected-peak memory budget in MB (0: use memory_share only)"))
        else:
            opts.append(OptionSpec((flag,), metavar="N", help=f"set {knob.replace('_', ' ')}"))
    opts.append(OptionSpec(("--group-cap",), metavar="GROUP=N|off", multi=True,
                           help="cap concurrent leases in GROUP at N (>= 1), or off/0 to remove (repeatable)"))
    return tuple(opts)


COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec(
        "run",
        "run [-n N|all | --cores C|auto] [--label L] [--meta K=V]... [--priority P] [--ticket ID] [--yieldable] [--wait S] [--session NAME] [--] COMMAND...",
        "run COMMAND holding N slots (default 1)",
        options=(
            OptionSpec(("-n", "--slots"), metavar="N|all", help="number of slots to hold (or all)"),
            OptionSpec(("--cores",), metavar="C|auto",
                       help="hold the slots that C cores need (auto: the label family's measured use)"),
            OptionSpec(("-l", "--label"), metavar="L", help="request label"),
            OptionSpec(("--meta",), metavar="K=V", multi=True, help="request metadata key=value (repeatable)"),
            OptionSpec(("--priority",), metavar="P", help="request class: interactive, normal, or batch"),
            OptionSpec(("--ticket",), metavar="ID", help="ticket identifier"),
            OptionSpec(("--yieldable",), help="allow higher-priority waiters to request a yield"),
            OptionSpec(("--wait",), metavar="S", help="give up after S seconds (exit 75)"),
            OptionSpec(("--session",), metavar="NAME", help="run inside an existing measurement session"),
        ),
        stop_at_positional=True,
    ),
    CommandSpec(
        "yield",
        "yield",
        "inside a yieldable lease: yield if asked",
    ),
    CommandSpec(
        "lock",
        "lock PATH -- COMMAND...",
        "run COMMAND holding a flock on PATH; enclosing lease frees slots while waiting",
        stop_at_positional=True,
    ),
    CommandSpec(
        "session",
        "session start --label NAME [-n N|all] [--idle S] [--expect S] [--priority P] [--ticket ID] [--meta K=V]... [--wait S] | session stop NAME",
        "hold the machine for a series of `run --session NAME -- COMMAND`",
        options=(
            OptionSpec(("-l", "--label"), metavar="NAME", help="session name"),
            OptionSpec(("-n", "--slots"), metavar="N|all", help="number of slots to hold (default all)"),
            OptionSpec(("--idle",), metavar="S", help="idle timeout before ending the session"),
            OptionSpec(("--expect",), metavar="S", help="expected total session duration"),
            OptionSpec(("--priority",), metavar="P", help="request class: interactive, normal, or batch"),
            OptionSpec(("--ticket",), metavar="ID", help="ticket identifier"),
            OptionSpec(("--meta",), metavar="K=V", multi=True, help="metadata key=value (repeatable)"),
            OptionSpec(("--wait",), metavar="S", help="give up after S seconds"),
        ),
    ),
    CommandSpec(
        "status",
        "status [--json [--pid PID | --full]]",
        "running and waiting requests, in queue order",
        options=(
            OptionSpec(("--json",), help="print queue status as JSON"),
            OptionSpec(("--pid",), metavar="PID", help="filter JSON status to one holder PID"),
            OptionSpec(("--full",), help="include host, time and dashboard model in JSON status"),
        ),
    ),
    CommandSpec(
        "capabilities",
        "capabilities [--json]",
        "what a dashboard (Godfather) can read and run here",
        options=(
            OptionSpec(("--json",), help="print capabilities as JSON"),
        ),
    ),
    CommandSpec(
        "settings",
        "settings [check KEY VALUE | set KEY VALUE | reset KEY|--all] [--revision R] [--json]",
        "the shared settings as data, and changes to them",
        options=(
            OptionSpec(("--json",), help="print settings or result as JSON"),
            OptionSpec(("--revision",), metavar="R", help="expected settings revision for set/reset"),
            OptionSpec(("--all",), help="reset every setting (with `settings reset`)"),
        ),
    ),
    CommandSpec(
        "reprioritize",
        "reprioritize PID|LABEL interactive|normal|batch [--start START]",
        "move a request to another class, keeping its place",
        options=(
            OptionSpec(("--start",), metavar="START", help="process start time as reported by status --json",
                       missing_value="--start takes the process's start time, as status --json gives it"),
        ),
    ),
    CommandSpec(
        "ask-yield",
        "ask-yield PID --start START",
        "ask a yieldable lease to yield for a higher class",
        options=(
            OptionSpec(("--start",), metavar="START", help="process start time as reported by status --json",
                       missing_value="--start takes the process's start time, as status --json gives it"),
        ),
    ),
    CommandSpec(
        "procs",
        "procs [--json] [--cwd PATH] [--pid PID]",
        "read-only process, lease and session ancestry",
        options=(
            OptionSpec(("--json",), help="print process table as JSON"),
            OptionSpec(("--cwd",), metavar="PATH", help="filter to processes under PATH"),
            OptionSpec(("--pid",), metavar="PID", help="inspect a single process ID"),
        ),
    ),
    CommandSpec(
        "lane-admission",
        "lane-admission --json",
        "stateless lifetime lane policy, stdin JSON",
        options=(
            OptionSpec(("--json",), help="read one JSON request on stdin and print JSON reply"),
        ),
    ),
    CommandSpec(
        "version",
        "version [--json]",
        "identify this runtime source",
        options=(
            OptionSpec(("--json",), help="print version metadata as JSON"),
        ),
    ),
    CommandSpec(
        "changelog",
        "changelog [--json] [--since VERSION]",
        "the installed release's changelog as data",
        options=(
            OptionSpec(("--json",), help="print changelog as JSON"),
            OptionSpec(("--since",), metavar="VERSION", help="only entries newer than VERSION"),
        ),
    ),
    CommandSpec(
        "history",
        "history [N] | history export | history import HOST [FILE]",
        "recent requests that left the queue; export or import another host's history",
    ),
    CommandSpec(
        "stats",
        "stats [--since WHEN] [--label GLOB] [--by B] [--host HOST] [--top N] [--trend [--project P] [--phase X]] [--json]",
        "request resource use, machine occupancy and run-type trends",
        options=(
            OptionSpec(("--since",), metavar="WHEN", help="start age (90m, 36h, 7d) or ISO date/time"),
            OptionSpec(("--label",), metavar="GLOB", help="filter labels by glob pattern"),
            OptionSpec(("--by",), metavar="B", help="group by both, label, kind, priority, ticket, family, derived, or meta.KEY"),
            OptionSpec(("--host",), metavar="HOST", help="read imported history for HOST"),
            OptionSpec(("--top",), metavar="N", help="maximum number of rows to display"),
            OptionSpec(("--trend",), help="show weekly run-type p50/p90 trends"),
            OptionSpec(("--project",), metavar="P", help="filter --trend to project P"),
            OptionSpec(("--phase",), metavar="X", help="filter --trend to phase X"),
            OptionSpec(("--json",), help="print statistics as JSON"),
        ),
    ),
    CommandSpec(
        "slots",
        "slots [N | memory GIB | lower GIB | reset] [--json]",
        "show, save or reset shared capacity",
        options=(
            OptionSpec(("--json",), help="print capacity state as JSON"),
        ),
    ),
    CommandSpec(
        "available",
        "available [--label L] [-- COMMAND...]",
        "queue space before memory checks",
        options=(
            OptionSpec(("-l", "--label"), metavar="L", help="request label for estimate lookup"),
        ),
    ),
    CommandSpec(
        "advise",
        "advise [--max N] [--exclusive] [--label L] [--cores C|auto] [--json] [-- COMMAND...]",
        "a recommended worker budget, as JSON",
        options=(
            OptionSpec(("--max",), metavar="N", help="upper bound on recommended workers"),
            OptionSpec(("--cores",), metavar="C|auto",
                       help="cores the request uses, for its weight (default auto: its label family's measured use)"),
            OptionSpec(("--exclusive",), help="ignore held slots (for an all-slot request)"),
            OptionSpec(("-l", "--label"), metavar="L", help="request label for estimate lookup"),
            OptionSpec(("--json",), help="print advice as JSON"),
        ),
    ),
    CommandSpec(
        "order",
        "order [backfill|window] [--safety X] [--limit S] ... [--json] | order reset [--json]",
        "show, save or reset the queue's scheduling order",
        options=_order_options(),
    ),
    CommandSpec(
        "sweep",
        "sweep [--live [--dev-servers]] [--json] [--quiet] [--no-log] [--pid PID]... | sweep config [KEY VALUE]... | sweep --plist [--live [--dev-servers]] [--interval SECONDS]",
        "find (and stop) processes left behind",
        options=(
            OptionSpec(("--live",), help="signal matching processes instead of dry-run"),
            OptionSpec(("--dev-servers",), help="also include dev servers (with --live)"),
            OptionSpec(("--json",), help="print sweep report as JSON"),
            OptionSpec(("--quiet",), help="suppress output when nothing is found"),
            OptionSpec(("--no-log",), help="do not record dry-run to sweep.jsonl"),
            OptionSpec(("--plist",), help="print a launchd plist for periodic sweeps"),
            OptionSpec(("--interval",), metavar="SECONDS", help="launchd interval in seconds (>= 60)"),
            OptionSpec(("--pid",), metavar="PID", multi=True, help="limit sweep to PID (repeatable)"),
        ),
    ),
    CommandSpec(
        "dashboard",
        "dashboard [ARGS...]",
        "the live view: runs `godfather ARGS...`",
        passthrough=True,
    ),
)

COMMAND_MAP: dict[str, CommandSpec] = {spec.name: spec for spec in COMMANDS}


def format_help() -> str:
    """Generate top-level help text from the command table."""
    lines = [
        "usage: slotfather [--help | --version] <command> [options] [args...]",
        "       slotfather                     (in a terminal: the live dashboard, godfather)",
        "",
        "commands:",
    ]
    width = max(len(spec.name) for spec in COMMANDS) + 2
    for spec in COMMANDS:
        lines.append(f"  {spec.name:<{width}}{spec.summary}")
    lines.extend([
        "",
        "options:",
        "  -h, --help      show this help and exit",
        "  --version       show version information and exit",
        "",
        "Run 'slotfather <command> --help' for details on a command.",
    ])
    return "\n".join(lines)


def format_command_help(spec: CommandSpec) -> str:
    """Generate per-command help text from a CommandSpec."""
    lines = [f"usage: slotfather {spec.usage}", "", spec.summary]
    if spec.options:
        lines.append("")
        lines.append("options:")
        entries: list[tuple[str, str]] = []
        for opt in spec.options:
            label = ", ".join(opt.names)
            if opt.metavar:
                label += f" {opt.metavar}"
            entries.append((label, opt.help))
        entries.append(("-h, --help", "show this help and exit"))
        width = max(len(label) for label, _ in entries) + 2
        for label, desc in entries:
            lines.append(f"  {label:<{width}}{desc}")
    else:
        lines.append("")
        lines.append("options:")
        lines.append("  -h, --help  show this help and exit")
    return "\n".join(lines)


USAGE = format_help()


def parse_options(argv: list[str], spec: CommandSpec) -> tuple[dict[str, Any], list[str], list[str], bool]:
    """Table-driven option parser for one CommandSpec.

    Returns (values, positionals, after_dash, help_requested).
    """
    by_name: dict[str, OptionSpec] = {}
    for opt in spec.options:
        for name in opt.names:
            by_name[name] = opt
    values: dict[str, Any] = {}
    positionals: list[str] = []
    after_dash: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            after_dash.extend(argv[i + 1:])
            break
        if arg in ("-h", "--help"):
            return {}, [], [], True
        if arg.startswith("--") and "=" in arg:
            opt_name, opt_val = arg.split("=", 1)
            opt = by_name.get(opt_name)
            if opt is None:
                raise UsageError(f"unknown option {opt_name} for {spec.name}", spec.name)
            if opt.metavar is None:
                raise UsageError(f"option {opt_name} takes no value", spec.name)
            if opt.multi:
                values.setdefault(opt.key, []).append(opt_val)
            else:
                values[opt.key] = opt_val
            i += 1
        elif arg.startswith("-") and arg != "-":
            opt = by_name.get(arg)
            if opt is None:
                if len(arg) > 1 and arg[1].isdigit():
                    if spec.stop_at_positional:
                        positionals.extend(argv[i:])
                        break
                    positionals.append(arg)
                    i += 1
                    continue
                raise UsageError(f"unknown option {arg} for {spec.name}", spec.name)
            if opt.metavar is None:
                values[opt.key] = True
                i += 1
            else:
                if i + 1 >= len(argv):
                    raise UsageError(opt.missing_value or f"option {arg} requires a value", spec.name)
                if opt.multi:
                    values.setdefault(opt.key, []).append(argv[i + 1])
                else:
                    values[opt.key] = argv[i + 1]
                i += 2
        else:
            if spec.stop_at_positional:
                positionals.extend(argv[i:])
                break
            positionals.append(arg)
            i += 1
    return values, positionals, after_dash, False


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


def parse_count(value: str) -> int:
    if value == "all":
        from . import settings as _settings
        return _settings.total()
    if not value.isdigit() or int(value) < 1:
        raise UsageError(f"-n takes a positive count or all, not {value!r}", "run")
    return int(value)


def parse_slots(value: str) -> tuple[int | str, int | None]:
    """Parse `run -n`: `all`, a positive count `N`, or a range `MIN-MAX` (1 <= MIN <= MAX, fathers#128)."""
    if value == "all":
        return "all", None
    if "-" in value:
        left, sep, right = value.partition("-")
        if sep and left.isascii() and right.isascii() and left.isdigit() and right.isdigit():
            low, high = int(left), int(right)
            if low >= 1 and high >= low:
                return low, (high if high > low else None)
        raise UsageError(f"-n takes a positive count, MIN-MAX range, or all, not {value!r}", "run")
    if not value.isascii() or not value.isdigit() or int(value) < 1:
        raise UsageError(f"-n takes a positive count, MIN-MAX range, or all, not {value!r}", "run")
    return int(value), None


def parse_cores(value: str, command: str) -> float | str:
    """Parse `--cores` (fathers#148): `auto` or a positive number of cores."""
    if value == "auto":
        return value
    try:
        found = float(value)
    except ValueError:
        found = 0.0
    if not math.isfinite(found) or found <= 0:
        raise UsageError(f"--cores takes a positive number of cores or auto, not {value!r}", command)
    return found


def run_command(values: dict[str, Any], positionals: list[str], after_dash: list[str]) -> int:
    from . import runner as _runner
    cmd = positionals or after_dash
    if not cmd:
        raise UsageError("no command to run", "run")
    counted = "--slots" in values
    count: int | str = 1
    max_count: int | None = None
    if counted:
        count, max_count = parse_slots(str(values["--slots"]))
    cores = parse_cores(str(values["--cores"]), "run") if "--cores" in values else None
    if counted and cores is not None:
        raise UsageError("run takes -n or --cores, not both", "run")
    labelled = "--label" in values
    label = str(values["--label"]) if labelled else os.path.basename(os.getcwd())
    pairs: list[str] = list(values.get("--meta") or [])
    priority: str | None = None
    if "--priority" in values:
        raw_priority = str(values["--priority"])
        if raw_priority not in _config.PRIORITIES:
            raise UsageError(f"--priority takes {', '.join(_config.PRIORITIES)}, not {raw_priority!r}", "run")
        priority = raw_priority
    ticket: str | None = str(values["--ticket"]) if "--ticket" in values else None
    yieldable = bool(values.get("--yieldable", False))
    wait: float | None = None
    if "--wait" in values:
        try:
            wait = float(values["--wait"])
        except ValueError:
            raise UsageError(f"--wait takes seconds, not {values['--wait']!r}", "run") from None
    session: str | None = str(values["--session"]) if "--session" in values else None
    from .entry import parse_meta
    if session is not None:
        if counted or cores is not None or priority or ticket or yieldable or wait is not None:
            raise UsageError("run --session takes only --label and --meta: the session's lease is already held", "run")
        from .session import run_inside
        return run_inside(session, cmd, label if labelled else None, parse_meta(pairs))
    return _runner.run(count, label, cmd, wait, parse_meta(pairs), yieldable, priority=priority, ticket=ticket,
                       labelled=labelled, max_count=max_count, cores=cores)


def yield_command(values: dict[str, Any], positionals: list[str], after_dash: list[str]) -> int:
    """`slotfather yield`, from inside a yieldable lease between two steps: yields if asked and returns once the
    lease is held again; returns at once when nothing asked. Exit 0 either way."""
    from . import client as _client
    from . import process as _process
    from . import queue as _queue
    from . import state as _state
    if positionals or after_dash:
        raise UsageError("yield takes no arguments", "yield")
    _state.require_own(_config.state_dir(), "yield")
    found = _queue.inherited_lease(_process.processes())
    if found is None or found[0].parent.parent != _config.state_dir():
        raise _config.SlotError("yield runs inside a lease of this queue (SLOTFATHER_LEASE, or a holder among its ancestors)")
    if not _client.yield_now(found[0]):
        print("slot: no yield requested", file=sys.stderr)
    return 0


def lock_command(values: dict[str, Any], positionals: list[str], after_dash: list[str]) -> int:
    from .lockwait import command as _lock_command
    reconstructed = list(positionals)
    if after_dash:
        reconstructed.append("--")
        reconstructed.extend(after_dash)
    return _lock_command(reconstructed)


def session_command(argv: list[str]) -> int:
    if argv[:1] == ["hold"]:
        from .session import command as _session_command
        return _session_command(argv)
    spec = COMMAND_MAP["session"]
    values, positionals, after_dash, help_requested = parse_options(argv, spec)
    if help_requested:
        print(format_command_help(spec))
        return 0
    if after_dash:
        raise UsageError("session does not take -- arguments", "session")
    if not positionals or positionals[0] not in ("start", "stop"):
        bad = positionals[0] if positionals else ""
        raise UsageError(f"session takes start or stop{f', not {bad!r}' if bad else ''}", "session")
    forwarded: list[str] = [positionals[0]]
    for opt in spec.options:
        if opt.key not in values:
            continue
        if opt.multi:
            for item in values[opt.key]:
                forwarded.extend([opt.key, str(item)])
        elif opt.metavar is None:
            forwarded.append(opt.key)
        else:
            forwarded.extend([opt.key, str(values[opt.key])])
    forwarded.extend(positionals[1:])
    from .session import command as _session_command
    return _session_command(forwarded)


def status_command(values: dict[str, Any], positionals: list[str], after_dash: list[str]) -> int:
    """`status [--json [--pid PID | --full]]`."""
    if positionals or after_dash:
        bad = (positionals or after_dash)[0]
        raise UsageError(f"unexpected argument {bad!r} for status", "status")
    as_json = bool(values.get("--json", False))
    full = bool(values.get("--full", False))
    raw_pid = values.get("--pid")
    if not as_json:
        if full or raw_pid is not None:
            raise UsageError("status takes --json with --pid or --full", "status")
        from . import query as _query
        print("\n".join(_query.status_lines()))
        return 0
    if full and raw_pid is not None:
        raise UsageError("status takes --pid or --full, not both", "status")
    if full:
        from . import board as _board
        print(json.dumps(_board.full_status(), sort_keys=True))
        return 0
    pid: int | None = None
    if raw_pid is not None:
        text_pid = str(raw_pid)
        if not text_pid.isdigit():
            raise UsageError(f"--pid takes a positive integer, not {text_pid!r}", "status")
        pid = int(text_pid)
    from . import query as _query
    print(json.dumps(_query.status_json(pid=pid), sort_keys=True))
    return 0


def capabilities_command(values: dict[str, Any], positionals: list[str], after_dash: list[str]) -> int:
    if positionals or after_dash:
        bad = (positionals or after_dash)[0]
        raise UsageError(f"unexpected argument {bad!r} for capabilities", "capabilities")
    from .capabilities import capabilities
    data = capabilities()
    if values.get("--json"):
        print(json.dumps(data, sort_keys=True))
        return 0
    dash = data["dashboard"]
    actions = ", ".join(dash.get("actions", []))
    print(f"{data['tool']} {data['version']} (capabilities v{data['v']}; dashboard v{dash['v']}, actions: {actions})")
    return 0


def settings_command(values: dict[str, Any], positionals: list[str], after_dash: list[str]) -> int:
    if after_dash:
        raise UsageError("settings does not take -- arguments", "settings")
    as_json = bool(values.get("--json", False))
    wanted: str | None = str(values["--revision"]) if "--revision" in values else None
    reset_all = bool(values.get("--all", False))
    if as_json:
        forwarded: list[str] = list(positionals)
        if reset_all:
            forwarded.append("--all")
        if wanted is not None:
            forwarded.extend(["--revision", wanted])
        forwarded.append("--json")
        from .settings_cli import command as _settings_command
        return _settings_command(forwarded)
    from . import settings_cli as _settings_cli
    if not positionals and wanted is None and not reset_all:
        document = _settings_cli.Reading().document()
        for section in document["sections"]:
            print(f"[{section['title']}]")
            for row in section["settings"]:
                print(f"  {row['key']} = {row['value']}")
        return 0
    try:
        if len(positionals) == 3 and positionals[0] == "check" and wanted is None and not reset_all:
            _, reply = _settings_cli.change(_settings_cli.Reading(), positionals[1], positionals[2])
            print(reply.get("message") or json.dumps(reply, sort_keys=True))
            return 0
        if len(positionals) == 3 and positionals[0] == "set" and not reset_all:
            changes, _ = _settings_cli.change(_settings_cli.Reading(), positionals[1], positionals[2])
            code, reply = _settings_cli.save(changes, wanted)
            print(reply.get("message") or reply.get("error") or json.dumps(reply, sort_keys=True))
            return code
        if positionals[:1] == ["reset"]:
            target = "--all" if reset_all and len(positionals) == 1 else (
                positionals[1] if len(positionals) == 2 and not reset_all else None)
            if target is not None:
                code, reply = _settings_cli.save(_settings_cli.reset_change(target), wanted)
                print(reply.get("message") or reply.get("error") or json.dumps(reply, sort_keys=True))
                return code
    except _settings_cli.Invalid as error:
        raise UsageError(str(error), "settings") from None
    raise UsageError("settings takes [check KEY VALUE | set KEY VALUE | reset KEY|--all] [--revision R] [--json]",
                     "settings")


def reprioritize_command(values: dict[str, Any], positionals: list[str], after_dash: list[str]) -> int:
    """`slotfather reprioritize PID|LABEL interactive|normal|batch [--start START]`."""
    from . import leases as _leases
    if after_dash:
        raise UsageError("reprioritize does not take -- arguments", "reprioritize")
    start: str | None = None
    if "--start" in values:
        start = str(values["--start"])
        if not start.strip():
            raise UsageError("--start takes the process's start time, as status --json gives it", "reprioritize")
    if len(positionals) != 2 or positionals[1] not in _config.PRIORITIES:
        raise UsageError(f"reprioritize takes a pid or label and then one of {', '.join(_config.PRIORITIES)}",
                         "reprioritize")
    for entry in _leases.reprioritize(positionals[0], positionals[1], start=start):
        first = entry.get("reprioritized")
        print(f"{entry.get('label')} (pid {entry.get('pid')}, {entry.get('state')}): {positionals[1]}" +
              (f", asked {first} first" if first and first != positionals[1] else "") +
              f"; queued since {entry.get('since')}")
    return 0


def ask_yield_command(values: dict[str, Any], positionals: list[str], after_dash: list[str]) -> int:
    """`slotfather ask-yield PID --start START`."""
    from . import leases as _leases
    if after_dash:
        raise UsageError("ask-yield does not take -- arguments", "ask-yield")
    start = str(values["--start"]) if "--start" in values else None
    if start is not None and not start.strip():
        raise UsageError("--start takes the process's start time, as status --json gives it", "ask-yield")
    if len(positionals) != 1 or not positionals[0].isdigit() or start is None:
        raise UsageError("ask-yield takes the holder's pid and --start START", "ask-yield")
    entry, request, before = _leases.ask_yield(int(positionals[0]), start)
    print(f"{entry.get('label')} (pid {entry.get('pid')}): " +
          (f"already asked to yield for {request.get('label')}" if before else
           f"asked to yield {entry.get('count')} slot(s) for {request.get('label')}; it yields at its next check"))
    return 0


def procs_command(values: dict[str, Any], positionals: list[str], after_dash: list[str]) -> int:
    if positionals or after_dash:
        bad = (positionals or after_dash)[0]
        raise UsageError(f"unexpected argument {bad!r} for procs", "procs")
    pid: int | None = None
    if "--pid" in values:
        raw_pid = str(values["--pid"])
        if not raw_pid.isascii() or not raw_pid.isdigit() or int(raw_pid) <= 0:
            raise UsageError(f"--pid takes a positive integer, not {raw_pid!r}", "procs")
        pid = int(raw_pid)
    cwd = str(values["--cwd"]) if "--cwd" in values else None
    from . import procs as _procs
    found = _procs.query(pid=pid, cwd=cwd)
    if values.get("--json"):
        print(json.dumps(found))
        return 0
    for proc in found["processes"]:
        print(f"pid={proc['pid']} ppid={proc['ppid']} uid={proc['uid']} cmd={proc['cmd']}")
    return 0


def lane_admission_command(values: dict[str, Any], positionals: list[str], after_dash: list[str]) -> int:
    if positionals or after_dash or not values.get("--json"):
        raise UsageError("lane-admission takes --json and one stdin request", "lane-admission")
    from .lane_admission import command as _lane_admission_command
    return _lane_admission_command(["--json"])


def version_command(values: dict[str, Any], positionals: list[str], after_dash: list[str]) -> int:
    if positionals or after_dash:
        bad = (positionals or after_dash)[0]
        raise UsageError(f"unexpected argument {bad!r} for version", "version")
    from .version import info, text
    if values.get("--json"):
        print(json.dumps(info(), sort_keys=True))
    else:
        print(text())
    return 0


def changelog_command(values: dict[str, Any], positionals: list[str], after_dash: list[str]) -> int:
    if positionals or after_dash:
        bad = (positionals or after_dash)[0]
        raise UsageError(f"unexpected argument {bad!r} for changelog", "changelog")
    from . import changelog as _changelog
    since = str(values["--since"]) if "--since" in values else None
    if since is not None:
        _changelog.version_key(since)
    data = _changelog.changelog(since)
    if values.get("--json"):
        print(json.dumps(data, sort_keys=True))
        return 0
    for entry in data["entries"]:
        header = "Unreleased" if entry.get("unreleased") else f"{entry.get('version')} ({entry.get('date')})"
        section = f" [{entry['section']}]" if entry.get("section") else ""
        print(f"{header}{section}: {entry['text']}")
    return 0


def history_command(values: dict[str, Any], positionals: list[str], after_dash: list[str]) -> int:
    if after_dash:
        raise UsageError("history does not take -- arguments", "history")
    if positionals and not positionals[0].isdigit():
        from .archive import command as _archive_command
        return _archive_command(positionals)
    if not positionals or (len(positionals) == 1 and positionals[0].isdigit()):
        from . import state as _state
        print("\n".join(_state.history_lines(int(positionals[0]) if positionals else 20)))
        return 0
    raise UsageError("history takes N, export, or import HOST [FILE]", "history")


def _resolve_request_label(values: dict[str, Any], after_dash: list[str]) -> tuple[str, str]:
    labelled = "--label" in values
    label = str(values["--label"]) if labelled else os.path.basename(os.getcwd())
    command = " ".join(after_dash)
    if command and not labelled:
        import contextlib
        import shlex
        with contextlib.suppress(Exception):
            from .labels import choose
            label = choose(None, os.getcwd(), shlex.split(command))["label"] or label
    return label, command


def advise_command(values: dict[str, Any], positionals: list[str], after_dash: list[str]) -> int:
    if positionals:
        raise UsageError(f"unexpected argument {positionals[0]!r} for advise (use -- before COMMAND)", "advise")
    from . import query as _query
    label, command = _resolve_request_label(values, after_dash)
    maximum = parse_count(str(values["--max"])) if "--max" in values else None
    cores = parse_cores(str(values["--cores"]), "advise") if "--cores" in values else "auto"
    found = _query.advise(maximum, bool(values.get("--exclusive", False)), label=label, command=command)
    from . import runtype as _runtype
    found["weight"] = _runtype.weight(_config.state_dir(), label, None if cores == "auto" else float(cores))
    reply: dict[str, Any] = {"v": 1, **found} if "v" not in found else found
    print(json.dumps(reply, sort_keys=True))
    return 0


def available_command(values: dict[str, Any], positionals: list[str], after_dash: list[str]) -> int:
    if positionals:
        raise UsageError(f"unexpected argument {positionals[0]!r} for available (use -- before COMMAND)", "available")
    from . import query as _query
    label, command = _resolve_request_label(values, after_dash)
    print(_query.available(label=label, command=command))
    return 0


def order_line(found: dict[str, Any]) -> str:
    def ages(key: str) -> str:
        return f"after {found[key]:g} s" if found[key] else "never"
    extra = ""
    if found.get("memory_budget_mb"):
        extra += f"; memory budget {found['memory_budget_mb']:g} MB"
    caps = found.get("group_caps")
    if isinstance(caps, dict) and caps:
        extra += "; group caps " + ", ".join(f"{k}={v}" for k, v in sorted(cast(dict[str, int], caps).items()))
    return (f"{found['order']}: safety {found['safety']:g}, limit {found['limit']:g} s, fallback "
            f"{found['fallback']:g} s, overrun {found['overrun']:g}; interactive {found['interactive']:g} s ahead, "
            f"batch {found['batch']:g} s behind, yield after {found['yield_after']:g} s; idle below "
            f"{found['idle_cpu']:g} cores for {found['idle_after']:g} s; batch ages {ages('aging_batch')}, normal "
            f"{ages('aging_normal')}; warn after {found['warn_wait']:g} s waiting; a blocked head gives way "
            f"{ages('reservation_timeout')} past its reserved start; memory pressure warning admits "
            f"{'one 1-slot lease at a time' if found['warning_admits'] >= 1 else 'nothing'}; CPU load holds grants "
            f"beside running leases {load_gate(found)}{extra} "
            f"({'saved' if found['saved'] else 'default'})")


def load_gate(found: dict[str, Any]) -> str:
    """order_line()'s words for the CPU load gate (fathers#184)."""
    from .schedule import LOAD_PEAK
    gate = float(found.get("load_gate", _config.ORDER_DEFAULTS["load_gate"]))
    if not gate:
        return "never"
    return f"at {gate:g}x the cores unleased or {gate * LOAD_PEAK:g}x in all"


def order_command(values: dict[str, Any], positionals: list[str], after_dash: list[str]) -> int:
    """`slotfather order [backfill|window] [--safety X] ... [--json] | reset [--json]`."""
    if after_dash:
        raise UsageError("order does not take -- arguments", "order")
    from . import durations as _durations
    from . import leases as _leases
    from . import settings as _settings
    directory = _config.state_dir()
    changes: dict[str, Any] = {}
    reset = False
    for word in positionals:
        if word in _config.QUEUE_ORDERS:
            changes["order"] = word
        elif word == "reset":
            reset = True
        else:
            raise UsageError(f"unknown order argument {word!r}; expected backfill, window, or reset", "order")
    for knob in _config.ORDER_KNOBS:
        flag = "--" + knob.replace("_", "-")
        if flag not in values:
            continue
        raw = str(values[flag])
        try:
            changes[knob] = _durations.clock(raw) if knob in _config.DURATION_KNOBS else float(raw)
        except ValueError:
            kind = "seconds, or a duration such as 30m or 1.5h" if knob in _config.DURATION_KNOBS else "a number"
            raise UsageError(f"{flag} takes {kind}, not {raw!r}", "order") from None
    merge_group_caps = False
    if "--group-cap" in values:
        cap_patch: dict[str, int | None] = {}
        for raw_item in values["--group-cap"]:
            text = str(raw_item)
            if "=" not in text:
                raise UsageError(f"--group-cap takes GROUP=N or GROUP=off, not {text!r}", "order")
            group, _, raw_val = text.partition("=")
            group = group.strip()
            raw_val = raw_val.strip().lower()
            if not _config.GROUP_NAME.fullmatch(group):
                raise UsageError(f"--group-cap group must be a lowercase identifier, not {group!r}", "order")
            if raw_val in ("0", "off", "none"):
                cap_patch[group] = None
            elif raw_val.isascii() and raw_val.isdigit() and int(raw_val) >= 1:
                cap_patch[group] = int(raw_val)
            else:
                raise UsageError(f"--group-cap {group} takes an integer >= 1 or off, not {raw_val!r}", "order")
        changes["group_caps"] = cap_patch
        merge_group_caps = True
    if reset and changes:
        raise UsageError("order reset takes nothing else", "order")
    if changes or reset:
        _settings.configure({"order.json": None if reset else changes}, granted=_leases.grant_now,
                            merge_group_caps=merge_group_caps)
    saved = _settings.order_settings(directory, environment=False)
    moved = _settings.order_extension_keys(saved)
    if (changes or reset) and moved:
        print(f"slotfather: note: {_settings.order_compatibility(moved, historical=True)}", file=sys.stderr)
    current = _settings.order_settings(directory)
    if values.get("--json"):
        print(json.dumps({"v": 1, **current}, sort_keys=True))
    else:
        print(order_line(current))
    return 0


def slots_value(argv: list[str]) -> dict[str, Any] | None:
    """What `slots` saves (settings.capacity_change()), its note on stderr."""
    from . import settings as _settings
    return _settings.capacity_change(argv, lambda note: print(note, file=sys.stderr))


def slots_command(argv: list[str]) -> int:
    spec = COMMAND_MAP["slots"]
    values, positionals, after_dash, help_requested = parse_options(argv, spec)
    if help_requested:
        print(format_command_help(spec))
        return 0
    if after_dash:
        raise UsageError("slots does not take -- arguments", "slots")
    from . import leases as _leases
    from . import query as _query
    from . import settings as _settings
    from . import state as _state
    directory = _config.state_dir()
    if positionals:
        _state.require_own(directory, "a capacity change")
        # applies at the next grant, busy or not (#7)
        _settings.configure({"capacity.json": slots_value(positionals)}, granted=_leases.grant_now)
    look = _query.look(directory)
    found, held, drain = look.basis, look.held, look.draining
    if drain:
        print(f"slotfather: note: {held} slots held: draining {drain}; running work keeps its slots and nothing "
              f"more starts until at most {found['slots']} are held", file=sys.stderr)
    if values.get("--json"):
        print(json.dumps({"v": 1, "slots": found["slots"], "held": held, "draining": drain, "basis": found},
                         sort_keys=True))
    else:
        print(_settings.capacity_text(found))
    return 0


def stats_command(values: dict[str, Any], positionals: list[str], after_dash: list[str]) -> int:
    if positionals or after_dash:
        bad = (positionals or after_dash)[0]
        raise UsageError(f"unexpected argument {bad!r} for stats", "stats")
    from importlib import import_module
    try:
        stats_mod = import_module("slotfather.stats")
    except ModuleNotFoundError as error:
        if error.name != "slotfather.stats":
            raise
        raise _config.SlotError("slotfather-stats is not installed") from None
    since = None
    if "--since" in values:
        try:
            since = stats_mod.parse_since(str(values["--since"]))
        except stats_mod.StatsError as error:
            raise UsageError(str(error), "stats") from None
    label = str(values["--label"]) if "--label" in values else None
    host = str(values["--host"]) if "--host" in values else None
    project = str(values["--project"]) if "--project" in values else None
    phase = str(values["--phase"]) if "--phase" in values else None
    trend = bool(values.get("--trend", False))
    as_json = bool(values.get("--json", False))
    by = "both"
    if "--by" in values:
        raw_by = str(values["--by"])
        valid_by = raw_by in ("both", "label", "kind", "priority", "ticket", "family", "derived") or (
            raw_by.startswith("meta.") and raw_by[5:] not in ("class", "priority", "ticket")
            and bool(_config.META_KEY.fullmatch(raw_by[5:])))
        if not valid_by:
            raise UsageError(f"--by takes both, label, kind, priority, ticket, family, derived, or meta.KEY, not {raw_by!r}",
                             "stats")
        by = raw_by
    top = 25
    if "--top" in values:
        raw_top = str(values["--top"])
        if not raw_top.isdigit():
            raise UsageError(f"--top takes a non-negative integer, not {raw_top!r}", "stats")
        top = int(raw_top)
    try:
        if trend:
            from . import runtype as _runtype
            found = _runtype.report(since=since, project=project, phase=phase)
            if as_json:
                reply = found if isinstance(found.get("v"), int) else {"v": 1, **found}
                print(json.dumps(reply, sort_keys=True))
            else:
                print(_runtype.report_text(found))
            return 0
        summary = stats_mod.summarize(since=since, label=label, by=by, host=host)
    except stats_mod.StatsError as error:
        raise UsageError(str(error), "stats") from None
    if as_json:
        reply = summary if isinstance(summary.get("v"), int) else {"v": 1, **summary}
        print(json.dumps(reply, sort_keys=True))
    else:
        print(stats_mod.report(summary, top))
    return 0


def sweep_command(argv: list[str]) -> int:
    spec = COMMAND_MAP["sweep"]
    if argv[:1] == ["config"]:
        if any(arg in ("-h", "--help") for arg in argv[1:]):
            print(format_command_help(spec))
            return 0
        return companion("sweep", argv, "slotfather-sweep")
    values, positionals, after_dash, help_requested = parse_options(argv, spec)
    if help_requested:
        print(format_command_help(spec))
        return 0
    if positionals or after_dash:
        bad = (positionals or after_dash)[0]
        raise UsageError(f"unexpected argument {bad!r} for sweep", "sweep")
    normalized: list[str] = []
    for flag in ("--live", "--dev-servers", "--json", "--quiet", "--no-log", "--plist"):
        if values.get(flag):
            normalized.append(flag)
    if "--interval" in values:
        raw_interval = str(values["--interval"])
        if not raw_interval.isdigit() or int(raw_interval) < 60:
            raise UsageError(f"--interval takes an integer >= 60, not {raw_interval!r}", "sweep")
        normalized.extend(["--interval", raw_interval])
    raw_pids: list[str] = list(values.get("--pid") or [])
    for raw_pid in raw_pids:
        text_pid = str(raw_pid)
        if not text_pid.isdigit():
            raise UsageError(f"--pid takes a positive integer, not {text_pid!r}", "sweep")
        normalized.extend(["--pid", text_pid])
    return companion("sweep", normalized, "slotfather-sweep")


HANDLERS: dict[str, Callable[[dict[str, Any], list[str], list[str]], int]] = {
    "run": run_command,
    "yield": yield_command,
    "lock": lock_command,
    "status": status_command,
    "capabilities": capabilities_command,
    "settings": settings_command,
    "reprioritize": reprioritize_command,
    "ask-yield": ask_yield_command,
    "procs": procs_command,
    "lane-admission": lane_admission_command,
    "version": version_command,
    "changelog": changelog_command,
    "history": history_command,
    "stats": stats_command,
    "available": available_command,
    "advise": advise_command,
    "order": order_command,
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # A person typing just `slotfather` wants to see the queue; a script or agent without a terminal gets usage.
    if not argv:
        if sys.stdin.isatty() and sys.stdout.isatty():
            return dashboard([])
        print("usage: slotfather <command> [options] [args...]\nTry 'slotfather --help'.", file=sys.stderr)
        return 2
    command, rest = argv[0], argv[1:]
    if command in ("-h", "--help"):
        print(format_help())
        return 0
    if command == "help":
        if not rest:
            print(format_help())
            return 0
        if len(rest) == 1 and rest[0] in COMMAND_MAP:
            print(format_command_help(COMMAND_MAP[rest[0]]))
            return 0
        print(f"slotfather: unknown command {rest[0]!r}\nTry 'slotfather --help'.", file=sys.stderr)
        return 2
    if command == "--version":
        if rest:
            print(f"slotfather: unexpected argument {rest[0]!r} for --version\nTry 'slotfather --help'.", file=sys.stderr)
            return 2
        from .version import text
        print(text())
        return 0
    if command.startswith("-"):
        print(f"slotfather: unknown option {command}\nTry 'slotfather --help'.", file=sys.stderr)
        return 2
    spec = COMMAND_MAP.get(command)
    if spec is None:
        print(f"slotfather: unknown command {command!r}\nTry 'slotfather --help'.", file=sys.stderr)
        return 2
    try:
        if spec.passthrough:
            return dashboard(rest)
        if command == "session":
            return session_command(rest)
        if command == "sweep":
            return sweep_command(rest)
        if command == "slots":
            return slots_command(rest)
        values, positionals, after_dash, help_requested = parse_options(rest, spec)
        if help_requested:
            print(format_command_help(spec))
            return 0
        return HANDLERS[command](values, positionals, after_dash)
    except UsageError as error:
        pointer = f"slotfather {error.command} --help" if error.command else "slotfather --help"
        print(f"slotfather: {error}\nTry '{pointer}'.", file=sys.stderr)
        return 2
    except _config.SlotError as error:
        pointer = f"slotfather {command} --help" if command in COMMAND_MAP else "slotfather --help"
        print(f"slotfather: {error}\nTry '{pointer}'.", file=sys.stderr)
        return 2
