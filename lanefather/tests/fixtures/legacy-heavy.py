#!/usr/bin/env python3
"""Straylight's client of `slotfather`, the machine-wide queue of slot leases every project shares (#599, #619).

slotfather is a machine service, installed as ~/.local/bin/slotfather and kept compatible by its own repository;
Straylight only calls the command, as Sid-Kit's scripts/heavy.sh does:

  slotfather run -n K|all --label L [--meta K=V]... [--class C] [--yieldable] [--wait S] -- COMMAND...
                                                               lease K slots and run COMMAND under them
  slotfather yield                                             inside a yieldable lease: give the slots back
                                                               for a while if a waiter asked (#643)
  slotfather advise [--max N] [--exclusive]                    JSON advice on how many jobs to run
  slotfather slots                                             the shared capacity

COMMAND gets SLOTFATHER_LEASE and SLOTFATHER_COUNT, so a nested run takes no second lease, and a yieldable lease's
command SLOTFATHER_YIELD, the path of a waiter's request. Without slotfather on PATH (a machine or CI runner that does
not have it) a command runs directly, under no lease.

Every lease says who asked and why (#650): `--meta` carries the lane and ticket of the worktree it runs in (from
tools/lane's records), the session (CLAUDE_CODE_SESSION_ID), and what the caller adds (phase, kind, check); `--class`
says how much someone waits for it. tools/lane and verify.sh hand their choice to a verify.sh that queues itself
through STRAYLIGHT_SLOT_LABEL, STRAYLIGHT_SLOT_CLASS (interactive|normal|batch, default normal),
STRAYLIGHT_SLOT_META (a JSON object) and STRAYLIGHT_SLOT_YIELDABLE=1. Only the client that took a yieldable lease
yields, between its own steps; it hides SLOTFATHER_YIELD from the commands it runs.

As a command, for agents' ad-hoc runs:

  tools/heavy.py run [-n K|all] [--label L] [--kind K] [--class C] [--yieldable|--no-yieldable] [--meta K=V]...
                     [--repeat N] [--wait S] -- COMMAND...

runs COMMAND under a lease with that metadata, and the lease's cores as STRAYLIGHT_CHECK_CORES (#642), from which
tools/cargo-test.py sizes its Cargo jobs as under verify.sh. --kind profile, stress, repro or measure makes it batch and
yieldable unless --class or --no-yieldable says otherwise. A lease yields only at a step boundary: --repeat N runs
COMMAND N times and yields between runs when asked, and verify.sh yields between its checks; any other single
command takes a lease that does not yield.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

TIMED_OUT = 75  # slotfather run --wait gave up; the command did not run
STATUS = "slotfather status"  # the command that shows the queue, for messages
CLASSES = ("interactive", "normal", "batch")
# Ad-hoc kinds nobody waits on minute by minute: batch, and they give their slots back between runs when asked.
BACKGROUND_KINDS = ("profile", "stress", "repro", "measure")
META_KEY = re.compile(r"[a-z0-9_.-]{1,32}")
META_VALUE_LIMIT, META_KEYS_LIMIT = 200, 16  # slotfather's limits


class Unavailable(Exception):
    """slotfather is not installed or did not answer."""


class LeaseLost(Exception):
    """`slotfather yield` failed: the lease is gone, so nothing more may run under it."""


def command() -> str | None:
    """The slotfather on PATH, or None."""
    return shutil.which("slotfather")


def leased() -> bool:
    """Whether this process runs under a lease (an outer slotfather run handed it one)."""
    return bool(os.environ.get("SLOTFATHER_LEASE"))


def lease_count() -> int | None:
    """The slots of the lease this process runs under, or None."""
    value = os.environ.get("SLOTFATHER_COUNT", "")
    return int(value) if leased() and value.isdigit() else None


def cpus() -> int:
    """The machine's CPUs that leases divide among their slots; STRAYLIGHT_VERIFY_CPUS overrides the host's count."""
    configured = os.environ.get("STRAYLIGHT_VERIFY_CPUS", "")
    return int(configured) if configured.isdigit() and int(configured) > 0 else os.cpu_count() or 1


def cores() -> float | None:
    """The cores this process may use (#642): what verify.py or the `heavy.py run` that took its lease gave it
    (STRAYLIGHT_CHECK_CORES), else the cores of the lease it runs under (its slots of cpus() over slotfather's
    capacity), else None."""
    given = os.environ.get("STRAYLIGHT_CHECK_CORES", "")
    try:
        if float(given) > 0:
            return float(given)
    except ValueError:
        pass
    count = lease_count()
    if count is None:
        return None
    try:
        capacity = total()
    except Unavailable:
        return None
    return cpus() * min(count, capacity) / capacity


def lane_records() -> Path:
    return Path(os.path.expanduser(os.environ.get("STRAYLIGHT_LANE_STATE")
                                   or "~/.local/state/straylight-orchestration/lanes"))


def worktree_root(path: str | os.PathLike) -> str | None:
    """The Git worktree `path` is in (the first directory upward holding .git), or None."""
    current = os.path.realpath(path)
    while True:
        if os.path.exists(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def lane_of(path: str | os.PathLike) -> dict | None:
    """{"lane", "ticket"} of the tools/lane lane whose worktree holds `path`, or None. A review-fix lane takes over
    its landed lane's worktree, so a record still working in it wins over a finished one."""
    root = worktree_root(path)
    if root is None:
        return None
    found = []
    try:
        names = sorted(os.listdir(lane_records()))
    except OSError:
        return None
    for name in names:
        if not re.fullmatch(r"[^.]+\.json", name):
            continue
        try:
            record = json.loads((lane_records() / name).read_text())
        except (OSError, ValueError):
            continue
        if isinstance(record, dict) and isinstance(record.get("worktree"), str) \
                and os.path.realpath(record["worktree"]) == root:
            found.append(record)
    if not found:
        return None
    record = max(found, key=lambda r: (r.get("status") not in ("done", "released"), r.get("created") or ""))
    ticket = str(record.get("ticket") or "").lstrip("#")
    return {"lane": str(record.get("id") or ""), **({"ticket": ticket} if ticket else {})}


def clean_meta(meta: dict) -> dict:
    """The metadata slotfather accepts: lowercase keys of at most 32 characters, printable values of at most 200,
    at most 16 keys; empty values are left out."""
    result = {}
    for key, value in meta.items():
        if value is None or value == "" or not META_KEY.fullmatch(str(key)):
            continue
        text = "".join(c if c.isprintable() else " " for c in str(value))[:META_VALUE_LIMIT]
        result[str(key)] = text
    return dict(list(result.items())[:META_KEYS_LIMIT])


def lease_meta(cwd: str | os.PathLike | None = None, meta: dict | None = None) -> dict:
    """Who asks and why: the lane of the worktree `cwd` is in, the session, STRAYLIGHT_SLOT_META, then `meta`."""
    found = dict(lane_of(cwd if cwd is not None else os.getcwd()) or {})
    session = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if session:
        found["session"] = session
    configured = os.environ.get("STRAYLIGHT_SLOT_META")
    if configured:
        try:
            given = json.loads(configured)
        except ValueError:
            raise ValueError(f"STRAYLIGHT_SLOT_META is not a JSON object: {configured[:200]}") from None
        if not isinstance(given, dict):
            raise ValueError(f"STRAYLIGHT_SLOT_META is not a JSON object: {configured[:200]}")
        found.update(given)
    found.update(meta or {})
    return clean_meta(found)


def lease_class(klass: str | None = None) -> str:
    """`klass`, else STRAYLIGHT_SLOT_CLASS, else normal."""
    chosen = klass or os.environ.get("STRAYLIGHT_SLOT_CLASS") or "normal"
    if chosen not in CLASSES:
        raise ValueError(f"lease class {chosen!r} is not one of {', '.join(CLASSES)}")
    return chosen


def wrap(count: int | str, label: str, argv: list[str], wait: float | None = None, meta: dict | None = None,
         klass: str | None = None, yieldable: bool | None = None, cwd: str | os.PathLike | None = None) -> list[str]:
    """argv run under a lease of `count` slots (a number or "all") with its metadata, class and whether it yields
    (default STRAYLIGHT_SLOT_YIELDABLE=1); argv itself under a lease or without slotfather."""
    found = command()
    if leased() or found is None:
        return list(argv)
    if yieldable is None:
        yieldable = os.environ.get("STRAYLIGHT_SLOT_YIELDABLE") == "1"
    flags = [part for key, value in lease_meta(cwd, meta).items() for part in ("--meta", f"{key}={value}")]
    return [found, "run", "-n", str(count), "--label", label, *flags, "--class", lease_class(klass),
            *(("--yieldable",) if yieldable else ()), *(("--wait", f"{wait:g}") if wait is not None else ()),
            "--", *argv]


def queue_environment(label: str, klass: str = "normal", yieldable: bool = False, **meta) -> dict:
    """The variables that make a verify.sh which queues itself take this label, class and metadata."""
    return {"STRAYLIGHT_SLOT_LABEL": label, "STRAYLIGHT_SLOT_CLASS": lease_class(klass),
            "STRAYLIGHT_SLOT_META": json.dumps(clean_meta(meta), sort_keys=True),
            "STRAYLIGHT_SLOT_YIELDABLE": "1" if yieldable else ""}


def yield_requested() -> bool:
    """Whether a waiter asked this process's yieldable lease to give its slots back (one stat)."""
    path = os.environ.get("SLOTFATHER_YIELD")
    return bool(path) and os.path.exists(path)


def yield_slots() -> None:
    """`slotfather yield`: back in the queue behind the waiter, returning once the lease holds its slots again.
    Call it only between steps. Raises LeaseLost when it fails: the lease is gone, so stop."""
    found = command()
    if found is None:
        raise LeaseLost("slotfather is not on PATH, so the lease cannot yield")
    try:
        result = subprocess.run([found, "yield"], capture_output=True, text=True)
    except OSError as error:
        raise LeaseLost(f"slotfather yield: {error}") from None
    if result.returncode:
        raise LeaseLost(f"slotfather yield exited {result.returncode}: "
                        f"{(result.stderr or result.stdout).strip()[:300]}")


def without_yield(environment: dict) -> dict:
    """`environment` for a command this client runs as one step: only the client that took the lease yields."""
    return {key: value for key, value in environment.items() if key != "SLOTFATHER_YIELD"}


def _ask(*args: str) -> str:
    found = command()
    if found is None:
        raise Unavailable("slotfather is not on PATH")
    try:
        result = subprocess.run([found, *args], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as error:
        raise Unavailable(f"slotfather {' '.join(args)}: {error}") from None
    if result.returncode:
        raise Unavailable(f"slotfather {' '.join(args)} exited {result.returncode}: "
                          f"{(result.stderr or result.stdout).strip()[:300]}")
    return result.stdout


def advise(maximum: int | None = None, exclusive: bool = False) -> dict:
    """`slotfather advise`: the job count for the load and the queue, with the reasons slotfather gives."""
    output = _ask("advise", *(("--max", str(maximum)) if maximum else ()), *(("--exclusive",) if exclusive else ()))
    try:
        advice = json.loads(output)
        int(advice["jobs"])
    except (ValueError, KeyError, TypeError):
        raise Unavailable(f"slotfather advise printed no job count: {output.strip()[:300]}") from None
    return advice


def total() -> int:
    """The machine's slot capacity (`slotfather slots`)."""
    output = _ask("slots")
    found = re.match(r"\s*(\d+)", output)
    if not found:
        raise Unavailable(f"slotfather slots printed no capacity: {output.strip()[:300]}")
    return int(found.group(1))


def yields_itself(argv: list[str]) -> bool:
    """Whether argv is a client that yields between its own steps: verify.sh between checks."""
    return bool(argv) and os.path.basename(argv[0]) == "verify.sh"


def repeat(times: int, argv: list[str]) -> int:
    """Run argv `times` times under the lease this process holds, yielding between runs when a waiter asked:
    0 when every run passed, else the first failing status. Stops when a yield fails (the lease is gone)."""
    failed, first = 0, 0
    # The command learns the cores of its lease (#642): tools/cargo-test.py sizes its Cargo jobs from them.
    held = cores()
    if held is not None:
        os.environ["STRAYLIGHT_CHECK_CORES"] = f"{held:g}"
    for index in range(times):
        if index and yield_requested():
            print(f"heavy: run {index + 1} of {times} waits: the lease yields to a waiter ({STATUS})", flush=True)
            try:
                yield_slots()
            except LeaseLost as error:
                print(f"heavy: {error}; stopping after {index} of {times} runs", file=sys.stderr, flush=True)
                return first or 1
        environment = dict(os.environ) if yields_itself(argv) else without_yield(dict(os.environ))
        status = subprocess.run(argv, env=environment).returncode
        if status:
            failed += 1
            first = first or status
    if times > 1:
        print(f"heavy: {times - failed} of {times} runs passed", flush=True)
    return first


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="tools/heavy.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run COMMAND under a lease with this lane's metadata")
    run.add_argument("-n", dest="count", default="1", help="slots: a number or all (default 1)")
    run.add_argument("--label", help="the queue's name for it (default: the command's first word)")
    run.add_argument("--kind", help="what it is: profile, stress, repro and measure are batch and yieldable")
    run.add_argument("--class", dest="klass", choices=CLASSES, help="default: batch for those kinds, else normal")
    run.add_argument("--yieldable", action=argparse.BooleanOptionalAction, default=None,
                     help="give the slots back between --repeat runs when asked (default: for those kinds)")
    run.add_argument("--meta", action="append", default=[], metavar="KEY=VALUE")
    run.add_argument("--repeat", type=int, default=1, metavar="N", help="run COMMAND N times (default 1)")
    run.add_argument("--wait", type=float, help="give up with exit 75 after S seconds in the queue")
    run.add_argument("argv", nargs=argparse.REMAINDER, metavar="-- COMMAND...")
    inner = commands.add_parser("repeat", help=argparse.SUPPRESS)
    inner.add_argument("times", type=int)
    inner.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    rest = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
    if not rest:
        parser.error("no COMMAND to run")
    if args.command == "repeat":
        return repeat(args.times, rest)
    if args.count != "all" and not args.count.isdigit():
        parser.error("-n takes a number or all")
    if args.repeat < 1:
        parser.error("--repeat takes a positive count")
    meta = {"kind": args.kind}
    for item in args.meta:
        key, separator, value = item.partition("=")
        if not separator or not META_KEY.fullmatch(key):
            parser.error(f"--meta takes KEY=VALUE with a lowercase key: {item}")
        meta[key] = value
    background = args.kind in BACKGROUND_KINDS
    klass = args.klass or ("batch" if background else None)
    # A lease yields only between steps: between repeated runs, or verify.sh between its checks.
    steps = args.repeat > 1 or yields_itself(rest)
    yieldable = (args.yieldable if args.yieldable is not None else background) and steps
    if args.yieldable and not steps:
        print("heavy: --yieldable needs --repeat N (or verify.sh): a single run of this command has no step "
              "boundary to yield at; taking a lease that does not yield", file=sys.stderr)
    body = [sys.executable, os.path.abspath(__file__), "repeat", str(args.repeat), "--", *rest]
    try:
        wrapped = wrap(args.count, args.label or os.path.basename(rest[0]), body, wait=args.wait, meta=meta,
                       klass=klass, yieldable=yieldable)
    except ValueError as error:
        parser.error(str(error))
    if wrapped != body:
        # A lease of its own: the cores a caller's check was given are not this lease's (#642 review).
        os.environ.pop("STRAYLIGHT_CHECK_CORES", None)
    sys.stdout.flush()
    os.execvp(wrapped[0], wrapped)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
