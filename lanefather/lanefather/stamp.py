"""Verified-tree stamp ledger and `.md`-only equivalence checks (fathers#48).

A full verification run stamps the committed Git tree hash in `<git common dir>/<stamp_file>` (default
`verified-trees`), so every worktree of the clone shares the ledger and commit-message edits, amends or rebases
with an identical tree stay verified. A tree that differs from a stamped tree only in `.md` files also counts as
verified.

Row format (at most 50 entries retained, oldest evicted first):
  <tree> <commit> <timestamp>[ <token>]

When a 32-hex transaction token is supplied, evictions are journaled in
`<git common dir>/verified-tree-transaction-<token>.json` before publication so `abort` can roll back only that
run's stamp without losing earlier stamps.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any

from fathers_common.atomic import atomic_write

from . import config
from .errors import LaneError, LaneUsageError
from .gitops import common_dir, git, resolve

VERSION = 1
BUILTIN = "builtin"
DEFAULT_STAMP_FILE = "verified-trees"
MAX_ENTRIES = 50
TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
TREE_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


class StampStateError(ValueError):
    """Raised when a ledger operation conflicts with current ledger state (e.g. reused token)."""


@dataclass(frozen=True)
class Entry:
    """One row of the verified-tree stamp ledger."""

    tree: str
    commit: str = "-"
    timestamp: str = ""
    token: str | None = None

    def format_row(self) -> str:
        parts = [self.tree, self.commit, self.timestamp]
        if self.token is not None:
            parts.append(self.token)
        return " ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"tree": self.tree, "commit": self.commit, "timestamp": self.timestamp}
        if self.token is not None:
            data["token"] = self.token
        return data


@dataclass(frozen=True)
class CheckResult:
    """Whether a target tree is verified by the ledger (exact or `.md`-only equivalent)."""

    verified: bool
    tree: str
    match: str | None = None
    matched_tree: str | None = None
    md_paths: tuple[str, ...] = ()
    ledger: Path | None = None


def utc_now() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp_line(tree: str) -> str:
    """The standard line `verify.STAMPED` parses when a full run stamps a tree."""
    return f"Verified tree {tree} stamped."


def not_stamped_line(reason: str) -> str:
    """The standard line `verify.NOT_STAMPED` parses when a full run does not stamp."""
    return f"Verified tree not stamped: {reason}."


def validate_stamp_file(name: str) -> str:
    """Validate that `name` is a plain filename within the Git common directory (no path traversal or dotfiles)."""
    if not name or "/" in name or "\\" in name or ".." in name or name.startswith("."):
        raise ValueError(
            f"invalid stamp file {name!r}: must be a plain filename without path separators, '..' or leading '.'"
        )
    return name


def ledger_path(common: Path | str, stamp_file: str = DEFAULT_STAMP_FILE) -> Path:
    return Path(common) / validate_stamp_file(stamp_file)


def lock_path(common: Path | str, stamp_file: str = DEFAULT_STAMP_FILE) -> Path:
    return Path(common) / f"{validate_stamp_file(stamp_file)}.lock"


def journal_path(common: Path | str, token: str) -> Path:
    return Path(common) / f"verified-tree-transaction-{token}.json"


def _owned(row: str, token: str) -> bool:
    fields = row.split()
    return len(fields) == 4 and fields[3] == token


def _validate_token(token: str | None) -> None:
    if token is not None and not TOKEN_RE.fullmatch(token):
        raise ValueError("invalid verification stamp token")


def read_entries(common: Path | str, *, stamp_file: str = DEFAULT_STAMP_FILE) -> list[Entry]:
    """Read stamped entries from `<common>/<stamp_file>`, oldest first."""
    path = ledger_path(common, stamp_file)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries: list[Entry] = []
    for line in lines:
        fields = line.split()
        if not fields or not TREE_RE.fullmatch(fields[0]):
            continue
        tree = fields[0]
        commit = fields[1] if len(fields) > 1 else "-"
        timestamp = fields[2] if len(fields) > 2 else ""
        token = fields[3] if len(fields) > 3 else None
        entries.append(Entry(tree=tree, commit=commit, timestamp=timestamp, token=token))
    return entries


def stamped_trees(common: Path | str, *, stamp_file: str = DEFAULT_STAMP_FILE) -> set[str]:
    """The set of tree hashes recorded in `<common>/<stamp_file>`."""
    return {entry.tree for entry in read_entries(common, stamp_file=stamp_file)}


def mutate(
    directory: Path,
    operation: str,
    arguments: Sequence[str],
    *,
    stamp_file: str = DEFAULT_STAMP_FILE,
) -> None:
    """Serialize stamp publication and rollback of one verification run's stamp under an exclusive lock."""
    ledger = ledger_path(directory, stamp_file)
    token: str | None = (
        arguments[3]
        if operation == "add" and len(arguments) == 4
        else arguments[0]
        if operation != "add" and arguments
        else None
    )
    _validate_token(token)
    journal = journal_path(directory, token) if token else None
    directory.mkdir(parents=True, exist_ok=True)
    with lock_path(directory, stamp_file).open("a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        rows = ledger.read_text(encoding="utf-8").splitlines() if ledger.exists() else []
        if operation == "add":
            if len(arguments) not in (3, 4):
                raise ValueError("add requires tree, commit, timestamp and optional token")
            record = " ".join(arguments)
            keep = MAX_ENTRIES - 1
            if journal and token is not None:
                if journal.exists() or any(_owned(row, token) for row in rows):
                    raise StampStateError("verification stamp token already used")
                # Record evictions BEFORE publication, so interruption after rename
                # can remove only this run without losing its earlier stamps.
                atomic_write(journal, json.dumps({"before": rows, "pruned": rows[:-keep]}) + "\n")
            atomic_write(ledger, "\n".join(rows[-keep:] + [record]) + "\n")
        elif operation == "abort":
            if token is None:
                raise ValueError("abort requires a verification stamp token")
            published = any(_owned(row, token) for row in rows)
            kept = [row for row in rows if not _owned(row, token)]
            if published and journal and journal.exists():
                transaction = json.loads(journal.read_text(encoding="utf-8"))
                before: list[str] = list(transaction["before"])
                pruned: list[str] = list(transaction["pruned"])
                # Place evictions before the first surviving original successor.
                # This also preserves order if interleaved writers both abort.
                index = next((kept.index(row) for row in before[len(pruned):] if row in kept), 0)
                # Preserve duplicates: each row evicted by this publication is
                # restored once. Atomic token removal makes retry idempotent.
                kept[index:index] = pruned
            if kept != rows:
                atomic_write(ledger, "\n".join(kept) + ("\n" if kept else ""))
            if journal:
                journal.unlink(missing_ok=True)
        elif operation == "complete":
            if token is None:
                raise ValueError("complete requires a verification stamp token")
            if journal:
                journal.unlink(missing_ok=True)
        else:
            raise ValueError("unknown stamp operation")


def add(
    directory: Path | str,
    tree: str,
    commit: str = "-",
    timestamp: str | None = None,
    *,
    token: str | None = None,
    stamp_file: str = DEFAULT_STAMP_FILE,
) -> Entry:
    """Atomically append a verified tree entry to `<directory>/<stamp_file>`."""
    ts = timestamp if timestamp is not None else utc_now()
    args = [tree, commit, ts] if token is None else [tree, commit, ts, token]
    mutate(Path(directory), "add", args, stamp_file=stamp_file)
    return Entry(tree=tree, commit=commit, timestamp=ts, token=token)


def abort(directory: Path | str, token: str, *, stamp_file: str = DEFAULT_STAMP_FILE) -> None:
    """Roll back the stamped row owned by `token` and restore any rows it evicted."""
    mutate(Path(directory), "abort", [token], stamp_file=stamp_file)


def complete(directory: Path | str, token: str, *, stamp_file: str = DEFAULT_STAMP_FILE) -> None:
    """Clean up the transaction journal for `token` once verification publication completes."""
    mutate(Path(directory), "complete", [token], stamp_file=stamp_file)


def snapshot(cwd: str) -> tuple[str, str]:
    """Return `(tree, 'clean'|'dirty')` for `HEAD` in `cwd`; untracked files do not count."""
    tree = git(cwd, "rev-parse", "-q", "--verify", "HEAD^{tree}", check=False) or "none"
    changes = git(cwd, "--no-optional-locks", "status", "--porcelain", "--untracked-files=no", check=False)
    return tree, ("clean" if not changes else "dirty")


def finish(
    cwd: str,
    common: Path | str,
    start_state_text: str,
    *,
    token: str | None = None,
    stamp_file: str = DEFAULT_STAMP_FILE,
) -> tuple[bool, str, Entry | None]:
    """Stamp `HEAD`'s tree if it stayed clean and unchanged from `start_state_text`, else `(False, reason, None)`."""
    parts = start_state_text.split()
    start_tree = parts[0] if parts else ""
    start_state = parts[1] if len(parts) > 1 else ""
    end_tree, end_state = snapshot(cwd)
    reason = ""
    if not start_tree or start_tree == "none":
        reason = "no Git commit was checked out"
    elif start_state != "clean":
        reason = "uncommitted changes to tracked files at the start"
    elif end_tree != start_tree:
        reason = "HEAD's tree changed during the run"
    elif end_state != "clean":
        reason = "tracked files changed during the run"
    if reason:
        return False, reason, None
    commit = resolve(cwd, "HEAD") or "-"
    entry = add(common, start_tree, commit, token=token, stamp_file=stamp_file)
    return True, "", entry


def is_md_path(path: str) -> bool:
    """Whether `path` is a Markdown documentation file for `.md`-only stamp equivalence (case-sensitive)."""
    return path.endswith(".md")


def _diff_paths(cwd: str, tree_a: str, tree_b: str) -> list[str] | None:
    """Changed paths between two Git trees, or None if either tree is absent from the object store."""
    if not TREE_RE.fullmatch(tree_a) or not TREE_RE.fullmatch(tree_b):
        return None
    result = subprocess.run(
        ["git", "diff-tree", "-r", "--name-only", "-z", "--end-of-options", tree_a, tree_b],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return [path for path in result.stdout.split("\0") if path]


def check_tree(
    cwd: str,
    common: Path | str,
    rev: str = "HEAD",
    *,
    stamp_file: str = DEFAULT_STAMP_FILE,
) -> CheckResult:
    """Check whether `rev`'s tree is stamped or differs from a stamped tree only in `.md` files."""
    tree = git(cwd, "rev-parse", "--verify", "--quiet", f"{rev}^{{tree}}", check=False)
    if not tree:
        raise LaneUsageError(f"stamp: {rev} names no tree here")
    ledger = ledger_path(common, stamp_file)
    entries = read_entries(common, stamp_file=stamp_file)
    newest_first = list(dict.fromkeys(reversed([entry.tree for entry in entries])))
    if tree in newest_first:
        return CheckResult(verified=True, tree=tree, match="exact", matched_tree=tree, ledger=ledger)
    for candidate in newest_first:
        if not TREE_RE.fullmatch(candidate) or candidate == tree:
            continue
        paths = _diff_paths(cwd, candidate, tree)
        if not paths:
            continue
        if all(is_md_path(path) for path in paths):
            return CheckResult(
                verified=True,
                tree=tree,
                match="md-only",
                matched_tree=candidate,
                md_paths=tuple(paths),
                ledger=ledger,
            )
    return CheckResult(verified=False, tree=tree, match=None, matched_tree=None, ledger=ledger)


def _require_common_dir(cwd: str) -> Path:
    try:
        return Path(common_dir(cwd))
    except LaneError:
        raise LaneError("stamp: not in a Git repository") from None


def command(args: Any, project: config.Config = config.DEFAULT) -> int:
    """CLI handler for `lanefather stamp`."""
    action: str = args.action
    target: str | None = args.target
    token: str | None = args.token
    as_json: bool = bool(args.json)
    raw_file: str = args.file if args.file is not None else (project.stamp_file or DEFAULT_STAMP_FILE)
    try:
        stamp_file = validate_stamp_file(raw_file)
    except ValueError as error:
        raise LaneUsageError(str(error)) from None
    cwd = os.getcwd()

    if action in ("check", "list", "start") and token is not None:
        raise LaneUsageError(f"--token does not go with {action}")
    if action in ("list", "start") and target is not None:
        raise LaneUsageError(f"{action} takes no positional argument")

    if action == "start":
        tree, state_text = snapshot(cwd)
        if as_json:
            print(json.dumps({"v": VERSION, "tree": tree, "state": state_text}))
        else:
            print(f"{tree} {state_text}")
        return 0

    common = _require_common_dir(cwd)

    if action == "record":
        rev = target or "HEAD"
        tree = git(cwd, "rev-parse", "--verify", "--quiet", f"{rev}^{{tree}}", check=False)
        if not tree:
            raise LaneUsageError(f"stamp: {rev} names no tree here")
        commit = resolve(cwd, rev) or "-"
        try:
            entry = add(common, tree, commit, token=token, stamp_file=stamp_file)
        except StampStateError as error:
            raise LaneError(str(error)) from None
        except ValueError as error:
            raise LaneUsageError(str(error)) from None
        line = stamp_line(entry.tree)
        if as_json:
            print(json.dumps({
                "v": VERSION,
                "stamped": True,
                "tree": entry.tree,
                "commit": entry.commit,
                "timestamp": entry.timestamp,
                "token": entry.token,
                "ledger": str(ledger_path(common, stamp_file)),
                "line": line,
            }))
        else:
            print(line)
        return 0

    if action == "finish":
        if not target:
            raise LaneUsageError("finish requires the snapshot printed by 'stamp start'")
        try:
            ok, reason, entry = finish(cwd, common, target, token=token, stamp_file=stamp_file)
        except StampStateError as error:
            raise LaneError(str(error)) from None
        except ValueError as error:
            raise LaneUsageError(str(error)) from None
        line = stamp_line(entry.tree) if ok and entry is not None else not_stamped_line(reason)
        if as_json:
            print(json.dumps({
                "v": VERSION,
                "stamped": ok,
                "tree": entry.tree if entry is not None else None,
                "commit": entry.commit if entry is not None else None,
                "timestamp": entry.timestamp if entry is not None else None,
                "reason": None if ok else reason,
                "ledger": str(ledger_path(common, stamp_file)),
                "line": line,
            }))
        else:
            print(line)
        return 0

    if action == "check":
        rev = target or "HEAD"
        result = check_tree(cwd, common, rev, stamp_file=stamp_file)
        if as_json:
            print(json.dumps({
                "v": VERSION,
                "verified": result.verified,
                "rev": rev,
                "tree": result.tree,
                "match": result.match,
                "matched_tree": result.matched_tree,
                "md_paths": list(result.md_paths),
                "ledger": str(result.ledger),
            }))
        elif result.verified:
            if result.match == "md-only" and result.matched_tree is not None:
                shown = ", ".join(result.md_paths[:5]) + (", ..." if len(result.md_paths) > 5 else "")
                print(f"Verified tree {result.tree} matches stamped tree {result.matched_tree} (.md-only: {shown}).")
            else:
                print(f"Verified tree {result.tree} is stamped.")
        else:
            print(f"Tree {result.tree} is not stamped.")
        return 0 if result.verified else 1

    if action == "list":
        entries = read_entries(common, stamp_file=stamp_file)
        if as_json:
            print(json.dumps({
                "v": VERSION,
                "ledger": str(ledger_path(common, stamp_file)),
                "entries": [entry.to_dict() for entry in entries],
            }))
        else:
            for entry in entries:
                print(entry.format_row())
        return 0

    if action in ("abort", "complete"):
        if token is not None and target is not None and token != target:
            raise LaneUsageError(f"{action}: positional token and --token differ")
        chosen_token = token if token is not None else target
        if not chosen_token:
            raise LaneUsageError(f"{action} requires a 32-hex verification stamp token")
        try:
            if action == "abort":
                abort(common, chosen_token, stamp_file=stamp_file)
            else:
                complete(common, chosen_token, stamp_file=stamp_file)
        except StampStateError as error:
            raise LaneError(str(error)) from None
        except ValueError as error:
            raise LaneUsageError(str(error)) from None
        if as_json:
            print(json.dumps({"v": VERSION, "action": action, "token": chosen_token, "ok": True}))
        return 0

    raise LaneUsageError(f"unknown stamp action {action!r}")
