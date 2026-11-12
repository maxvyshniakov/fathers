"""Path claims and the rule snippets a lane's brief carries.

Claims are globs relative to the repository: `*` and `?` stay within a path segment, `**` spans segments, and a
claim without wildcards names one file, or a directory and everything under it (names_directory, lanefather#2).
Two lanes' claims overlap when some path matches both. check adds each path the lane changed outside its claims to
them (#574), and warns only when one is in the claims of another planned or active lane that is not declared in
sequence with it, naming that lane.
"""
from __future__ import annotations

from collections.abc import Iterable
import fnmatch
import functools
import os
from pathlib import Path
import subprocess
from typing import Any

from . import config, journal, state
from .state import Record


def tokens(segment: str) -> tuple[str, ...]:
    result: list[str] = []
    index = 0
    while index < len(segment):
        char = segment[index]
        end = segment.find("]", index + 2) if char == "[" else -1
        if end > 0:
            result.append(segment[index:end + 1])
            index = end + 1
        else:
            result.append(char)
            index += 1
    return tuple(result)


def char_overlap(a: str, b: str) -> bool:
    if "?" in (a, b):
        return True
    candidates = [c for c in (a, b) if len(c) == 1] or [chr(code) for code in range(32, 127)]
    return any(fnmatch.fnmatchcase(c, a) and fnmatch.fnmatchcase(c, b) for c in candidates)


@functools.cache
def segments_overlap(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    if not a or not b:
        return all(t == "*" for t in a + b)
    if a[0] == "*" and (segments_overlap(a[1:], b) or segments_overlap(a, b[1:])):
        return True
    if b[0] == "*" and (segments_overlap(a, b[1:]) or segments_overlap(a[1:], b)):
        return True
    return "*" not in (a[0], b[0]) and char_overlap(a[0], b[0]) and segments_overlap(a[1:], b[1:])


_head_kinds: dict[tuple[str, str], str] = {}


def project_root() -> str:
    """Where claim kinds are asked: the directory of the project's configuration, else the working directory."""
    path = config.current().path
    return str(path.parent) if path else os.getcwd()


def head_kind(path: str) -> str:
    """The object type HEAD of the project has at the repository path (`tree`, `blob`, `commit` for a submodule),
    or "" when HEAD has none or the project is no repository; asked once per path and project in a run."""
    prefetch_kinds([path])
    return _head_kinds.get((project_root(), path.strip("/")), "")


def prefetch_kinds(literals: Iterable[str]) -> None:
    """Ask the project's HEAD for the type at each claim without wildcards in one git call (a lane check widens
    claims by whole file lists), so head_kind answers them from the cache."""
    root = project_root()
    paths = list(dict.fromkeys(c.strip("/") for c in literals if c.strip("/") and "\n" not in c
                               and not any(w in c for w in "*?[") and (root, c.strip("/")) not in _head_kinds))
    if not paths:
        return
    result = subprocess.run(["git", "cat-file", "--batch-check=%(objecttype)"], cwd=root, capture_output=True,
                            text=True, input="".join(f"HEAD:{path}\n" for path in paths))
    answers = result.stdout.splitlines() if result.returncode == 0 else []
    if len(answers) != len(paths):
        answers = [""] * len(paths)  # no repository (or no HEAD) here: HEAD has none of them
    for path, answer in zip(paths, answers, strict=True):
        # A submodule answers `<sha> submodule`; a path HEAD lacks, `HEAD:<path> missing`.
        kind = "commit" if answer.endswith(" submodule") else answer
        _head_kinds[(root, path)] = kind if kind in ("tree", "blob", "commit") else ""


def names_directory(literal: str) -> bool:
    """A claim without wildcards names a directory when it ends in '/' or HEAD has a tree there; one HEAD has as a
    file is that file; a path HEAD does not have names a directory when its last segment has no extension
    (lanefather#2: `desktop_methods.json/**` let `src/**/ledger*.rs` overlap a file through a path no repository
    holds)."""
    path = literal.strip("/")
    if literal.endswith("/") or not path:
        return True
    kind = head_kind(path)
    if kind:
        return kind in ("tree", "commit")  # a commit at HEAD is a submodule, a directory
    return "." not in path.rsplit("/", 1)[-1].lstrip(".")


def glob_segments(claim: str) -> tuple[str, ...]:
    parts = tuple(part for part in claim.strip("/").split("/") if part)
    if any(c in claim for c in "*?["):
        return parts
    return parts + ("**",) if names_directory(claim) else parts


@functools.cache
def paths_overlap(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    if not a or not b:
        return all(part == "**" for part in a + b)
    if a[0] == "**" and (paths_overlap(a[1:], b) or paths_overlap(a, b[1:])):
        return True
    if b[0] == "**" and (paths_overlap(a, b[1:]) or paths_overlap(a[1:], b)):
        return True
    return ("**" not in (a[0], b[0]) and segments_overlap(tokens(a[0]), tokens(b[0]))
            and paths_overlap(a[1:], b[1:]))


def claims_overlap(a: str, b: str) -> bool:
    return paths_overlap(glob_segments(a), glob_segments(b))


def matches(claim: str, path: str) -> bool:
    """A changed path, whose segments carry no wildcards in practice, against a claim."""
    return paths_overlap(glob_segments(claim), tuple(path.split("/")))


def overlapping(claims: Iterable[str], other: Record) -> list[tuple[str, str]]:
    claims = list(claims)
    prefetch_kinds([*claims, *other["claims"]])
    return [(mine, theirs) for mine in claims for theirs in other["claims"] if claims_overlap(mine, theirs)]


def unclaimed(path: str) -> bool:
    """A path every lane may change (release notes merge by union), never claimed."""
    return path.startswith(config.current().unclaimed)


def widen_claims(lane_id: str, paths: list[str]) -> tuple[list[str], list[dict[str, Any]]]:
    """Add the changed paths outside the lane's claims to them (#574): the paths added, and each overlap of one with
    the claims of another planned or active lane. Lanes declared in sequence (--after either way) may overlap."""
    with state.locked("state"):
        record = state.load(lane_id)
        added = [p for p in dict.fromkeys(paths) if not any(matches(c, p) for c in record["claims"])]
        if not added:
            return [], []
        others = [r for r in state.occupying() if r["id"] != lane_id and r["id"] not in record["after"]
                  and lane_id not in r.get("after", [])]
        prefetch_kinds([*record["claims"], *(claim for other in others for claim in other["claims"])])
        overlaps = [{"path": path, "lane": other["id"], "ticket": other["ticket"], "claim": claim}
                    for path in added for other in others for claim in other["claims"] if matches(claim, path)]
        record["claims"] = [*record["claims"], *added]
        state.save(record)
        journal.ledger(f"claims {lane_id} + {' '.join(added)}"
                       + "".join(f" (overlaps {o['lane']}: {o['claim']})" for o in overlaps),
                       kind="claims.added", lane=record,
                       details={"added": added, "overlaps": [{"lane": o["lane"], "claim": o["claim"]}
                                                             for o in overlaps]})
    return added, overlaps


# Rule snippets (the project's rules_dir, *.md with front matter `paths:`): the brief lists those a claim touches.

def read_rules(root: Any) -> list[dict[str, Any]]:
    """Each snippet's name, title, path globs and body; a file without front matter paths is skipped."""
    rules_dir = config.current().rules_dir
    if not rules_dir:
        return []
    rules: list[dict[str, Any]] = []
    for path in sorted((Path(root) / rules_dir).glob("*.md")):
        lines = path.read_text().splitlines()
        if not lines or lines[0].strip() != "---" or "---" not in (line.strip() for line in lines[1:]):
            continue
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
        title, globs, key = path.stem, list[str](), None
        for line in lines[1:end]:
            stripped = line.strip()
            if stripped.startswith("- ") and key == "paths":
                globs.append(stripped[2:].strip())
            elif ":" in stripped:
                key, _, value = (part.strip() for part in stripped.partition(":"))
                if key == "title" and value:
                    title = value
        body = "\n".join(lines[end + 1:]).strip()
        if globs and body:
            rules.append({"name": path.stem, "title": title, "paths": globs, "body": body})
    return rules


def rules_for(claims: Iterable[str], root: Any) -> list[dict[str, Any]]:
    """The snippets with a glob some claim overlaps, each with the globs that matched."""
    claims = list(claims)
    selected: list[dict[str, Any]] = []
    for rule in read_rules(root):
        hit = [glob for glob in rule["paths"] if any(claims_overlap(claim, glob) for claim in claims)]
        if hit:
            selected.append(dict(rule, matched=hit))
    return selected


def rules_block(rules: list[dict[str, Any]]) -> list[str]:
    rules_dir = config.current().rules_dir or "no rules_dir in .lanefather.toml"
    if not rules:
        return [f"Rules for these claims: none ({rules_dir}/)"]
    lines = [f"Rules for these claims ({rules_dir}/):"]
    for rule in rules:
        lines.append(f"{rule['title']} ({', '.join(rule['matched'])}):")
        lines.extend("  " + line if line else "" for line in rule["body"].splitlines())
    return lines
