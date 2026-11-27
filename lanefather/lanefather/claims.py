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
_merge_attrs: dict[tuple[str, str], str] = {}


def project_root(project: config.Config = config.DEFAULT, fallback: str | Path | None = None) -> str:
    """Where claim kinds are asked: the directory of the project's configuration, else fallback or the working
    directory."""
    if project.path is not None:
        return str(project.path.parent)
    return str(fallback) if fallback is not None else os.getcwd()


def prefetch_merge_attrs(literals: Iterable[str], *, root: str | None = None,
                         project: config.Config = config.DEFAULT) -> None:
    """Ask git check-attr for the `merge` attribute of each literal path in one call; fail closed to '' on errors."""
    where = root or project_root(project)
    paths = list(dict.fromkeys(
        c.strip("/") for c in literals
        if c.strip("/") and not c.endswith("/") and "\n" not in c and "\x00" not in c
        and not any(w in c for w in "*?[") and (where, c.strip("/")) not in _merge_attrs
    ))
    if not paths:
        return
    payload = b"\x00".join(p.encode("utf-8", errors="surrogateescape") for p in paths) + b"\x00"
    try:
        result = subprocess.run(
            ["git", "check-attr", "-z", "--stdin", "merge"],
            cwd=where, capture_output=True, input=payload,
        )
    except OSError:
        for path in paths:
            _merge_attrs[(where, path)] = ""
        return
    if result.returncode != 0:
        for path in paths:
            _merge_attrs[(where, path)] = ""
        return
    parts = result.stdout.split(b"\x00")
    if parts and parts[-1] == b"":
        parts = parts[:-1]
    if len(parts) != 3 * len(paths):
        for path in paths:
            _merge_attrs[(where, path)] = ""
        return
    for i in range(0, len(parts), 3):
        raw_path = parts[i].decode("utf-8", errors="surrogateescape")
        value = parts[i + 2].decode("utf-8", errors="surrogateescape")
        _merge_attrs[(where, raw_path)] = "" if value in ("unspecified", "unset", "set") else value


def merge_attr(path: str, *, root: str | None = None, project: config.Config = config.DEFAULT) -> str:
    """The git `merge` attribute for a literal repository path, or '' when unset or outside a git repo."""
    if path.endswith("/") or any(w in path for w in "*?["):
        return ""
    norm = path.strip("/")
    if not norm:
        return ""
    where = root or project_root(project)
    prefetch_merge_attrs([norm], root=where, project=project)
    return _merge_attrs.get((where, norm), "")


def head_kind(path: str, *, root: str | None = None, project: config.Config = config.DEFAULT) -> str:
    """The object type HEAD of the project has at the repository path (`tree`, `blob`, `commit` for a submodule),
    or "" when HEAD has none or the project is no repository; asked once per path and project in a run."""
    where = root or project_root(project)
    prefetch_kinds([path], root=where)
    return _head_kinds.get((where, path.strip("/")), "")


def prefetch_kinds(literals: Iterable[str], *, root: str | None = None,
                   project: config.Config = config.DEFAULT) -> None:
    """Ask the project's HEAD for the type at each claim without wildcards in one git call (a lane check widens
    claims by whole file lists), so head_kind answers them from the cache."""
    where = root or project_root(project)
    paths = list(dict.fromkeys(c.strip("/") for c in literals if c.strip("/") and "\n" not in c
                               and not any(w in c for w in "*?[") and (where, c.strip("/")) not in _head_kinds))
    if not paths:
        return
    result = subprocess.run(["git", "cat-file", "--batch-check=%(objecttype)"], cwd=where, capture_output=True,
                            text=True, input="".join(f"HEAD:{path}\n" for path in paths))
    answers = result.stdout.splitlines() if result.returncode == 0 else []
    if len(answers) != len(paths):
        answers = [""] * len(paths)  # no repository (or no HEAD) here: HEAD has none of them
    for path, answer in zip(paths, answers, strict=True):
        # A submodule answers `<sha> submodule` on Git >= 2.45; older Git answers `HEAD:<path> missing`.
        kind = "commit" if answer.endswith(" submodule") else answer
        _head_kinds[(where, path)] = kind if kind in ("tree", "blob", "commit") else ""
    missing = [path for path, answer in zip(paths, answers, strict=True) if answer.endswith(" missing")]
    if missing:
        ls = subprocess.run(["git", "ls-tree", "-z", "HEAD", "--", *missing], cwd=where, capture_output=True)
        if ls.returncode == 0:
            for entry in ls.stdout.split(b"\x00"):
                if b"\t" not in entry:
                    continue
                meta, raw_path = entry.split(b"\t", 1)
                meta_parts = meta.decode("utf-8", errors="replace").split()
                if len(meta_parts) >= 2 and meta_parts[1] in ("tree", "blob", "commit"):
                    _head_kinds[(where, raw_path.decode("utf-8", errors="surrogateescape"))] = meta_parts[1]


def names_directory(literal: str, *, root: str | None = None, project: config.Config = config.DEFAULT) -> bool:
    """A claim without wildcards names a directory when it ends in '/' or HEAD has a tree there; one HEAD has as a
    file is that file; a path HEAD does not have names a directory when its last segment has no extension
    (lanefather#2: `desktop_methods.json/**` let `src/**/ledger*.rs` overlap a file through a path no repository
    holds)."""
    path = literal.strip("/")
    if literal.endswith("/") or not path:
        return True
    kind = head_kind(path, root=root, project=project)
    if kind:
        return kind in ("tree", "commit")  # a commit at HEAD is a submodule, a directory
    return "." not in path.rsplit("/", 1)[-1].lstrip(".")


def glob_segments(claim: str, *, root: str | None = None,
                  project: config.Config = config.DEFAULT) -> tuple[str, ...]:
    parts = tuple(part for part in claim.strip("/").split("/") if part)
    if any(c in claim for c in "*?["):
        return parts
    return parts + ("**",) if names_directory(claim, root=root, project=project) else parts


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


def claims_overlap(a: str, b: str, *, root: str | None = None,
                   project: config.Config = config.DEFAULT) -> bool:
    return paths_overlap(glob_segments(a, root=root, project=project),
                         glob_segments(b, root=root, project=project))


def matches(claim: str, path: str, *, root: str | None = None,
            project: config.Config = config.DEFAULT) -> bool:
    """A changed path, whose segments carry no wildcards in practice, against a claim."""
    return paths_overlap(glob_segments(claim, root=root, project=project), tuple(path.split("/")))


def overlapping(claims: Iterable[str], other: Record, *, root: str | None = None,
                project: config.Config = config.DEFAULT) -> list[tuple[str, str]]:
    where = root or project_root(project)
    claims_list = list(claims)
    all_claims = [*claims_list, *other["claims"]]
    prefetch_kinds(all_claims, root=where, project=project)
    prefetch_merge_attrs(all_claims, root=where, project=project)
    return [
        (mine, theirs)
        for mine in claims_list
        for theirs in other["claims"]
        if not claim_is_unclaimed(mine, root=where, project=project)
        and not claim_is_unclaimed(theirs, root=where, project=project)
        and claims_overlap(mine, theirs, root=where, project=project)
    ]


def unclaimed(path: str, project: config.Config = config.DEFAULT, *, root: str | None = None) -> bool:
    """A path every lane may change (configured unclaimed prefix/glob or git `merge=union`), never claimed."""
    if path.startswith(project.unclaimed):
        return True
    where = root or project_root(project)
    if any(matches(pat, path, root=where, project=project) for pat in project.unclaimed):
        return True
    if not any(w in path for w in "*?[") and not names_directory(path, root=where, project=project):
        return merge_attr(path, root=where, project=project) == "union"
    return False


def claim_is_unclaimed(claim: str, *, root: str | None = None,
                       project: config.Config = config.DEFAULT) -> bool:
    """Whether a lane claim names only an unclaimed path (never exempting broad globs or directories)."""
    where = root or project_root(project)
    if not any(w in claim for w in "*?[") and not names_directory(claim, root=where, project=project):
        return unclaimed(claim, project, root=where)
    return claim.startswith(project.unclaimed) or claim in project.unclaimed


def widen_claims(store: state.Store, lane_id: str,
                 paths: list[str]) -> tuple[list[str], list[dict[str, Any]]]:
    """Add the changed paths outside the lane's claims to them (#574): the paths added, and each overlap of one with
    the claims of another planned or active lane. Lanes declared in sequence (--after either way) may overlap."""
    where = project_root(store.project)
    prefetch_kinds(paths, root=where, project=store.project)
    prefetch_merge_attrs(paths, root=where, project=store.project)
    added: list[str] = []
    overlaps: list[dict[str, Any]] = []

    def _widen(record: Record) -> bool:
        nonlocal added, overlaps
        added = [
            p for p in dict.fromkeys(paths)
            if not unclaimed(p, store.project, root=where)
            and not any(matches(c, p, root=where, project=store.project) for c in record["claims"])
        ]
        if not added:
            return False
        others = [r for r in state.occupying(state.of_project(store.lanes(), record.get("common_dir")))
                  if r["id"] != lane_id and r["id"] not in record["after"]
                  and lane_id not in r.get("after", [])]
        prefetch_kinds([*record["claims"], *(claim for other in others for claim in other["claims"])], root=where)
        overlaps = [{"path": path, "lane": other["id"], "ticket": other["ticket"], "claim": claim}
                    for path in added for other in others for claim in other["claims"]
                    if matches(claim, path, root=where)]
        record["claims"] = [*record["claims"], *added]
        return True

    with store.locked("state"):
        record = store.update(lane_id, _widen)
        if added:
            journal.ledger(f"claims {lane_id} + {' '.join(added)}"
                           + "".join(f" (overlaps {o['lane']}: {o['claim']})" for o in overlaps),
                           kind="claims.added", lane=record,
                           details={"added": added, "overlaps": [{"lane": o["lane"], "claim": o["claim"]}
                                                                 for o in overlaps]},
                           store=store)
    return added, overlaps


# Rule snippets (the project's rules_dir, *.md with front matter `paths:`): the brief lists those a claim touches.

def read_rules(root: Any, project: config.Config = config.DEFAULT) -> list[dict[str, Any]]:
    """Each snippet's name, title, path globs and body; a file without front matter paths is skipped."""
    rules_dir = project.rules_dir
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


def rules_for(claims: Iterable[str], root: Any,
              project: config.Config = config.DEFAULT) -> list[dict[str, Any]]:
    """The snippets with a glob some claim overlaps, each with the globs that matched."""
    claims_list = list(claims)
    where = project_root(project, fallback=root)
    selected: list[dict[str, Any]] = []
    for rule in read_rules(root, project):
        hit = [glob for glob in rule["paths"] if any(claims_overlap(claim, glob, root=where) for claim in claims_list)]
        if hit:
            selected.append(dict(rule, matched=hit))
    return selected


def rules_block(rules: list[dict[str, Any]], project: config.Config = config.DEFAULT) -> list[str]:
    rules_dir = project.rules_dir or "no rules_dir in .lanefather.toml"
    if not rules:
        return [f"Rules for these claims: none ({rules_dir}/)"]
    lines = [f"Rules for these claims ({rules_dir}/):"]
    for rule in rules:
        lines.append(f"{rule['title']} ({', '.join(rule['matched'])}):")
        lines.extend("  " + line if line else "" for line in rule["body"].splitlines())
    return lines
