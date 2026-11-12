"""Check results by tree and plan (fathers#91): a result belongs to the tree it ran on, not to a branch or commit.

  <state>/results/<tree>.json   {"v": 1, "tree": T, "entries": [entry, ...]}, newest last

An entry is one run's outcome on the tree: {"checks": {id: status}, "full", "platform", "env", "plan", "status",
"source", "lane", "sha", "receipt", "at", "reused", "origin"}. `reused` names the checks the run did not run but took
from another result (an earlier tree of the lane whose change the plan says the check does not read, #640), and `origin`
where each came from. `env` hashes what the tree does not hold but a result depends on: the
platform the run ran on (OS and architecture, owner decision 2026-11-13) and the toolchain the tree's resolver
reports (#571); `plan` hashes the plan (its sorted check ids, or "full" for the full
gate) together with that environment. The tree holds every file a check reads, its configuration included, so the
same tree under the same environment gives the same result.

Reuse:
- a check reuses the newest result of the same check on the same tree under the same environment, when that result
  passed; a newer red result of it wins over an older pass; a result the check produced on this tree always wins
  over one a run took from another tree (inferred), whatever their times, and `lookup` reads only the former;
- a plan is verified on a tree when each of its checks is (`lookup`); the full gate only by a green full entry, since
  the gate's check list is the tree's own;
- a changed environment (another platform or toolchain) reuses nothing; a check that never ran on the tree runs. A
  tree is verified only for the platform it was checked on: a pass on the Mac never stands for Linux;
- an entry naming no platform (an older release's) counts for no platform: the store cannot tell which host wrote
  it, since a state directory can be shared, so the check runs once more.

The interface for callers outside Lanefather (Repo-Kit's gates and CI templates) is `lanefather results`, documented
in docs/results.md. Files older than RETENTION_DAYS go when a newer result is recorded.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform as host
import re
import time
from typing import Any

from . import state
from .journal import now

VERSION = 1
FULL = "full"
# Entries kept per tree; older ones go first. A tree is checked a few times at most (lane, smoke, gate).
KEEP_ENTRIES = 64
RETENTION_DAYS = 60
PASSED = "passed"
TREE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
# A platform is `<os>-<arch>` in Node's names (process.platform, process.arch), as Repo-Kit's status contexts write
# it: darwin-arm64, linux-x64.
PLATFORM = re.compile(r"^[a-z0-9]+-[a-z0-9_]+$")
SYSTEMS = {"darwin": "darwin", "linux": "linux", "windows": "win32"}
MACHINES = {"x86_64": "x64", "amd64": "x64", "x64": "x64", "arm64": "arm64", "aarch64": "arm64", "i386": "ia32",
            "i686": "ia32", "x86": "ia32"}


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def host_platform() -> str:
    """This host's platform, `<os>-<arch>` (darwin-arm64, linux-x64); LANEFATHER_PLATFORM stands in for it."""
    from . import config
    given = config.current().env("PLATFORM")
    if given:
        return checked_platform(given)
    system, machine = host.system().lower(), host.machine().lower()
    return checked_platform(f"{SYSTEMS.get(system, system)}-{MACHINES.get(machine, machine)}")


def checked_platform(given: str) -> str:
    if not PLATFORM.match(given):
        raise ValueError(f"not a platform: {given!r} (give <os>-<arch>, such as linux-x64 or darwin-arm64)")
    return given


def env_hash(toolchain: Any, platform: str | None = None) -> str:
    """The hash of what a result depends on outside the tree: the platform (this host's by default) and the
    toolchain resolution (None for a tree that pins none)."""
    return hashlib.sha256(canonical({"v": VERSION, "platform": platform or host_platform(), "toolchain": toolchain}
                                    ).encode()).hexdigest()


def plan_hash(checks: Any, toolchain: Any = None, platform: str | None = None) -> str:
    """The plan's key: its sorted check ids (or FULL), with the environment's hash, which names the platform."""
    selection = FULL if checks == FULL else sorted(set(checks))
    return hashlib.sha256(canonical({"v": VERSION, "checks": selection, "env": env_hash(toolchain, platform)}
                                    ).encode()).hexdigest()


def under(entry: dict[str, Any], env: str, platform: str) -> bool:
    """Whether the entry ran under the environment: its hash and its platform both say so. An entry naming no
    platform (an older release's) is under none."""
    return entry.get("env") == env and entry.get("platform") == platform


def directory() -> Path:
    path = state.state_dir() / "results"
    path.mkdir(exist_ok=True)
    return path


def path_for(tree: str) -> Path:
    if not TREE.match(tree):
        raise ValueError(f"not a tree id: {tree!r}")
    return directory() / f"{tree}.json"


def entries(tree: str) -> list[dict[str, Any]]:
    """The tree's recorded entries, oldest first; [] for a tree never recorded or an unreadable file."""
    data = state.read_json(path_for(tree)) or {}
    if data.get("v") != VERSION or data.get("tree") != tree or not isinstance(data.get("entries"), list):
        return []
    found: list[Any] = data["entries"]
    return [e for e in found if isinstance(e, dict) and isinstance(e.get("checks"), dict)]  # pyright: ignore[reportUnknownMemberType]


def record(tree: str, checks: dict[str, str], *, source: str, toolchain: Any = None, full: bool = False,
           status: str | None = None, lane: str | None = None, sha: str | None = None, receipt: str | None = None,
           reused: list[str] | None = None, origins: dict[str, dict[str, Any]] | None = None,
           extra: dict[str, Any] | None = None, phase: str | None = None,
           platform: str | None = None) -> dict[str, Any]:
    """Record one run's check statuses on the tree; returns the entry. `status` defaults to green when every check
    passed. A record never fails its caller's command: an unwritable store only loses the reuse. A check that passes
    here after its newest result on the tree, under this environment, failed is a flake in the history (fathers#92).
    `platform` is where the run ran, this host by default."""
    platform = platform or host_platform()
    green = all(s == PASSED for s in checks.values()) and (bool(checks) or full)
    entry: dict[str, Any] = {
        "checks": dict(checks), "full": full, "platform": platform, "env": env_hash(toolchain, platform),
        "plan": plan_hash(FULL if full else list(checks), toolchain, platform),
        "status": status or ("green" if green else "red"), "source": source, "at": now(),
        **({"lane": lane} if lane else {}), **({"sha": sha} if sha else {}),
        **({"receipt": receipt} if receipt else {}), **({"reused": sorted(reused)} if reused else {}),
        **({"origin": {c: o for c, o in origins.items() if c in (reused or ())}} if origins and reused else {}),
        **(extra or {})}
    try:
        with state.locked("results"):
            found = entries(tree)
            before = newest(found, entry["env"], platform)
            found.append(entry)
            state.write_json(path_for(tree), {"v": VERSION, "tree": tree, "entries": found[-KEEP_ENTRIES:]})
            prune()
    except (OSError, ValueError):
        return entry
    from . import history
    for check, given in checks.items():
        prior = before.get(check)
        if (given == PASSED and prior and prior["status"] == "failed" and check not in (reused or ())
                and not history.has_flake(check, tree)):
            history.flake(check, lane=lane, tree=tree, failed_at=prior.get("at"), how="later-run", phase=phase)
    return entry


def newest(found: list[dict[str, Any]], env: str, platform: str) -> dict[str, dict[str, Any]]:
    """Each check's newest status among the entries, under the environment."""
    latest_of: dict[str, dict[str, Any]] = {}
    for entry in found:
        if under(entry, env, platform):
            for check, status in entry["checks"].items():
                latest_of[check] = {"status": status, "at": entry.get("at")}
    return latest_of


def prune(days: int = RETENTION_DAYS) -> None:
    cutoff = time.time() - days * 86400
    for path in directory().glob("*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            continue


def latest(tree: str, toolchain: Any = None, inferred: bool = True,
           platform: str | None = None) -> dict[str, dict[str, Any]]:
    """Each check's newest result on the tree under this environment: {id: {status, source, receipt, at, ...}}. A
    result the check produced here wins over an inferred one; `inferred=False` leaves the inferred ones out."""
    platform = platform or host_platform()
    env = env_hash(toolchain, platform)
    fresh: dict[str, dict[str, Any]] = {}
    taken: dict[str, dict[str, Any]] = {}
    for entry in entries(tree):
        if not under(entry, env, platform):
            continue
        reused = set(entry.get("reused") or ())
        for check, status in entry["checks"].items():
            result = {"status": status, "source": entry.get("source"), "at": entry.get("at"),
                      **{k: entry[k] for k in ("lane", "sha", "receipt") if entry.get(k)}}
            if check in reused:
                taken[check] = dict(result, inferred=True,
                                    **({"origin": entry["origin"][check]}
                                       if isinstance(entry.get("origin"), dict) and entry["origin"].get(check)
                                       else {}))
            else:
                fresh[check] = result
    return {**(taken if inferred else {}), **fresh}


def passed(tree: str, toolchain: Any = None, inferred: bool = True,
           platform: str | None = None) -> dict[str, dict[str, Any]]:
    """The checks whose newest result on the tree under this environment passed."""
    return {c: r for c, r in latest(tree, toolchain, inferred, platform).items() if r["status"] == PASSED}


def full_green(tree: str, toolchain: Any = None, platform: str | None = None) -> dict[str, Any] | None:
    """The newest green full-gate entry of the tree under this environment, unless a newer full run was red."""
    platform = platform or host_platform()
    env = env_hash(toolchain, platform)
    fulls = [e for e in entries(tree) if e.get("full") and under(e, env, platform)]
    return fulls[-1] if fulls and fulls[-1].get("status") == "green" else None


def lookup(tree: str, checks: Any, toolchain: Any = None, platform: str | None = None) -> dict[str, Any]:
    """Whether the plan (`checks`, a list of ids or FULL) is verified on the tree for the platform (this host's by
    default): {v, tree, plan, env, platform, verified, reason, results}. `results` names, per check, the result that
    stands for it, or the full-gate entry."""
    platform = platform or host_platform()
    key = plan_hash(checks, toolchain, platform)
    answer: dict[str, Any] = {"v": VERSION, "tree": tree, "plan": key, "env": env_hash(toolchain, platform),
                              "platform": platform}
    if checks == FULL:
        entry = full_green(tree, toolchain, platform)
        if entry:
            return dict(answer, verified=True, reason=f"the full gate passed on this tree on {platform} "
                                                      f"({entry.get('source')}, {entry.get('at')})",
                        results={FULL: entry})
        return dict(answer, verified=False, reason=f"no green full gate on this tree on {platform} under this "
                                                   f"environment",
                    results={})
    wanted = sorted(set(checks))
    if not wanted:
        return dict(answer, verified=False, reason="an empty plan verifies nothing", results={})
    newest = latest(tree, toolchain, inferred=False, platform=platform)
    missing = [c for c in wanted if newest.get(c, {}).get("status") != PASSED]
    if missing:
        never = [c for c in missing if c not in newest]
        red = [c for c in missing if c in newest]
        parts = ([f"never ran here: {', '.join(never)}"] if never else []) + (
            [f"newest result not passed: {', '.join(red)}"] if red else [])
        return dict(answer, verified=False, reason="; ".join(parts), results={c: newest[c] for c in red})
    return dict(answer, verified=True, reason=f"each of the {len(wanted)} checks passed on this tree on {platform}",
                results={c: newest[c] for c in wanted})


# The command: lanefather results key|lookup|record

def resolve_tree(cwd: str, rev: str) -> str:
    from .errors import LaneError
    from .gitops import git
    tree = git(cwd, "rev-parse", "--verify", "--quiet", rev + "^{tree}", check=False)
    if not tree:
        raise LaneError(f"results: {rev} names no tree here")
    return tree


def selection(args: Any) -> Any:
    from .errors import LaneError
    if args.full == bool(args.checks):
        raise LaneError("results: give exactly one of --full or --checks A,B")
    return FULL if args.full else [c for c in args.checks.split(",") if c]


def environment(cwd: str, rev: str) -> Any:
    """The toolchain a result on REV's tree depends on, resolved in this checkout, which must pin what REV pins: a
    checkout of another pin would resolve another toolchain (review of fathers#91)."""
    from . import config, plan
    from .errors import LaneError
    from .gitops import git
    top = git(cwd, "rev-parse", "--show-toplevel", check=False) or cwd
    pin = config.current().toolchain_pin
    if pin and config.current().toolchain:
        here = Path(top) / pin
        theirs = git(top, "show", f"{rev}:{pin}", check=False)
        if (here.read_text().strip() if here.is_file() else "") != theirs.strip():
            raise LaneError(f"results: {rev} pins another toolchain ({pin}) than this checkout; run in a checkout of "
                            f"{rev}")
    return plan.toolchain_state(top)


def command(args: Any) -> int:
    """`results key` prints the key; `results lookup` answers whether the plan is verified (exit 0) or not (exit 1);
    `results record` stores a result a caller outside Lanefather produced."""
    from .errors import LaneError
    cwd = os.getcwd()
    tree = resolve_tree(cwd, args.rev)
    try:
        platform = checked_platform(args.platform) if getattr(args, "platform", None) else host_platform()
    except ValueError as error:
        raise LaneError(f"results: {error}") from None
    if args.action == "record":
        statuses: dict[str, str] = {}
        if args.full:
            if args.status not in ("green", "red"):
                raise LaneError("results record --full: give --status green or red")
            full, checks = True, {}
            status: str | None = args.status
        else:
            full, status = False, None
            for part in (args.checks or "").split(","):
                check, _, given = part.partition("=")
                if not check:
                    continue
                statuses[check] = given or args.status or ""
                if statuses[check] not in (PASSED, "failed"):
                    raise LaneError(f"results record: {check} needs a status: passed or failed (A=passed or --status)")
            if not statuses:
                raise LaneError("results record: give --full or --checks A=passed,B=failed")
            checks = statuses
        entry = record(tree, checks, source=f"external:{args.source}", toolchain=environment(cwd, args.rev),
                       full=full, status=status, sha=resolve_commit(cwd, args.rev), receipt=args.receipt,
                       platform=platform)
        print(json.dumps({"v": VERSION, "tree": tree, **entry}) if args.json
              else f"Recorded {entry['status']} for plan {entry['plan'][:12]} on tree {tree[:12]}")
        return 0
    wanted = selection(args)
    toolchain = environment(cwd, args.rev)
    if args.action == "key":
        key = {"v": VERSION, "tree": tree, "plan": plan_hash(wanted, toolchain, platform),
               "env": env_hash(toolchain, platform), "platform": platform}
        print(json.dumps(key) if args.json else f"tree {tree} plan {key['plan']}")
        return 0
    answer = lookup(tree, wanted, toolchain, platform)
    if args.json:
        print(json.dumps(answer))
    else:
        print(f"{'verified' if answer['verified'] else 'not verified'}: tree {tree[:12]} plan {answer['plan'][:12]} "
              f"on {platform}: "
              f"{answer['reason']}")
    return 0 if answer["verified"] else 1


def resolve_commit(cwd: str, rev: str) -> str | None:
    from .gitops import git
    return git(cwd, "rev-parse", "--verify", "--quiet", rev + "^{commit}", check=False) or None
