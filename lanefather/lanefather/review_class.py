"""Risk-based review classes decided at lane start and checked at land (fathers#252).

Classes form a strict order: `none < evidence < full < double`.
- `none`: no independent review (docs, stories, test-only, CI/tooling outside package, generated files).
- `evidence`: no independent review when the lane carries BOTH an owner decision (a `--decision` comment URL in scope
  or `--review-decision` quote+date) AND a guarding gate/check/test (`--gate`) that passed in the lane's check receipt.
- `full`: standard single-reviewer (or configured `[review] panel`) review.
- `double`: two-reviewer panel review for risky surfaces (requires >= 2 distinct finished reviewers).

Project rules live under `[review.classes]` in `.lanefather.toml`. When no rules are configured, existing behaviour
(`docs_only` and `--fix-of` exemptions) is preserved.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import fnmatch
import hashlib
import subprocess
from typing import Any, cast

from . import claims, config, scope
from .errors import LaneError
from .state import Record

REVIEW_CLASSES: tuple[str, ...] = ("none", "evidence", "full", "double")
CLASS_RANK: dict[str, int] = {name: rank for rank, name in enumerate(REVIEW_CLASSES)}
AUDIT_SEVERITIES: tuple[str, ...] = ("P0", "P1")


@dataclass(frozen=True)
class DiffStats:
    """Changed paths and diff size between two commits."""
    paths: tuple[str, ...]
    files: int
    lines: int
    binary_files: int = 0


@dataclass(frozen=True)
class ClassRequirement:
    """Minimum review class required for a diff under project rules."""
    min_class: str
    reason: str
    steering_touched: bool = False
    size_exceeded: bool = False


def blocks_gate(severity: Any, review_blocks: Sequence[str]) -> bool:
    """Whether a finding severity holds lands, drops, batch gate, or promote (`P0` always blocks)."""
    return isinstance(severity, str) and (severity == "P0" or severity in review_blocks)


def steering_globs(project: config.Config = config.DEFAULT) -> tuple[str, ...]:
    return project.steering + ((project.rules_dir.rstrip("/") + "/**",) if project.rules_dir else ())


def _match_literal_segments(pattern: tuple[str, ...], path_parts: tuple[str, ...]) -> bool:
    """Match a glob's segments against a literal repository path's segments (wildcards in `path_parts` are literal)."""
    if not pattern:
        return not path_parts
    if pattern[0] == "**":
        if _match_literal_segments(pattern[1:], path_parts):
            return True
        return bool(path_parts) and _match_literal_segments(pattern, path_parts[1:])
    if not path_parts:
        return False
    return fnmatch.fnmatchcase(path_parts[0], pattern[0]) and _match_literal_segments(pattern[1:], path_parts[1:])


def path_matches_glob(glob: str, path: str, *, root: str | None = None,
                      project: config.Config = config.DEFAULT) -> bool:
    """Match a changed repository path literally against a config glob."""
    pattern = claims.glob_segments(glob, root=root, project=project)
    parts = tuple(part for part in path.strip("/").split("/") if part)
    return _match_literal_segments(pattern, parts)


def documentation_only(paths: Sequence[str], project: config.Config = config.DEFAULT, *,
                       root: str | None = None) -> bool:
    """Whether a merge changes only documentation, *.md and docs/**, and none of the steering files."""
    globs = steering_globs(project)
    return all(
        (p.endswith(".md") or p.startswith("docs/"))
        and not any(path_matches_glob(s, p, root=root, project=project) for s in globs)
        for p in paths
    )


def diff_stats(cwd: str, base: str, tip: str) -> DiffStats:
    """Compute changed paths, file count, line count, and binary file count via `git diff --numstat -z`.

    Fails closed by raising `LaneError` if git diff fails.
    """
    proc = subprocess.run(
        ["git", "-c", "core.quotePath=false", "diff", "--no-renames", "--numstat", "-z", base, tip, "--"],
        cwd=cwd,
        capture_output=True,
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        raise LaneError(f"git diff failed for {base[:10]}..{tip[:10]}: {err or f'exit {proc.returncode}'}")
    if not proc.stdout:
        return DiffStats(paths=(), files=0, lines=0, binary_files=0)
    paths: list[str] = []
    total_lines = 0
    binary_files = 0
    for entry in proc.stdout.split(b"\0"):
        if not entry:
            continue
        parts = entry.split(b"\t", 2)
        if len(parts) != 3:
            continue
        added_b, deleted_b, path_b = parts
        path = path_b.decode("utf-8", errors="replace")
        if not path:
            continue
        paths.append(path)
        if added_b == b"-" or deleted_b == b"-":
            binary_files += 1
        else:
            if added_b.isdigit():
                total_lines += int(added_b)
            if deleted_b.isdigit():
                total_lines += int(deleted_b)
    return DiffStats(paths=tuple(paths), files=len(paths), lines=total_lines, binary_files=binary_files)


def declared_class(record: Record) -> str | None:
    """The lane's declared review class from `start --review`, or None when unset."""
    raw = record.get("review_class")
    if isinstance(raw, str) and raw in CLASS_RANK:
        return raw
    return None


def evidence_of(record: Record) -> tuple[str | None, str | None]:
    """Return `(owner_decision, guarding_gate)` recorded on the lane."""
    decision: str | None = None
    gate: str | None = None
    raw_ev: Any = record.get("review_evidence")
    if isinstance(raw_ev, dict):
        ev = cast(dict[str, Any], raw_ev)
        raw_dec = ev.get("decision")
        if isinstance(raw_dec, str) and raw_dec.strip():
            decision = raw_dec.strip()
        raw_gate = ev.get("gate")
        if isinstance(raw_gate, str) and raw_gate.strip():
            gate = raw_gate.strip()
    if decision is None:
        for item in scope.of(record):
            if isinstance(item, scope.Decision):
                decision = item.url
                break
    return decision, gate


def gate_passed_in_receipt(gate: str, receipt: dict[str, Any]) -> bool:
    """Whether `gate` names a check id or suite that passed in `receipt`."""
    raw_checks: Any = receipt.get("check_results")
    if isinstance(raw_checks, dict):
        entry = cast(dict[str, Any], raw_checks).get(gate)
        if isinstance(entry, dict) and cast(dict[str, Any], entry).get("status") == "passed":
            return True
    raw_results: Any = receipt.get("results")
    if isinstance(raw_results, list):
        for item in cast(list[Any], raw_results):
            if not isinstance(item, dict):
                continue
            row = cast(dict[str, Any], item)
            if row.get("suite") == gate and row.get("status") == "passed":
                return True
    return False


def evidence_problem(record: Record, *, receipt: dict[str, Any] | None = None) -> str | None:
    """Validate that an `evidence` lane carries both an owner decision and a guarding gate/check/test.

    When `receipt` is provided, also verify that the named gate passed in the lane's check receipt.
    """
    decision, gate = evidence_of(record)
    missing: list[str] = []
    if not decision:
        missing.append("an owner decision (--decision URL or --review-decision '<quote/date>')")
    if not gate:
        missing.append("a guarding gate/check/test (--gate <name>)")
    if missing:
        return (
            f"lane {record['id']} has review class 'evidence' but is missing "
            + " and ".join(missing)
        )
    assert gate is not None
    if receipt is not None and not gate_passed_in_receipt(gate, receipt):
        return (
            f"lane {record['id']} names --gate {gate!r}, which did not pass in its check receipt"
        )
    return None


def evidence_summary(record: Record) -> str:
    """Human-readable summary of an `evidence` lane's owner decision and guarding gate."""
    decision, gate = evidence_of(record)
    parts: list[str] = []
    if decision:
        parts.append(f"decision: {decision}")
    if gate:
        parts.append(f"gate: {gate}")
    raw_reason = record.get("review_reason")
    if isinstance(raw_reason, str) and raw_reason.strip():
        parts.append(raw_reason.strip())
    return "; ".join(parts) if parts else "owner decision and guarding gate"


def distinct_finished_reviewers(raw: dict[str, Any] | None) -> list[str]:
    """Distinct non-empty reviewer aliases that finished in a review receipt."""
    if not isinstance(raw, dict) or raw.get("status") != "finished":
        return []
    if raw.get("recovered") is True or raw.get("evidence_source") == "historical":
        return []
    raw_panel: Any = raw.get("panel")
    if isinstance(raw_panel, list) and raw_panel:
        found: list[str] = []
        for item in cast(list[Any], raw_panel):
            if not isinstance(item, dict):
                continue
            entry = cast(dict[str, Any], item)
            if entry.get("status") != "finished":
                continue
            name = entry.get("reviewer") or entry.get("member")
            if isinstance(name, str) and name.strip() and name.strip() not in found:
                found.append(name.strip())
        return found
    raw_rev = raw.get("reviewer")
    if isinstance(raw_rev, str) and raw_rev.strip():
        return [raw_rev.strip()]
    return []


def double_review_problem(raw: dict[str, Any] | None, *, label: str, prog: str = "lanefather") -> str | None:
    """Verify a `double`-class review receipt has at least 2 distinct finished reviewers."""
    reviewers = distinct_finished_reviewers(raw)
    if len(reviewers) >= 2:
        return None
    got = ", ".join(reviewers) if reviewers else "none"
    return (
        f"review class 'double' requires at least 2 distinct finished reviewers (got {len(reviewers)}: {got}); "
        f"run {prog} review {label} --reviewer both"
    )


def required_class(
    paths: Sequence[str],
    *,
    lines: int,
    files: int,
    binary_files: int = 0,
    project: config.Config = config.DEFAULT,
    root: str | None = None,
) -> ClassRequirement:
    """Compute the minimum review class required for `paths` and diff size under `[review.classes]`."""
    rules = project.review_classes
    steer = steering_globs(project)
    if not paths:
        return ClassRequirement("none", "no changed files")

    steering_hit = any(
        any(path_matches_glob(s, p, root=root, project=project) for s in steer)
        for p in paths
    )

    if not rules.has_rules():
        if documentation_only(paths, project, root=root):
            return ClassRequirement("none", "documentation only (*.md and docs/** outside steering files)")
        reason = "touches steering files" if steering_hit else "changes source files"
        return ClassRequirement("full", reason, steering_touched=steering_hit)

    best_class = "none"
    best_reason = "all paths match [review.classes] none"
    size_exceeded = False

    def raise_to(cls: str, reason: str) -> None:
        nonlocal best_class, best_reason
        if CLASS_RANK[cls] > CLASS_RANK[best_class]:
            best_class = cls
            best_reason = reason

    for path in paths:
        double_match = next((g for g in rules.double if path_matches_glob(g, path, root=root, project=project)), None)
        if double_match is not None:
            raise_to("double", f"{path} matches [review.classes] double ({double_match})")
            continue
        if any(path_matches_glob(s, path, root=root, project=project) for s in steer):
            raise_to("full", f"{path} is a steering file")
            continue
        ev_match = next((g for g in rules.evidence if path_matches_glob(g, path, root=root, project=project)), None)
        if ev_match is not None:
            raise_to("evidence", f"{path} matches [review.classes] evidence ({ev_match})")
            continue
        none_match = next((g for g in rules.none if path_matches_glob(g, path, root=root, project=project)), None)
        if none_match is not None:
            continue
        full_match = next((g for g in rules.full if path_matches_glob(g, path, root=root, project=project)), None)
        if full_match is not None:
            raise_to("full", f"{path} matches [review.classes] full ({full_match})")
            continue
        if not rules.none and documentation_only([path], project, root=root):
            continue
        raise_to("full", f"{path} matches no [review.classes] none/evidence glob")

    if rules.max_lines is not None and binary_files > 0:
        size_exceeded = True
        raise_to("full", f"diff changes {binary_files} binary file(s) with [review.classes] max_lines set")
    if rules.max_lines is not None and lines > rules.max_lines:
        size_exceeded = True
        raise_to("full", f"diff has {lines} changed lines, above [review.classes] max_lines = {rules.max_lines}")
    if rules.max_files is not None and files > rules.max_files:
        size_exceeded = True
        raise_to("full", f"diff changes {files} files, above [review.classes] max_files = {rules.max_files}")

    return ClassRequirement(best_class, best_reason, steering_touched=steering_hit, size_exceeded=size_exceeded)


def _evidence_allowed_on_broad_full(declared: str | None, req: ClassRequirement, project: config.Config) -> bool:
    """When `[review.classes]` defines no explicit `evidence` globs, an explicit `--review evidence` lane is allowed
    on non-double, non-steering diffs within size bounds."""
    return (
        declared == "evidence"
        and not project.review_classes.evidence
        and req.min_class != "double"
        and not req.steering_touched
        and not req.size_exceeded
    )


def land_problems(
    record: Record,
    stats: DiffStats,
    project: config.Config = config.DEFAULT,
    *,
    root: str | None = None,
    is_fix_lane: bool = False,
    receipt: dict[str, Any] | None = None,
) -> list[str]:
    """Refusal reasons at `land` when the lane's declared review class is lower than computed or lacks evidence."""
    if is_fix_lane:
        return []
    declared = declared_class(record)
    if declared is None:
        return []
    req = required_class(
        stats.paths, lines=stats.lines, files=stats.files, binary_files=stats.binary_files,
        project=project, root=root,
    )
    problems: list[str] = []
    if not _evidence_allowed_on_broad_full(declared, req, project) and CLASS_RANK[declared] < CLASS_RANK[req.min_class]:
        problems.append(
            f"declared review class {declared!r} is lower than required {req.min_class!r} ({req.reason})"
        )
    if declared == "evidence":
        ev_err = evidence_problem(record, receipt=receipt)
        if ev_err:
            problems.append(ev_err)
    return problems


def effective_class(
    record: Record,
    stats: DiffStats,
    project: config.Config = config.DEFAULT,
    *,
    root: str | None = None,
    is_fix_lane: bool = False,
    receipt: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Effective `(review_class, reason)` recorded on `record['landed']` after `land`.

    Never lower than computed `required_class` (so `land --force-reason` waives the land refusal only, never the
    required review), and never auto-assigns `evidence` to an undeclared lane.
    """
    if is_fix_lane:
        return "none", "fix lane"
    declared = declared_class(record)
    req = required_class(
        stats.paths, lines=stats.lines, files=stats.files, binary_files=stats.binary_files,
        project=project, root=root,
    )
    raw_reason = record.get("review_reason")
    user_reason = raw_reason.strip() if isinstance(raw_reason, str) and raw_reason.strip() else ""
    if declared is None:
        if req.min_class == "evidence":
            return "full", f"{req.reason}; undeclared lane requires full review"
        return req.min_class, req.reason
    if declared == "evidence":
        allowed = (
            _evidence_allowed_on_broad_full(declared, req, project)
            or CLASS_RANK["evidence"] >= CLASS_RANK[req.min_class]
        )
        if allowed and evidence_problem(record, receipt=receipt) is None:
            return "evidence", evidence_summary(record)
        target = req.min_class if CLASS_RANK[req.min_class] >= CLASS_RANK["full"] else "full"
        why = evidence_problem(record, receipt=receipt) or req.reason
        return target, why
    if CLASS_RANK[declared] < CLASS_RANK[req.min_class]:
        return req.min_class, req.reason
    return declared, user_reason or req.reason


def should_audit(lane_id: str, merge_sha: str, audit_every: int) -> bool:
    """Deterministic 1-in-N post-hoc audit sampling for `none`/`evidence` lanes (fathers#252)."""
    if audit_every <= 0:
        return False
    if audit_every == 1:
        return True
    digest = hashlib.sha256(f"{lane_id}:{merge_sha}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % audit_every == 0


def audit_label(lane_id: str) -> str:
    """Receipt label for a post-hoc audit review; cannot collide with any valid lane id."""
    return f"_audit.{lane_id}"
