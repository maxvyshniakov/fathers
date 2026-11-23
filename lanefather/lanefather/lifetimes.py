"""Lanefather's seam to Spawnfather, the one owner of lifetimes (fathers#171): Lanefather decides when a lane's
things are no longer needed; Spawnfather owns them, stops what works in them and removes them.

  start, adopt      spawnfather own-resource --owner lane:<project>/<id> --kind worktree|dir --id PATH --json
                    (the lane's worktree, its scratch and its build directories; owning one again succeeds)
  fix-lane handover spawnfather hand --owner lane:<project>/<parent> --kind worktree --to lane:<project>/<fix>
  done, drop        spawnfather reap --owner lane:<project>/<id> [--base REF]... [--disposable PATTERN]...
                    [--override REASON] [--stop-inside] [--dry-run] --json
  gc                spawnfather reap --worktree PATH [--base REF]... [--disposable PATTERN]... [--dry-run] --json

The project is the main checkout's real path, so two repositories with the same lane id never cross. done, drop and
gc register the paths the lane record names again before they reap (register-on-demand): a lane started before the
upgrade, or one whose registration failed, is covered then. Lanefather keeps its own refusals (not landed, commits
not in the integration branch, a fix lane sharing the worktree, the agent's lock, kept lanes); Spawnfather checks its
guards again when it acts. Without a Spawnfather that offers `lifetimes` v1, nothing is removed (fail closed): done,
drop and gc refuse with INSTALL_HINT, exit 3.

The lane's own throwaway checkouts (a base checkout, a review's, the smoke's and the batch gate's) and the shared
cache partitions stay Lanefather's (worktrees.py): they live inside one command and own no lifetime.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
import shutil
import subprocess
import sys
from typing import Any, cast

from . import config, journal, state
from .errors import LaneRefused
from .gitops import main_checkout

INSTALL_HINT = ("Spawnfather is not installed or too old (needs lifetimes v1): install it with "
                "`python3 install.py spawnfather`")
TIMEOUT = 600.0
OK, REFUSED, NOT_FOUND = 0, 3, 6


def command() -> str | None:
    """The spawnfather command: $LANEFATHER_SPAWNFATHER, else `spawnfather` on PATH."""
    return os.environ.get("LANEFATHER_SPAWNFATHER") or shutil.which("spawnfather")


def owner(record: state.Record) -> str:
    """`lane:<project>/<id>`: the project is the lane's main checkout (its real path)."""
    return f"lane:{os.path.realpath(main_checkout(str(record['common_dir'])))}/{record['id']}"


def stop_inside(project: config.Config) -> bool:
    """`[done] stop_inside`: done, drop and gc have Spawnfather stop the unowned processes left in what they remove.
    Unset, a project that still names the deprecated `[hooks] process_scan` (which stopped them) gets it, with a
    warning."""
    if project.done_stop_inside is not None:
        return project.done_stop_inside
    if project.process_scan:
        print(f"{project.command}: warning: [hooks] process_scan is deprecated (fathers#171): Spawnfather stops what a "
              "lane left running; set [done] stop_inside = true instead", file=sys.stderr)
        return True
    return False


@dataclass(frozen=True)
class Evidence:
    """What Lanefather vouches for when it asks for a removal: the refs the work is merged into (`bases`), ignored
    files that may go (`disposable`), a person's override (`override`), and whether to stop what works inside."""
    bases: tuple[str, ...] = ()
    disposable: tuple[str, ...] = ()
    override: str | None = None
    stop_inside: bool = False

    def words(self) -> list[str]:
        found = [word for base in self.bases if base for word in ("--base", base)]
        found += [word for pattern in self.disposable for word in ("--disposable", pattern)]
        if self.override:
            found += ["--override", self.override]
        if self.stop_inside:
            found.append("--stop-inside")
        return found


@dataclass
class Reaped:
    """What one reap did or would do: its result rows, the paths gone, the processes stopped, and why Spawnfather
    keeps the rest (`kept`, one line each)."""
    rows: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    removed: list[str] = field(default_factory=list[str])
    stopped: list[str] = field(default_factory=list[str])
    kept: list[str] = field(default_factory=list[str])


class Spawnfather:
    """The installed spawnfather, negotiated through `capabilities --json` (lifetimes v1)."""

    def __init__(self, program: str):
        self.program = program

    def run(self, *args: str) -> tuple[int, str, str]:
        try:
            done = subprocess.run([self.program, *args], capture_output=True, text=True, timeout=TIMEOUT,
                                  stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as error:
            return 1, "", str(error)
        return done.returncode, done.stdout, done.stderr

    def supported(self) -> bool:
        code, out, _ = self.run("capabilities", "--json")
        if code != 0:
            return False
        try:
            data: Any = json.loads(out)
        except ValueError:
            return False
        if not isinstance(data, dict):
            return False
        lifetimes: Any = cast(dict[str, Any], data).get("lifetimes")
        return isinstance(lifetimes, dict) and cast(dict[str, Any], lifetimes).get("v") == 1

    def register(self, lane: str, kind: str, path: str) -> str | None:
        """Own `path` for the lane; None when it is owned (now or already) or gone, else why not."""
        code, _, err = self.run("own-resource", "--owner", lane, "--kind", kind, "--id", path, "--json")
        if code in (OK, NOT_FOUND):
            return None
        return (err.strip() or f"exit {code}").removeprefix("spawnfather: ")

    def hand(self, source: str, target: str, kind: str | None = None) -> str | None:
        code, _, err = self.run("hand", "--owner", source, *(["--kind", kind] if kind else []), "--to", target,
                                "--json")
        return None if code == OK else (err.strip() or f"exit {code}").removeprefix("spawnfather: ")

    def _rows(self, args: list[str]) -> list[dict[str, Any]]:
        code, out, err = self.run(*args)
        try:
            data: Any = json.loads(out)
            rows: Any = data["results"]
            if not isinstance(rows, list):
                raise TypeError("results is not a list")
            return [cast(dict[str, Any], row) for row in cast(list[Any], rows) if isinstance(row, dict)]
        except (ValueError, KeyError, TypeError):
            raise LaneRefused(f"spawnfather {' '.join(args[:3])} gave no answer (exit {code}): "
                              f"{(err.strip() or out.strip())[:300]}") from None

    def reap(self, lane: str, evidence: Evidence, *, dry_run: bool, reason: str) -> Reaped:
        return outcome(self._rows(["reap", "--owner", lane, *evidence.words(), "--reason", reason, "--json",
                                   *(["--dry-run"] if dry_run else [])]), dry_run)

    def remove_worktree(self, path: str, evidence: Evidence, *, dry_run: bool, reason: str) -> Reaped:
        return outcome(self._rows(["reap", "--worktree", path, *evidence.words(), "--reason", reason, "--json",
                                   *(["--dry-run"] if dry_run else [])]), dry_run)


def outcome(rows: list[dict[str, Any]], dry_run: bool) -> Reaped:
    found = Reaped(rows=rows)
    for row in rows:
        resource = cast(dict[str, Any], row.get("resource") or {})
        what = str(resource.get("id") or row.get("how") or row.get("id"))
        result = str(row.get("result"))
        found.stopped += [str(item) for item in cast(list[Any], row.get("stopped") or row.get("stops") or [])]
        if result in ("failed", "would-fail", "unknown"):
            found.kept.append(f"{what}: {str(row.get('reason') or result).removeprefix('reap; ')}")
        elif result in ("stopped", "killed", "gone", "would-stop") and resource and (
                not dry_run or result == "would-stop"):
            found.removed.append(what)
    return found


def client() -> Spawnfather | None:
    """The installed Spawnfather that offers lifetimes v1, or None."""
    program = command()
    if not program:
        return None
    found = Spawnfather(program)
    return found if found.supported() else None


def require() -> Spawnfather:
    """The installed Spawnfather, or the refusal with the install hint (exit 3)."""
    found = client()
    if found is None:
        raise LaneRefused(INSTALL_HINT)
    return found


def lane_paths(record: state.Record) -> list[tuple[str, str]]:
    """(kind, path) of what the lane owns and that exists now: its worktree, its build directories, its scratch."""
    found: list[tuple[str, str]] = []
    worktree = record.get("worktree")
    if isinstance(worktree, str) and worktree and os.path.isdir(worktree):
        found.append(("worktree", os.path.realpath(worktree)))
    for path in [*record.get("cargo_targets", []), *record.get("scratch", [])]:
        if isinstance(path, str) and path and os.path.isdir(path) and not os.path.islink(path):
            found.append(("dir", os.path.realpath(path)))
    return found


def register(spawnfather: Spawnfather, record: state.Record, *, skip: tuple[str, ...] = ()) -> list[str]:
    """Own every path the lane names (`lane_paths`) for its lane owner; one line per path Spawnfather refused.
    `skip`: kinds left out (a fix lane takes its worktree by hand-over)."""
    lane = owner(record)
    return [f"{path}: {why}" for kind, path in lane_paths(record) if kind not in skip
            for why in [spawnfather.register(lane, kind, path)] if why]


def register_quietly(store: state.Store, record: state.Record, verb: str, *, skip: tuple[str, ...] = ()) -> None:
    """start and adopt: register the lane's paths; a missing Spawnfather or a refusal is a warning and a ledger
    entry, never a failure (done registers them again before it removes anything)."""
    found = client()
    problems = [INSTALL_HINT + "; done will refuse until it is"] if found is None else register(found, record,
                                                                                                  skip=skip)
    if not problems:
        return
    prog = store.project.command
    for problem in problems:
        print(f"{prog}: warning: {verb} {record['id']}: Spawnfather does not own {problem}", file=sys.stderr)
    journal.ledger(f"{verb} {record['id']}: Spawnfather does not own {'; '.join(problems)}",
                   kind="lane.unregistered", level="warn", lane=record,
                   details={"command": verb, "problems": problems}, store=store)


def hand_worktree(store: state.Store, parent: state.Record, fix: state.Record) -> None:
    """A fix lane takes over its parent's worktree: hand that entry to the fix lane, then own the fix lane's own
    paths. A failure is a warning: done registers again."""
    found = client()
    problems: list[str] = []
    if found is None:
        problems.append(INSTALL_HINT)
    else:
        why = found.hand(owner(parent), owner(fix), "worktree")
        if why:
            problems.append(f"the hand-over of {fix.get('worktree')}: {why}")
        problems += register(found, fix)
    for problem in problems:
        print(f"{store.project.command}: warning: fix lane {fix['id']}: {problem}", file=sys.stderr)
    if problems:
        journal.ledger(f"fix lane {fix['id']}: Spawnfather {'; '.join(problems)}", kind="lane.unregistered",
                       level="warn", lane=fix, details={"command": "hand_over", "problems": problems}, store=store)
