"""Risk-based review classes decided at lane start and checked at land (fathers#252).

Classes form a strict order: `none < evidence < full < double`.
- `none`: no independent review (docs, stories, test-only, CI/tooling outside package, generated files).
- `evidence`: no independent review when the lane carries BOTH an owner decision (a `--decision` comment URL in scope
  or `--review-decision` quote+date) AND a guarding gate/check/test (`--gate`).
- `full`: standard single-reviewer (or configured `[review] panel`) review.
- `double`: two-reviewer panel review for risky surfaces.

Project rules live under `[review.classes]` in `.lanefather.toml`. When no rules are configured, existing behaviour
(`docs_only` and `--fix-of` exemptions) is preserved.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import hashlib
import subprocess
from typing import Any, cast

from . import claims, config, scope
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


@dataclass(frozen=True)
class ClassRequirement:
    """Minimum review class required for a diff under project rules."""
    min_class: str
    reason: str
    steering_touched: bool = False


def steering_globs(project: config.Config = config.DEFAULT) -> tuple[str, ...]:
    return project.steering + ((project.rules_dir.rstrip("/") + "/**",) if project.rules_dir else ())


def documentation_only(paths: Sequence[str], project: config.Config = config.DEFAULT) -> bool:
    """Whether a merge changes only documentation, *.md and docs/**, and none of the steering files."""
    globs = steering_globs(project)
    return all(
        (p.endswith(".md") or p.startswith("docs/")) and not any(claims.matches(s, p, project=project) for s in globs)
        for p in paths
    )


def diff_stats(cwd: str, base: str, tip: str) -> DiffStats:
    """Compute changed paths, file count, and total added+deleted lines using `git diff --numstat -z`."""
    proc = subprocess.run(
        ["git", "-c", "core.quotePath=false", "diff", "--no-renames", "--numstat", "-z", base, tip, "--"],
        cwd=cwd,
        capture_output=True,
    )
    if proc.returncode != 0 or not proc.stdout:
        return DiffStats(paths=(), files=0, lines=0)
    paths: list[str] = []
    total_lines = 0
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
        if added_b.isdigit():
            total_lines += int(added_b)
        if deleted_b.isdigit():
            total_lines += int(deleted_b)
    return DiffStats(paths=tuple(paths), files=len(paths), lines=total_lines)


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


def evidence_problem(record: Record) -> str | None:
    """Validate that an `evidence` lane carries both an owner decision and a guarding gate/check/test."""
    decision, gate = evidence_of(record)
    missing: list[str] = []
    if not decision:
        missing.append("an owner decision (--decision URL or --review-decision '<quote/date>')")
    if not gate:
        missing.append("a guarding gate/check/test (--gate <name>)")
    if not missing:
        return None
    return (
        f"lane {record['id']} has review class 'evidence' but is missing "
        + " and ".join(missing)
    )


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


def required_class(
    paths: Sequence[str],
    *,
    lines: int,
    files: int,
    project: config.Config = config.DEFAULT,
    root: str | None = None,
) -> ClassRequirement:
    """Compute the minimum review class required for `paths` and diff size under `[review.classes]`."""
    rules = project.review_classes
    steer = steering_globs(project)
    if not paths:
        return ClassRequirement("none", "no changed files")

    steering_hit = any(
        any(claims.matches(s, p, root=root, project=project) for s in steer)
        for p in paths
    )

    if not rules.has_rules():
        if documentation_only(paths, project):
            return ClassRequirement("none", "documentation only (*.md and docs/** outside steering files)")
        reason = "touches steering files" if steering_hit else "changes source files"
        return ClassRequirement("full", reason, steering_touched=steering_hit)

    best_class = "none"
    best_reason = "all paths match [review.classes] none"

    def raise_to(cls: str, reason: str) -> None:
        nonlocal best_class, best_reason
        if CLASS_RANK[cls] > CLASS_RANK[best_class]:
            best_class = cls
            best_reason = reason

    for path in paths:
        double_match = next((g for g in rules.double if claims.matches(g, path, root=root, project=project)), None)
        if double_match is not None:
            raise_to("double", f"{path} matches [review.classes] double ({double_match})")
            continue
        full_match = next((g for g in rules.full if claims.matches(g, path, root=root, project=project)), None)
        if full_match is not None:
            raise_to("full", f"{path} matches [review.classes] full ({full_match})")
            continue
        if any(claims.matches(s, path, root=root, project=project) for s in steer):
            raise_to("full", f"{path} is a steering file")
            continue
        ev_match = next((g for g in rules.evidence if claims.matches(g, path, root=root, project=project)), None)
        if ev_match is not None:
            raise_to("evidence", f"{path} matches [review.classes] evidence ({ev_match})")
            continue
        none_match = next((g for g in rules.none if claims.matches(g, path, root=root, project=project)), None)
        if none_match is not None:
            continue
        if not rules.none and documentation_only([path], project):
            continue
        raise_to("full", f"{path} matches no [review.classes] none/evidence glob")

    if rules.max_lines is not None and lines > rules.max_lines:
        raise_to("full", f"diff has {lines} changed lines, above [review.classes] max_lines = {rules.max_lines}")
    if rules.max_files is not None and files > rules.max_files:
        raise_to("full", f"diff changes {files} files, above [review.classes] max_files = {rules.max_files}")

    return ClassRequirement(best_class, best_reason, steering_touched=steering_hit)


def land_problems(
    record: Record,
    stats: DiffStats,
    project: config.Config = config.DEFAULT,
    *,
    root: str | None = None,
    is_fix_lane: bool = False,
) -> list[str]:
    """Refusal reasons at `land` when the lane's declared review class is lower than computed or lacks evidence."""
    if is_fix_lane:
        return []
    declared = declared_class(record)
    if declared is None:
        return []
    req = required_class(stats.paths, lines=stats.lines, files=stats.files, project=project, root=root)
    problems: list[str] = []
    # When [review.classes] has no rules configured, an explicit `evidence` lane is allowed on non-steering changes
    # provided it carries both required evidence pieces.
    allow_unconfigured_evidence = (
        declared == "evidence"
        and not project.review_classes.has_rules()
        and not req.steering_touched
    )
    if not allow_unconfigured_evidence and CLASS_RANK[declared] < CLASS_RANK[req.min_class]:
        problems.append(
            f"declared review class {declared!r} is lower than required {req.min_class!r} ({req.reason})"
        )
    if declared == "evidence":
        ev_err = evidence_problem(record)
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
) -> tuple[str, str]:
    """Effective `(review_class, reason)` recorded on `record['landed']` after `land`."""
    if is_fix_lane:
        return "none", "fix lane"
    declared = declared_class(record)
    req = required_class(stats.paths, lines=stats.lines, files=stats.files, project=project, root=root)
    raw_reason = record.get("review_reason")
    user_reason = raw_reason.strip() if isinstance(raw_reason, str) and raw_reason.strip() else ""
    if declared is not None:
        if declared == "evidence":
            return "evidence", evidence_summary(record)
        if declared == "none":
            return "none", user_reason or req.reason
        return declared, user_reason or req.reason
    return req.min_class, req.reason


def should_audit(lane_id: str, merge_sha: str, audit_every: int) -> bool:
    """Deterministic 1-in-N post-hoc audit sampling for `none`/`evidence` lanes (fathers#252)."""
    if audit_every <= 0:
        return False
    if audit_every == 1:
        return True
    digest = hashlib.sha256(f"{lane_id}:{merge_sha}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % audit_every == 0


def audit_label(lane_id: str) -> str:
    return f"audit-{lane_id}"
