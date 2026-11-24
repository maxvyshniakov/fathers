"""Review receipt & fix-round domain model and state machine (fathers#216).

Owns `<state>/<label>.review.json` transitions, the 8 review verdicts (separating `PartialPanel` from hard `Failed`),
stale review worker fencing (`StaleReviewRunError`), and authoritative single-location `pre_land_fix` storage with a
guarded legacy read fallback on `<id>.json`. Imports only layer-1 modules so `receipts` and `history` import it with
no cycle and no ambient `config.current()` / `state.state_dir()`.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Sequence
import contextlib
from dataclasses import dataclass
from enum import StrEnum
import os
from pathlib import Path
import re
import subprocess
from typing import Any, cast

from . import gitops, journal, lifecycle, state
from .errors import LaneError
from .gitops import git_ok, main_checkout
from .state import Record

Receipt = dict[str, Any]
SEVERITIES: tuple[str, ...] = ("P1", "P2", "P3")
OID_RE = re.compile(r"^[0-9a-f]{40,64}$")


class ReceiptPhase(StrEnum):
    """On-disk lifecycle phase of `<label>.review.json`."""

    ABSENT = "absent"
    RUNNING = "running"
    DIED = "died"
    FAILED = "failed"
    EXEMPT = "exempt"
    FINISHED = "finished"


class ReviewEvent(StrEnum):
    """State-machine transitions permitted on `<label>.review.json`."""

    START = "start"
    FINISH = "finish"
    FAIL = "fail"
    EXEMPT = "exempt"
    RECORD = "record"
    MARK = "mark"
    PRE_LAND_FIX = "pre_land_fix"
    FIX_LANE = "fix_lane"
    TICKET = "ticket"
    DELTA_START = "delta_start"
    DELTA_FINISH = "delta_finish"
    DELTA_FAIL = "delta_fail"


REVIEW_TRANSITIONS: dict[tuple[ReceiptPhase, ReviewEvent], ReceiptPhase] = {
    (ReceiptPhase.ABSENT, ReviewEvent.START): ReceiptPhase.RUNNING,
    (ReceiptPhase.DIED, ReviewEvent.START): ReceiptPhase.RUNNING,
    (ReceiptPhase.FAILED, ReviewEvent.START): ReceiptPhase.RUNNING,
    (ReceiptPhase.RUNNING, ReviewEvent.FINISH): ReceiptPhase.FINISHED,
    (ReceiptPhase.RUNNING, ReviewEvent.FAIL): ReceiptPhase.FAILED,
    (ReceiptPhase.ABSENT, ReviewEvent.EXEMPT): ReceiptPhase.EXEMPT,
    (ReceiptPhase.DIED, ReviewEvent.EXEMPT): ReceiptPhase.EXEMPT,
    (ReceiptPhase.FAILED, ReviewEvent.EXEMPT): ReceiptPhase.EXEMPT,
    (ReceiptPhase.ABSENT, ReviewEvent.RECORD): ReceiptPhase.FINISHED,
    (ReceiptPhase.DIED, ReviewEvent.RECORD): ReceiptPhase.FINISHED,
    (ReceiptPhase.FAILED, ReviewEvent.RECORD): ReceiptPhase.FINISHED,
    (ReceiptPhase.FINISHED, ReviewEvent.MARK): ReceiptPhase.FINISHED,
    (ReceiptPhase.FINISHED, ReviewEvent.PRE_LAND_FIX): ReceiptPhase.FINISHED,
    (ReceiptPhase.FINISHED, ReviewEvent.FIX_LANE): ReceiptPhase.FINISHED,
    (ReceiptPhase.FINISHED, ReviewEvent.TICKET): ReceiptPhase.FINISHED,
    (ReceiptPhase.FINISHED, ReviewEvent.DELTA_START): ReceiptPhase.FINISHED,
    (ReceiptPhase.FINISHED, ReviewEvent.DELTA_FINISH): ReceiptPhase.FINISHED,
    (ReceiptPhase.FINISHED, ReviewEvent.DELTA_FAIL): ReceiptPhase.FINISHED,
}


class StaleReviewRunError(LaneError):
    """Raised when a superseded review worker PID attempts to finish or fail a receipt owned by another run."""


# ---------------------------------------------------------------------------
# Review verdicts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Absent:
    """No review receipt exists."""


@dataclass(frozen=True)
class Running:
    """A review process is currently running."""

    pid: Any = None
    started: str | None = None
    log: str | None = None


@dataclass(frozen=True)
class Died:
    """The review receipt says `running`, but its PID is no longer alive."""

    pid: Any = None
    started: str | None = None
    log: str | None = None


@dataclass(frozen=True)
class Failed:
    """The review run failed without producing a finished receipt."""

    error: Any = None
    raw_status: str | None = "failed"


@dataclass(frozen=True)
class PartialPanel:
    """The review receipt is `finished`, `panel_required` is true, and at least one panel member failed."""

    missing_members: tuple[str, ...] = ()
    findings: tuple[dict[str, Any], ...] = ()
    unmarked: tuple[dict[str, Any], ...] = ()
    reviewer: Any = None


@dataclass(frozen=True)
class Exempt:
    """The lane is exempt from review."""

    reason: Any = None


@dataclass(frozen=True)
class Unmarked:
    """The review finished with at least one unmarked finding."""

    findings: tuple[dict[str, Any], ...] = ()
    unmarked: tuple[dict[str, Any], ...] = ()
    reviewer: Any = None
    missing_members: tuple[str, ...] = ()


@dataclass(frozen=True)
class Complete:
    """The review finished and every finding (if any) is marked."""

    findings: tuple[dict[str, Any], ...] = ()
    reviewer: Any = None
    missing_members: tuple[str, ...] = ()


@dataclass(frozen=True)
class Stale:
    """The review receipt's reviewed tip does not cover the lane's landed or current tip (fathers#53)."""

    reviewed_tip: str
    landed_tip: str
    findings: tuple[dict[str, Any], ...] = ()
    unmarked: tuple[dict[str, Any], ...] = ()
    reviewer: Any = None
    missing_members: tuple[str, ...] = ()
    delta: ReviewVerdict | None = None
    is_active: bool = False


ReviewVerdict = Absent | Running | Died | Failed | PartialPanel | Exempt | Unmarked | Complete | Stale


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def alive(pid: Any) -> bool:
    """Whether `pid` names a live local process."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def extract_findings(raw: Receipt | None) -> list[dict[str, Any]]:
    """Normalized finding dictionaries on `raw`."""
    if not raw:
        return []
    items: Any = raw.get("findings")
    if not isinstance(items, list):
        return []
    return [cast(dict[str, Any], f) for f in cast(list[Any], items) if isinstance(f, dict)]


def missing_members(raw: Receipt | None) -> list[str]:
    """Panel members of a finished review whose run failed."""
    if not raw:
        return []
    raw_panel: Any = raw.get("panel")
    if not isinstance(raw_panel, list):
        return []
    return [
        str(cast(dict[str, Any], entry).get("member"))
        for entry in cast(list[Any], raw_panel)
        if isinstance(entry, dict) and cast(dict[str, Any], entry).get("status") == "failed"
    ]


def receipt_phase(raw: Receipt | None) -> ReceiptPhase:
    """Derive the `ReceiptPhase` of `raw`."""
    if raw is None:
        return ReceiptPhase.ABSENT
    raw_status = raw.get("status")
    if raw_status == "exempt":
        return ReceiptPhase.EXEMPT
    if raw_status == "running":
        return ReceiptPhase.RUNNING if alive(raw.get("pid")) else ReceiptPhase.DIED
    if raw_status == "finished":
        return ReceiptPhase.FINISHED
    return ReceiptPhase.FAILED


def _git_read(root: str, *args: str) -> str:
    try:
        result = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, timeout=5)
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def is_clean_integration_merge(
    main: str,
    commit: str,
    int_heads: Sequence[str],
    reviewed_tip: str,
) -> tuple[bool, str]:
    """Whether `commit` is a two-parent merge of an integration commit (ancestor of any commit in `int_heads`)
    into a descendant of `reviewed_tip` whose tree equals the clean merge of its parents (leaving the lane's own
    diff unchanged). Returns `(is_clean, first_parent)`."""
    valid_heads = [h for h in int_heads if h]
    if not valid_heads:
        return False, ""
    parents_out = _git_read(main, "rev-list", "--parents", "-n", "1", commit)
    if not parents_out:
        return False, ""
    parts = parents_out.split()
    if len(parts) != 3:
        return False, ""
    _, p1, p2 = parts
    if not any(git_ok(main, "merge-base", "--is-ancestor", p2, head) for head in valid_heads):
        return False, ""
    if not git_ok(main, "merge-base", "--is-ancestor", reviewed_tip, p1):
        return False, ""
    commit_tree = _git_read(main, "rev-parse", "--verify", "--quiet", f"{commit}^{{tree}}")
    if not commit_tree:
        return False, ""
    try:
        merged = subprocess.run(
            ["git", "merge-tree", "--write-tree", p1, p2],
            cwd=main,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, ""
    if merged.returncode == 0:
        first_line = merged.stdout.splitlines()[0].strip() if merged.stdout.splitlines() else ""
        return (first_line == commit_tree, p1)
    if merged.returncode == 1:
        # Merge conflict was manually resolved: treat as unreviewed.
        return False, ""
    if merged.returncode != 129:
        return False, ""
    # Fallback only when `git merge-tree --write-tree` is unsupported (git < 2.38 usage error 129).
    mb = _git_read(main, "merge-base", p1, p2)
    if not mb:
        return False, ""
    lane_before = _git_read(main, "diff", "--no-ext-diff", mb, p1)
    lane_after = _git_read(main, "diff", "--no-ext-diff", p2, commit)
    return (lane_before == lane_after, p1)


def _in_lane_fixed_shas(
    findings: Sequence[dict[str, Any]],
    *,
    main: str,
    reviewed_tip: str,
    target_tip: str,
    landed_info: dict[str, Any] | None,
    has_delta: bool,
) -> set[str]:
    """Return commit SHAs from `fixed:<sha>` marks that represent in-lane pre-land fixes.

    On an active lane (`landed_info is None`), every valid `fixed` mark is treated as an in-lane fix (so resetting
    away a fix commit makes the review stale). On a landed lane, post-land follow-up merges (not ancestors of
    `target_tip`, or marked after landing, or when a matching delta review already exists) are excluded so they
    neither flip a clean parent to `Stale` nor retroactively cover unreviewed commits in `reviewed_tip..target_tip`.
    """
    if has_delta and landed_info is not None:
        return set()
    landed_at = landed_info.get("at") if landed_info is not None else None
    landed_at_str = landed_at if isinstance(landed_at, str) and landed_at else None
    fixed: set[str] = set()
    for finding in findings:
        raw_mark: Any = finding.get("mark")
        if not isinstance(raw_mark, dict):
            continue
        mark = cast(dict[str, Any], raw_mark)
        if mark.get("kind") != "fixed":
            continue
        merge_sha: Any = mark.get("merge")
        if not isinstance(merge_sha, str) or not OID_RE.fullmatch(merge_sha):
            continue
        if landed_info is not None:
            if mark.get("post_land") is True:
                continue
            mark_at = mark.get("at")
            if landed_at_str and isinstance(mark_at, str) and mark_at > landed_at_str:
                continue
            if not (
                main
                and merge_sha != reviewed_tip
                and git_ok(main, "merge-base", "--is-ancestor", reviewed_tip, merge_sha)
                and git_ok(main, "merge-base", "--is-ancestor", merge_sha, target_tip)
            ):
                continue
        fixed.add(merge_sha)
    return fixed


def _is_tip_covered(
    main: str,
    reviewed_tip: str,
    reviewed_tree: str,
    target_tip: str,
    target_tree: str,
    fixed_shas: set[str],
    integration_heads: Sequence[str],
) -> bool:
    """Whether `target_tip` adds no unreviewed lane-owned diff beyond `reviewed_tip`, ignoring clean integration
    merges and valid marked pre-land fixes."""
    if not git_ok(main, "merge-base", "--is-ancestor", reviewed_tip, target_tip):
        return False
    valid_trees = bool(OID_RE.fullmatch(reviewed_tree) and OID_RE.fullmatch(target_tree))
    if fixed_shas:
        for fix_sha in fixed_shas:
            if not git_ok(main, "merge-base", "--is-ancestor", fix_sha, target_tip):
                return False
        if valid_trees and target_tree == reviewed_tree:
            return False
    elif (
        valid_trees
        and target_tree == reviewed_tree
        and not _git_read(main, "rev-list", "--merges", "-n", "1", f"{reviewed_tip}..{target_tip}")
    ):
        return True
    visited: set[str] = set()
    queue: list[str] = [target_tip]
    while queue:
        sha = queue.pop(0)
        if sha in visited:
            continue
        visited.add(sha)
        if sha == reviewed_tip or git_ok(main, "merge-base", "--is-ancestor", sha, reviewed_tip):
            continue
        parents_out = _git_read(main, "rev-list", "--parents", "-n", "1", sha)
        if not parents_out:
            return False
        parts = parents_out.split()
        if not parts:
            return False
        parents = parts[1:]
        if len(parents) > 1:
            is_clean, p1 = is_clean_integration_merge(main, sha, integration_heads, reviewed_tip)
            if not is_clean:
                return False
            queue.append(p1)
            continue
        if fixed_shas and any(git_ok(main, "merge-base", "--is-ancestor", sha, fix_sha) for fix_sha in fixed_shas):
            queue.extend(parents)
            continue
        return False
    return True


def _resolve_integration_heads(
    main: str,
    record: Record,
    *,
    landed_info: dict[str, Any] | None,
    target_tip: str,
) -> list[str]:
    """Resolve trusted integration heads for clean-merge validation.

    For landed lanes, trust ONLY `landed_info["previous"]` (never post-land integration HEAD or `refs/heads/<base>`).
    For active lanes, use the integration worktree HEAD and `base` ref, excluding any ref that already contains
    `target_tip`.
    """
    candidates: list[str] = []
    if landed_info is not None:
        prev = landed_info.get("previous")
        if isinstance(prev, str) and OID_RE.fullmatch(prev):
            candidates.append(prev)
    else:
        rec_int = record.get("integration")
        if isinstance(rec_int, str) and rec_int and os.path.isdir(rec_int):
            head = _git_read(rec_int, "rev-parse", "--verify", "--quiet", "HEAD")
            if OID_RE.fullmatch(head):
                candidates.append(head)
        base = record.get("base")
        if isinstance(base, str) and base:
            base_head = _git_read(main, "rev-parse", "--verify", "--quiet", f"refs/heads/{base}^{{commit}}")
            if OID_RE.fullmatch(base_head):
                candidates.append(base_head)
            else:
                raw_base = _git_read(main, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}")
                if OID_RE.fullmatch(raw_base):
                    candidates.append(raw_base)
    trusted: list[str] = []
    for head in dict.fromkeys(candidates):
        if (
            target_tip
            and OID_RE.fullmatch(target_tip)
            and git_ok(main, "merge-base", "--is-ancestor", target_tip, head)
        ):
            continue
        trusted.append(head)
    return trusted


def _target_tip_and_tree(record: Record | None) -> tuple[str, str, bool, dict[str, Any] | None, str]:
    """Return `(target_tip, target_tree, is_active, landed_info, main)` for `record`."""
    if record is None:
        return "", "", False, None, ""
    raw_common = record.get("common_dir")
    common = raw_common if isinstance(raw_common, str) and raw_common else ""
    main = main_checkout(common) if common and os.path.isdir(common) else ""
    raw_landed: Any = record.get("landed")
    landed_info = cast(dict[str, Any], raw_landed) if isinstance(raw_landed, dict) else None
    if landed_info is not None and isinstance(landed_info.get("tip"), str) and landed_info["tip"]:
        target_tip = landed_info["tip"]
        raw_ltree = landed_info.get("tree")
        target_tree = raw_ltree if isinstance(raw_ltree, str) else ""
        return target_tip, target_tree, False, landed_info, main
    if lifecycle.active(record) and main:
        branch = record.get("branch")
        if isinstance(branch, str) and branch:
            target_tip = _git_read(main, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}")
            if target_tip:
                target_tree = _git_read(main, "rev-parse", "--verify", "--quiet", f"{target_tip}^{{tree}}")
                return target_tip, target_tree, True, None, main
    return "", "", False, landed_info, main


def matching_delta(
    raw: Receipt | None,
    reviewed_tip: str,
    target_tip: str,
    *,
    target_tree: str = "",
) -> Receipt | None:
    """Return `raw['delta']` when it is a delta receipt covering `reviewed_tip..target_tip`."""
    if raw is None or not reviewed_tip or not target_tip:
        return None
    raw_delta: Any = raw.get("delta")
    if not isinstance(raw_delta, dict):
        return None
    delta = cast(Receipt, raw_delta)
    if delta.get("kind") != "delta":
        return None
    if delta.get("base") != reviewed_tip or delta.get("tip") != target_tip:
        return None
    if target_tree:
        delta_tree = delta.get("tree")
        if delta_tree is not None and (not isinstance(delta_tree, str) or delta_tree != target_tree):
            return None
    return delta


def _stale_tips(raw: Receipt, record: Record | None) -> tuple[str, str, bool] | None:
    """Return `(reviewed_tip, target_tip, is_active)` when finished lane review `raw` does not cover `record`'s
    landed or current active tip (ignoring clean integration merges and valid marked pre-land fixes), else `None`."""
    if record is None:
        return None
    kind = raw.get("kind")
    if kind is not None and kind != "lane":
        return None
    target_tip, target_tree, is_active, landed_info, main = _target_tip_and_tree(record)
    if not target_tip:
        return None

    # Only legacy receipts without `tip` and without `pre_land=True` may skip tip binding.
    if "tip" not in raw and raw.get("pre_land") is not True:
        return None
    raw_reviewed_tip = raw.get("tip")
    if not isinstance(raw_reviewed_tip, str) or not raw_reviewed_tip:
        return "", target_tip, is_active
    reviewed_tip = raw_reviewed_tip

    has_tree_key = "tree" in raw
    raw_reviewed_tree = raw.get("tree")
    if has_tree_key and (not isinstance(raw_reviewed_tree, str) or not raw_reviewed_tree):
        return reviewed_tip, target_tip, is_active
    reviewed_tree = raw_reviewed_tree if isinstance(raw_reviewed_tree, str) else ""

    if reviewed_tip == target_tip:
        if reviewed_tree and target_tree and reviewed_tree != target_tree:
            return reviewed_tip, target_tip, is_active
        return None

    has_delta = matching_delta(raw, reviewed_tip, target_tip, target_tree=target_tree) is not None
    if (
        main
        and OID_RE.fullmatch(reviewed_tip)
        and OID_RE.fullmatch(target_tip)
        and git_ok(main, "cat-file", "-e", reviewed_tip)
        and git_ok(main, "cat-file", "-e", target_tip)
    ):
        resolved_reviewed_tree = reviewed_tree or _git_read(
            main, "rev-parse", "--verify", "--quiet", f"{reviewed_tip}^{{tree}}"
        )
        resolved_target_tree = target_tree or _git_read(
            main, "rev-parse", "--verify", "--quiet", f"{target_tip}^{{tree}}"
        )
        fixed_shas = _in_lane_fixed_shas(
            extract_findings(raw),
            main=main,
            reviewed_tip=reviewed_tip,
            target_tip=target_tip,
            landed_info=landed_info,
            has_delta=has_delta,
        )
        integration_heads = _resolve_integration_heads(
            main, record, landed_info=landed_info, target_tip=target_tip
        )
        if _is_tip_covered(
            main,
            reviewed_tip,
            resolved_reviewed_tree,
            target_tip,
            resolved_target_tree,
            fixed_shas,
            integration_heads,
        ):
            return None
        return reviewed_tip, target_tip, is_active

    return reviewed_tip, target_tip, is_active


def _combine_delta_verdict(
    delta_verdict: ReviewVerdict,
    findings: tuple[dict[str, Any], ...],
    unmarked: tuple[dict[str, Any], ...],
    missing: tuple[str, ...],
    reviewer: Any,
    *,
    panel_required: bool,
) -> ReviewVerdict | None:
    """Combine a finished delta verdict with the primary review; returns `None` when delta is not finished."""
    if not isinstance(delta_verdict, (Complete, Unmarked, PartialPanel)):
        return None
    delta_unmarked = delta_verdict.unmarked if isinstance(delta_verdict, (Unmarked, PartialPanel)) else ()
    all_findings = findings + delta_verdict.findings
    all_unmarked = unmarked + delta_unmarked
    all_missing = missing + delta_verdict.missing_members
    if all_missing and panel_required:
        return PartialPanel(
            missing_members=all_missing,
            findings=all_findings,
            unmarked=all_unmarked,
            reviewer=reviewer,
        )
    if all_unmarked:
        return Unmarked(
            findings=all_findings,
            unmarked=all_unmarked,
            reviewer=reviewer,
            missing_members=all_missing,
        )
    return Complete(
        findings=all_findings,
        reviewer=reviewer,
        missing_members=all_missing,
    )


def derive_verdict(
    raw: Receipt | None,
    *,
    panel_required: bool = False,
    record: Record | None = None,
) -> ReviewVerdict:
    """Evaluate a raw review receipt into one of the 9 `ReviewVerdict` variants."""
    phase = receipt_phase(raw)
    if phase is ReceiptPhase.ABSENT or raw is None:
        return Absent()
    if phase is ReceiptPhase.EXEMPT:
        return Exempt(reason=raw.get("exempt"))
    if phase in (ReceiptPhase.RUNNING, ReceiptPhase.DIED):
        pid = raw.get("pid")
        started = str(raw["started"]) if raw.get("started") is not None else None
        log = str(raw["log"]) if raw.get("log") is not None else None
        if phase is ReceiptPhase.RUNNING:
            return Running(pid=pid, started=started, log=log)
        return Died(pid=pid, started=started, log=log)
    if phase is ReceiptPhase.FAILED:
        raw_status = raw.get("status")
        status_str = str(raw_status) if isinstance(raw_status, str) else None
        return Failed(error=raw.get("error"), raw_status=status_str)
    findings = tuple(extract_findings(raw))
    unmarked = tuple(f for f in findings if not f.get("mark"))
    missing = tuple(missing_members(raw))
    reviewer = raw.get("reviewer")

    # Under panel_required=True, an incomplete primary panel is never masked by Stale or delta eligibility.
    if missing and panel_required:
        return PartialPanel(
            missing_members=missing,
            findings=findings,
            unmarked=unmarked,
            reviewer=reviewer,
        )

    target_tip, target_tree, _is_active, _landed_info, _main = _target_tip_and_tree(record)
    raw_reviewed_tip = raw.get("tip")
    reviewed_tip_str = raw_reviewed_tip if isinstance(raw_reviewed_tip, str) else ""
    existing_delta = matching_delta(raw, reviewed_tip_str, target_tip, target_tree=target_tree)

    stale = _stale_tips(raw, record)
    if stale is not None:
        reviewed_tip, landed_tip, is_active = stale
        delta_raw = matching_delta(raw, reviewed_tip, landed_tip, target_tree=target_tree)
        delta_verdict = (
            derive_verdict(delta_raw, panel_required=panel_required)
            if delta_raw is not None
            else None
        )
        if delta_verdict is not None:
            combined = _combine_delta_verdict(
                delta_verdict, findings, unmarked, missing, reviewer, panel_required=panel_required
            )
            if combined is not None:
                return combined
        return Stale(
            reviewed_tip=reviewed_tip,
            landed_tip=landed_tip,
            findings=findings,
            unmarked=unmarked,
            reviewer=reviewer,
            missing_members=missing,
            delta=delta_verdict,
            is_active=is_active,
        )

    # Never ignore a matching delta receipt once it exists on `raw` (e.g. post-land marks cannot drop delta findings).
    if existing_delta is not None:
        delta_verdict = derive_verdict(existing_delta, panel_required=panel_required)
        combined = _combine_delta_verdict(
            delta_verdict, findings, unmarked, missing, reviewer, panel_required=panel_required
        )
        if combined is not None:
            return combined
        return Stale(
            reviewed_tip=reviewed_tip_str,
            landed_tip=target_tip,
            findings=findings,
            unmarked=unmarked,
            reviewer=reviewer,
            missing_members=missing,
            delta=delta_verdict,
            is_active=_is_active,
        )

    if unmarked:
        return Unmarked(
            findings=findings,
            unmarked=unmarked,
            reviewer=reviewer,
            missing_members=missing,
        )
    return Complete(
        findings=findings,
        reviewer=reviewer,
        missing_members=missing,
    )


def review_words(label: str, verdict: ReviewVerdict, *, prog: str = "lanefather") -> tuple[bool, str]:
    """Map `verdict` onto `(lets_promote_move_dev, human_words)`."""
    if isinstance(verdict, Absent):
        return False, f"no review: run {prog} review {label}"
    if isinstance(verdict, Exempt):
        return True, f"review exempt: {verdict.reason}"
    if isinstance(verdict, Running):
        return False, f"review running since {verdict.started} (pid {verdict.pid}, {verdict.log})"
    if isinstance(verdict, Died):
        return False, f"review died (pid {verdict.pid}, {verdict.log}): rerun {prog} review {label}"
    if isinstance(verdict, PartialPanel):
        return False, (
            f"panel member {', '.join(verdict.missing_members)} gave no review ([review] panel_required): "
            f"{prog} review {label} --force-reason <why> reviews again"
        )
    if isinstance(verdict, Failed):
        return False, f"review failed: {verdict.error}; rerun {prog} review {label}"
    if isinstance(verdict, Stale):
        reviewed_short = verdict.reviewed_tip[:10] if verdict.reviewed_tip else "(missing)"
        target_label = "current" if verdict.is_active else "landed"
        span = f"reviewed {reviewed_short}, {target_label} {verdict.landed_tip[:10]}"
        if isinstance(verdict.delta, Running):
            return False, (
                f"review stale ({span}); delta review running since {verdict.delta.started} "
                f"(pid {verdict.delta.pid}, {verdict.delta.log})"
            )
        if isinstance(verdict.delta, Died):
            return False, (
                f"review stale ({span}); delta review died (pid {verdict.delta.pid}, {verdict.delta.log}): "
                f"rerun {prog} review {label}"
            )
        if isinstance(verdict.delta, Failed):
            return False, (
                f"review stale ({span}); delta review failed: {verdict.delta.error}; "
                f"rerun {prog} review {label}"
            )
        if verdict.is_active:
            return False, f"review stale ({span}): run {prog} review {label} --force-reason <why>"
        return False, f"review stale ({span}): run {prog} review {label}"
    if isinstance(verdict, Unmarked):
        return False, (
            f"{len(verdict.unmarked)} of {len(verdict.findings)} review findings unmarked "
            f"({', '.join('#' + str(f['n']) for f in verdict.unmarked)})"
        )
    return True, f"reviewed by {verdict.reviewer}: {len(verdict.findings)} findings, all marked"


def mark_help(lane_id: str = "{id}", prog: str = "lanefather") -> str:
    return f"{prog} review {lane_id} --mark <n> fixed:<merge>|rejected:<reason>"


def mark_counts(findings: Sequence[dict[str, Any]]) -> dict[str, int]:
    marks: list[dict[str, Any]] = [
        cast(dict[str, Any], m) if isinstance(m := f.get("mark"), dict) else {}
        for f in findings
    ]
    kinds: list[str | None] = [
        str(k) if isinstance(k := m.get("kind"), str) else None
        for m in marks
    ]
    return {
        "findings": len(findings),
        **{s.lower(): sum(1 for f in findings if f.get("severity") == s) for s in ("P0", *SEVERITIES)},
        "fixed": kinds.count("fixed"),
        "rejected": kinds.count("rejected"),
        "unmarked": kinds.count(None),
    }


# ---------------------------------------------------------------------------
# Domain value types & ReviewRecord
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PreLandFix:
    """One truthful pre-land fix round on a reviewed active candidate."""

    reviewed_sha: str
    branch: str | None = None
    worktree: str | None = None
    findings: tuple[int, ...] = ()
    started: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PreLandFix:
        raw_findings: Any = data.get("findings")
        nums = (
            tuple(
                int(n)
                for n in cast(list[Any], raw_findings)
                if isinstance(n, int) and not isinstance(n, bool)
            )
            if isinstance(raw_findings, (list, tuple))
            else ()
        )
        return cls(
            reviewed_sha=str(data.get("reviewed_sha") or ""),
            branch=str(data["branch"]) if data.get("branch") is not None else None,
            worktree=str(data["worktree"]) if data.get("worktree") is not None else None,
            findings=nums,
            started=str(data["started"]) if data.get("started") is not None else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "reviewed_sha": self.reviewed_sha,
            "branch": self.branch,
            "worktree": self.worktree,
            "findings": list(self.findings),
            "started": self.started,
        }


@dataclass(frozen=True)
class MarkApplication:
    """One validated mark applied to a review finding."""

    finding_n: int
    kind: str
    merge: str | None = None
    reason: str | None = None
    at: str = ""
    post_land: bool = False

    def to_dict(self) -> dict[str, Any]:
        if self.kind == "fixed":
            return {
                "kind": "fixed",
                "merge": self.merge,
                "at": self.at,
                **({"post_land": True} if self.post_land else {}),
            }
        return {"kind": "rejected", "reason": self.reason, "at": self.at}


@dataclass(frozen=True)
class MarkResult:
    """Outcome of `ReviewRecord.mark`."""

    review: ReviewRecord
    applied: tuple[MarkApplication, ...]

    @property
    def fixed_count(self) -> int:
        return sum(
            1
            for f in self.review.findings
            if isinstance(m := f.get("mark"), dict) and cast(dict[str, Any], m).get("kind") == "fixed"
        )

    @property
    def rejected_count(self) -> int:
        return sum(
            1
            for f in self.review.findings
            if isinstance(m := f.get("mark"), dict) and cast(dict[str, Any], m).get("kind") == "rejected"
        )


def _resolve_fix_merge(
    store: state.Store,
    common: str,
    value: str,
    *,
    marking_lane_id: str | None = None,
) -> str:
    """Resolve `fixed:<value>`: a landed lane's merge, the active lane's own tip during a pre-land review, or a SHA."""
    target = state.read_json(store.record_path(value)) if state.ID.match(value) else None
    if target is not None and target.get("common_dir") in (None, common):
        raw_landed: Any = target.get("landed")
        landed_info = cast(dict[str, Any], raw_landed) if isinstance(raw_landed, dict) else {}
        landed_merge = landed_info.get("merge")
        if isinstance(landed_merge, str) and landed_merge:
            if marking_lane_id is not None and value == marking_lane_id:
                raise LaneError(
                    f"fixed:{value}: lane {value} is already landed; give the follow-up fix lane or commit"
                )
            return landed_merge
        if lifecycle.active(target) and (marking_lane_id is None or value == marking_lane_id):
            target_rev = ReviewRecord.load(store, value, record=target)
            branch = target.get("branch")
            if (
                (target_rev.is_pre_land or target_rev.pre_land_fix(target) is not None)
                and isinstance(branch, str)
                and branch
            ):
                tip = gitops.git(
                    main_checkout(common),
                    "rev-parse",
                    "--verify",
                    "--quiet",
                    f"refs/heads/{branch}^{{commit}}",
                    check=False,
                )
                if tip:
                    return tip
    sha = gitops.git(main_checkout(common), "rev-parse", "--verify", "--quiet", value + "^{commit}", check=False)
    if not value or not sha:
        raise LaneError(f"fixed:{value}: not a commit or a landed lane here; give the follow-up merge")
    return sha


@dataclass(frozen=True)
class ReviewRecord:
    """Domain record over `<label>.review.json` with read projections and locked transitions."""

    label: str
    raw: Receipt | None
    phase: ReceiptPhase
    verdict: ReviewVerdict
    prog: str = "lanefather"

    @property
    def exists(self) -> bool:
        return self.raw is not None

    @property
    def is_pre_land(self) -> bool:
        return self.raw is not None and self.raw.get("pre_land") is True

    @property
    def reviewed_tip(self) -> str:
        if self.raw is None:
            return ""
        tip = self.raw.get("tip")
        return tip if isinstance(tip, str) else ""

    @property
    def reviewed_tree(self) -> str:
        if self.raw is None:
            return ""
        tree = self.raw.get("tree")
        return tree if isinstance(tree, str) else ""

    @property
    def allows_promote(self) -> bool:
        return isinstance(self.verdict, (Exempt, Complete))

    @property
    def has_finished_receipt(self) -> bool:
        return isinstance(self.verdict, (PartialPanel, Unmarked, Complete, Stale))

    @property
    def delta_raw(self) -> Receipt | None:
        if self.raw is None:
            return None
        raw_delta: Any = self.raw.get("delta")
        return cast(Receipt, raw_delta) if isinstance(raw_delta, dict) else None

    def matching_delta(self, record: Record | None = None) -> Receipt | None:
        """The `delta` sub-receipt when it matches this review's tip and `record`'s target tip/tree."""
        if self.raw is None:
            return None
        reviewed_tip = self.reviewed_tip
        if not reviewed_tip:
            return None
        if record is None:
            delta = self.delta_raw
            if delta is None or delta.get("kind") != "delta" or delta.get("base") != reviewed_tip:
                return None
            return delta
        target_tip, target_tree, _is_active, _landed_info, _main = _target_tip_and_tree(record)
        return matching_delta(self.raw, reviewed_tip, target_tip, target_tree=target_tree)

    @property
    def findings(self) -> tuple[dict[str, Any], ...]:
        if isinstance(self.verdict, (PartialPanel, Unmarked, Complete, Stale)):
            return self.verdict.findings
        return ()

    @property
    def open_findings(self) -> tuple[dict[str, Any], ...]:
        if isinstance(self.verdict, (PartialPanel, Unmarked, Stale)):
            return self.verdict.unmarked
        return ()

    def summary(self, *, prog: str | None = None) -> tuple[bool, str]:
        return review_words(self.label, self.verdict, prog=prog or self.prog)

    def pre_land_fix(self, record: Record | None = None) -> PreLandFix | None:
        """Authoritative `.review.json` `pre_land_fix` with guarded legacy `record['pre_land_fix']` fallback."""
        if self.raw is not None:
            raw_auth: Any = self.raw.get("pre_land_fix")
            if isinstance(raw_auth, dict) and raw_auth:
                return PreLandFix.from_dict(cast(dict[str, Any], raw_auth))
        if record is not None:
            raw_legacy: Any = record.get("pre_land_fix")
            if isinstance(raw_legacy, dict) and raw_legacy:
                legacy = cast(dict[str, Any], raw_legacy)
                tip = str(self.raw.get("tip") or "") if self.raw is not None else ""
                if self.has_finished_receipt and (not tip or legacy.get("reviewed_sha") == tip):
                    return PreLandFix.from_dict(legacy)
        return None

    def reviewer_views(self) -> list[dict[str, Any]]:
        """Each reviewer of a finished review with its own findings (fathers#151)."""
        payload = self.raw or {}
        all_findings = list(self.findings)
        raw_panel: Any = payload.get("panel")
        panel = (
            [cast(dict[str, Any], e) for e in cast(list[Any], raw_panel) if isinstance(e, dict)]
            if isinstance(raw_panel, list)
            else []
        )
        if not panel:
            raw_status = payload.get("status")
            status_str = str(raw_status) if raw_status is not None else None
            return [{
                "member": payload.get("reviewer"),
                "reviewer": payload.get("reviewer"),
                "provider": payload.get("provider"),
                "cli": payload.get("cli"),
                "model": payload.get("model"),
                "effort": payload.get("effort"),
                "options": payload.get("options"),
                "seconds": payload.get("seconds"),
                "status": status_str,
                "reason": payload.get("fallback"),
                "findings": all_findings,
            }]
        by_number = {f["n"]: f for f in all_findings if "n" in f}
        views: list[dict[str, Any]] = []
        for entry in panel:
            raw_nums: Any = entry.get("numbers")
            numbers = (
                [int(n) for n in cast(list[Any], raw_nums) if isinstance(n, int) and not isinstance(n, bool)]
                if isinstance(raw_nums, list)
                else []
            )
            views.append(dict(entry, findings=[by_number[n] for n in numbers if n in by_number]))
        return views

    # ------------------------------------------------------------------
    # Readers
    # ------------------------------------------------------------------

    @classmethod
    def from_raw(
        cls,
        label: str,
        raw: Receipt | None,
        *,
        panel_required: bool = False,
        prog: str = "lanefather",
        record: Record | None = None,
    ) -> ReviewRecord:
        return cls(
            label=label,
            raw=raw,
            phase=receipt_phase(raw),
            verdict=derive_verdict(raw, panel_required=panel_required, record=record),
            prog=prog,
        )

    @classmethod
    def load(
        cls,
        store: state.Store,
        label: str,
        *,
        panel_required: bool | None = None,
        lane_created: str | None = None,
        record: Record | None = None,
    ) -> ReviewRecord:
        required = store.project.review_panel_required if panel_required is None else panel_required
        expected = lane_created if lane_created is not None else state.lane_created_of(record)
        raw = state.read_json(store.receipt_path(label, "review"))
        if not state.receipt_matches_lane(raw, expected):
            raw = None
        return cls.from_raw(label, raw, panel_required=required, prog=store.project.command, record=record)

    @classmethod
    def all(
        cls,
        store: state.Store,
        *,
        panel_required: bool | None = None,
    ) -> list[ReviewRecord]:
        """Every readable `*.review.json` in `store`, sorted by filename, without creating directories."""
        required = store.project.review_panel_required if panel_required is None else panel_required
        found: list[ReviewRecord] = []
        with contextlib.suppress(OSError):
            for candidate in sorted(store.directory.glob("*.review.json")):
                raw = state.read_json(candidate)
                if raw is not None:
                    label = str(raw.get("label") or candidate.name.removesuffix(".review.json"))
                    found.append(cls.from_raw(label, raw, panel_required=required, prog=store.project.command))
        return found

    @classmethod
    def unmarked(
        cls,
        store: state.Store,
        records: Iterable[Record],
    ) -> Iterator[tuple[Record, list[dict[str, Any]]]]:
        """Open findings in each finished lane review, in record order."""
        for record in records:
            rev = cls.load(store, str(record["id"]), record=record)
            if rev.open_findings:
                yield record, list(rev.open_findings)

    @classmethod
    def fix_parent(
        cls,
        store: state.Store,
        lane_id: str,
        common: str,
        refusal: str,
    ) -> ReviewRecord:
        """Validate `lane_id` as a landed lane in `common` whose finished review has unmarked findings."""
        parent_record = state.read_json(store.record_path(lane_id))
        if parent_record is None:
            raise LaneError(f"{refusal}: no lane by that name")
        raw_landed: Any = parent_record.get("landed")
        landed_info = cast(dict[str, Any], raw_landed) if isinstance(raw_landed, dict) else {}
        if not lifecycle.landed(parent_record) or not landed_info.get("merge"):
            raise LaneError(
                f"{refusal}: lane {lane_id} is {parent_record.get('status')}, not landed; a fix lane fixes "
                "the review findings of landed lanes"
            )
        if parent_record.get("common_dir") != common:
            raise LaneError(f"{refusal}: lane {lane_id} is from another repository")
        rev = cls.load(store, lane_id, record=parent_record)
        if isinstance(rev.verdict, Stale):
            raise LaneError(
                f"{refusal}: lane {lane_id}'s review is stale "
                f"(reviewed {rev.verdict.reviewed_tip[:10]}, landed {rev.verdict.landed_tip[:10]}); "
                f"run {store.prog} review {lane_id} to cover the post-review commits first"
            )
        if not rev.open_findings or rev.raw is None:
            raise LaneError(f"{refusal}: lane {lane_id} has no finished review with unmarked findings")
        return rev

    # ------------------------------------------------------------------
    # Single locked writer & named mutators
    # ------------------------------------------------------------------

    @classmethod
    def _transition(
        cls,
        store: state.Store,
        label: str,
        event: ReviewEvent,
        build: Callable[[Receipt | None], Receipt],
        *,
        force: bool = False,
        owner_pid: int | None = None,
        lane_created: str | None = None,
        record: Record | None = None,
    ) -> ReviewRecord:
        """The single writer of `<label>.review.json`."""
        with store.locked(f"review-{label}"):
            raw_existing = state.read_json(store.receipt_path(label, "review"))
            if (
                event in (ReviewEvent.FINISH, ReviewEvent.FAIL)
                and owner_pid is not None
                and raw_existing is not None
                and raw_existing.get("pid") != owner_pid
            ):
                raise StaleReviewRunError(
                    f"review {label}: stale worker pid {owner_pid} cannot {event.value} receipt owned by "
                    f"pid {raw_existing.get('pid')}"
                )
            if event in (ReviewEvent.DELTA_FINISH, ReviewEvent.DELTA_FAIL) and owner_pid is not None:
                raw_delta: Any = raw_existing.get("delta") if isinstance(raw_existing, dict) else None
                delta_pid = cast(dict[str, Any], raw_delta).get("pid") if isinstance(raw_delta, dict) else None
                if delta_pid != owner_pid:
                    raise StaleReviewRunError(
                        f"delta review {label}: stale worker pid {owner_pid} cannot {event.value} delta owned by "
                        f"pid {delta_pid}"
                    )
            existing = (
                raw_existing
                if state.receipt_matches_lane(raw_existing, lane_created)
                else None
            )
            phase = receipt_phase(existing)
            if (force and event is ReviewEvent.START) or (
                phase is ReceiptPhase.RUNNING
                and event is ReviewEvent.START
                and owner_pid is not None
                and existing is not None
                and existing.get("pid") == owner_pid
            ):
                target = ReceiptPhase.RUNNING
            elif force and event is ReviewEvent.RECORD:
                target = ReceiptPhase.FINISHED
            elif (phase, event) in REVIEW_TRANSITIONS:
                target = REVIEW_TRANSITIONS[(phase, event)]
            else:
                raise LaneError(f"review {label}: cannot apply {event.value!r} in phase {phase.value!r}")
            updated = build(existing)
            if updated.get("status") != target.value:
                raise LaneError(
                    f"review {label}: event {event.value!r} must produce status {target.value!r}, "
                    f"got {updated.get('status')!r}"
                )
            store.directory.mkdir(parents=True, exist_ok=True)
            state.write_json(store.receipt_path(label, "review"), updated)
            return cls.from_raw(
                label,
                updated,
                panel_required=store.project.review_panel_required,
                prog=store.project.command,
                record=record,
            )

    @classmethod
    def begin(
        cls,
        store: state.Store,
        label: str,
        fields: dict[str, Any],
        *,
        pid: int,
        log: str | None,
        effort: str,
        started: str | None = None,
        force: bool = False,
    ) -> ReviewRecord:
        started_at = started or journal.now()
        raw_created = fields.get("lane_created")
        lane_created = raw_created if isinstance(raw_created, str) and raw_created else None
        return cls._transition(
            store,
            label,
            ReviewEvent.START,
            lambda _existing: {
                "version": 1,
                "label": label,
                **fields,
                "status": ReceiptPhase.RUNNING.value,
                "pid": pid,
                "log": log,
                "started": started_at,
                "effort": effort,
            },
            force=force,
            owner_pid=pid,
            lane_created=lane_created,
        )

    @classmethod
    def spawn_and_begin(
        cls,
        store: state.Store,
        label: str,
        fields: dict[str, Any],
        *,
        cmd: Sequence[str],
        cwd: str,
        env: dict[str, str],
        log: Path,
        effort: str,
        started: str | None = None,
    ) -> ReviewRecord:
        def _build(_existing: Receipt | None) -> Receipt:
            log.parent.mkdir(parents=True, exist_ok=True)
            with open(log, "w", encoding="utf-8") as out:
                child = subprocess.Popen(
                    list(cmd),
                    cwd=cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    env=env,
                )
            return {
                "version": 1,
                "label": label,
                **fields,
                "status": ReceiptPhase.RUNNING.value,
                "pid": child.pid,
                "log": str(log),
                "started": started or journal.now(),
                "effort": effort,
            }

        raw_created = fields.get("lane_created")
        lane_created = raw_created if isinstance(raw_created, str) and raw_created else None
        return cls._transition(store, label, ReviewEvent.START, _build, lane_created=lane_created)

    @classmethod
    def finish(
        cls,
        store: state.Store,
        label: str,
        payload: dict[str, Any],
        *,
        owner_pid: int | None = None,
        finished: str | None = None,
    ) -> ReviewRecord:
        def _build(existing: Receipt | None) -> Receipt:
            merged: Receipt = {
                **(existing or {}),
                **payload,
                "status": ReceiptPhase.FINISHED.value,
            }
            merged.setdefault("fallback", None)
            if finished is not None or "finished" not in merged:
                merged["finished"] = finished or journal.now()
            return merged

        return cls._transition(store, label, ReviewEvent.FINISH, _build, owner_pid=owner_pid)

    @classmethod
    def fail(
        cls,
        store: state.Store,
        label: str,
        error: str,
        *,
        extra: dict[str, Any] | None = None,
        owner_pid: int | None = None,
        finished: str | None = None,
    ) -> ReviewRecord:
        def _build(existing: Receipt | None) -> Receipt:
            merged: Receipt = {
                **(existing or {}),
                **(extra or {}),
                "status": ReceiptPhase.FAILED.value,
                "error": error,
            }
            merged.setdefault("fallback", None)
            if finished is not None or "finished" not in merged:
                merged["finished"] = finished or journal.now()
            return merged

        return cls._transition(store, label, ReviewEvent.FAIL, _build, owner_pid=owner_pid)

    @classmethod
    def exempt(
        cls,
        store: state.Store,
        label: str,
        reason: str,
        *,
        fields: dict[str, Any] | None = None,
        finished: str | None = None,
    ) -> ReviewRecord:
        raw_created = fields.get("lane_created") if fields else None
        lane_created = raw_created if isinstance(raw_created, str) and raw_created else None
        return cls._transition(
            store,
            label,
            ReviewEvent.EXEMPT,
            lambda _existing: {
                "version": 1,
                "label": label,
                **(fields or {}),
                "status": ReceiptPhase.EXEMPT.value,
                "exempt": reason,
                "finished": finished or journal.now(),
            },
            lane_created=lane_created,
        )

    @classmethod
    def record_external(
        cls,
        store: state.Store,
        label: str,
        payload: dict[str, Any],
        *,
        force: bool = False,
        merge_panel: bool = False,
        finished: str | None = None,
    ) -> ReviewRecord:
        """Record a finished review performed outside lanefather (`review --record`, fathers#252).

        When `merge_panel=True` and an existing finished receipt exists from a different reviewer, combines both into
        a multi-reviewer panel receipt (for `double` review class).
        """
        raw_created = payload.get("lane_created")
        lane_created = raw_created if isinstance(raw_created, str) and raw_created else None

        def _panel_entry_from(data: dict[str, Any], numbers: list[int]) -> dict[str, Any]:
            rev_name = data.get("reviewer") if isinstance(data.get("reviewer"), str) else "external"
            return {
                "member": rev_name,
                "reviewer": rev_name,
                "model": data.get("model"),
                "effort": data.get("effort"),
                "provider": data.get("provider"),
                "cli": data.get("cli"),
                "options": data.get("options"),
                "seconds": data.get("seconds", 0.0),
                "status": "finished",
                "output": data.get("output"),
                "numbers": numbers,
            }

        def _build(existing: Receipt | None) -> Receipt:
            if merge_panel and existing is not None and existing.get("status") == ReceiptPhase.FINISHED.value:
                prior_findings = [dict(f) for f in extract_findings(existing)]
                raw_panel: Any = existing.get("panel")
                if isinstance(raw_panel, list) and raw_panel:
                    panel = [dict(cast(dict[str, Any], e)) for e in cast(list[Any], raw_panel) if isinstance(e, dict)]
                else:
                    prior_nums = [int(f["n"]) for f in prior_findings if isinstance(f.get("n"), int)]
                    prior_rev = existing.get("reviewer") if isinstance(existing.get("reviewer"), str) else "external"
                    for f in prior_findings:
                        f.setdefault("reviewer", prior_rev)
                    panel = [_panel_entry_from(existing, prior_nums)]
                new_rev = payload.get("reviewer") if isinstance(payload.get("reviewer"), str) else "external"
                raw_new_findings: Any = payload.get("findings")
                new_items = (
                    [dict(cast(dict[str, Any], f)) for f in cast(list[Any], raw_new_findings) if isinstance(f, dict)]
                    if isinstance(raw_new_findings, list)
                    else []
                )
                new_nums: list[int] = []
                next_n = max((int(f["n"]) for f in prior_findings if isinstance(f.get("n"), int)), default=0) + 1
                for item in new_items:
                    item["n"] = next_n
                    item["reviewer"] = new_rev
                    new_nums.append(next_n)
                    prior_findings.append(item)
                    next_n += 1
                panel.append(_panel_entry_from(payload, new_nums))
                reviewers = [str(e.get("reviewer")) for e in panel if isinstance(e.get("reviewer"), str)]
                models = [str(e.get("model")) for e in panel if isinstance(e.get("model"), str)]
                merged: Receipt = {
                    **existing,
                    "reviewer": "+".join(dict.fromkeys(reviewers)),
                    "model": ", ".join(dict.fromkeys(models)),
                    "findings": prior_findings,
                    "panel": panel,
                    "status": ReceiptPhase.FINISHED.value,
                    "recorded": True,
                    "finished": finished or journal.now(),
                }
                return merged
            merged = {
                "version": 1,
                "label": label,
                **payload,
                "status": ReceiptPhase.FINISHED.value,
                "recorded": True,
            }
            merged.setdefault("fallback", None)
            merged.setdefault("seconds", 0.0)
            merged["finished"] = finished or str(merged.get("finished") or journal.now())
            return merged

        return cls._transition(
            store,
            label,
            ReviewEvent.RECORD,
            _build,
            force=force or merge_panel,
            lane_created=lane_created,
        )

    @classmethod
    def begin_delta(
        cls,
        store: state.Store,
        label: str,
        fields: dict[str, Any],
        *,
        base: str,
        tip: str,
        tree: str,
        pid: int,
        log: str | None,
        effort: str,
        started: str | None = None,
        force: bool = False,
        record: Record | None = None,
    ) -> ReviewRecord:
        started_at = started or journal.now()
        lane_created = state.lane_created_of(record)

        def _build(existing: Receipt | None) -> Receipt:
            assert existing is not None
            prev_delta: Any = existing.get("delta")
            if isinstance(prev_delta, dict):
                prev_dict = cast(Receipt, prev_delta)
                prev_phase = receipt_phase(prev_dict)
                if prev_phase is ReceiptPhase.RUNNING and prev_dict.get("pid") != pid and not force:
                    raise LaneError(
                        f"delta review {label}: already running (pid {prev_dict.get('pid')})"
                    )
                if (
                    prev_phase is ReceiptPhase.FINISHED
                    and prev_dict.get("base") == base
                    and prev_dict.get("tip") == tip
                    and not force
                ):
                    raise LaneError(
                        f"delta review {label}: already finished for {base[:10]}..{tip[:10]}"
                    )
            delta_entry: Receipt = {
                "kind": "delta",
                "base": base,
                "tip": tip,
                "tree": tree,
                **fields,
                "status": ReceiptPhase.RUNNING.value,
                "pid": pid,
                "log": log,
                "started": started_at,
                "effort": effort,
            }
            return {**existing, "delta": delta_entry}

        return cls._transition(
            store,
            label,
            ReviewEvent.DELTA_START,
            _build,
            owner_pid=pid,
            lane_created=lane_created,
            record=record,
        )

    @classmethod
    def spawn_and_begin_delta(
        cls,
        store: state.Store,
        label: str,
        fields: dict[str, Any],
        *,
        base: str,
        tip: str,
        tree: str,
        cmd: Sequence[str],
        cwd: str,
        env: dict[str, str],
        log: Path,
        effort: str,
        started: str | None = None,
        record: Record | None = None,
    ) -> ReviewRecord:
        lane_created = state.lane_created_of(record)

        def _build(existing: Receipt | None) -> Receipt:
            assert existing is not None
            prev_delta: Any = existing.get("delta")
            if isinstance(prev_delta, dict):
                prev_dict = cast(Receipt, prev_delta)
                prev_phase = receipt_phase(prev_dict)
                if prev_phase is ReceiptPhase.RUNNING:
                    raise LaneError(
                        f"delta review {label}: already running (pid {prev_dict.get('pid')})"
                    )
                if (
                    prev_phase is ReceiptPhase.FINISHED
                    and prev_dict.get("base") == base
                    and prev_dict.get("tip") == tip
                ):
                    raise LaneError(
                        f"delta review {label}: already finished for {base[:10]}..{tip[:10]}"
                    )
            log.parent.mkdir(parents=True, exist_ok=True)
            with open(log, "w", encoding="utf-8") as out:
                child = subprocess.Popen(
                    list(cmd),
                    cwd=cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    env=env,
                )
            delta_entry: Receipt = {
                "kind": "delta",
                "base": base,
                "tip": tip,
                "tree": tree,
                **fields,
                "status": ReceiptPhase.RUNNING.value,
                "pid": child.pid,
                "log": str(log),
                "started": started or journal.now(),
                "effort": effort,
            }
            return {**existing, "delta": delta_entry}

        return cls._transition(
            store,
            label,
            ReviewEvent.DELTA_START,
            _build,
            lane_created=lane_created,
            record=record,
        )

    @classmethod
    def finish_delta(
        cls,
        store: state.Store,
        label: str,
        payload: dict[str, Any],
        *,
        owner_pid: int | None = None,
        finished: str | None = None,
        record: Record | None = None,
    ) -> ReviewRecord:
        lane_created = state.lane_created_of(record)

        def _build(existing: Receipt | None) -> Receipt:
            assert existing is not None
            raw_prev: Any = existing.get("delta")
            prev_delta = cast(Receipt, raw_prev) if isinstance(raw_prev, dict) else {}
            primary_nums = [
                int(f["n"])
                for f in extract_findings(existing)
                if isinstance(f.get("n"), int) and not isinstance(f.get("n"), bool)
            ]
            offset = max(primary_nums, default=0)
            merged_delta: Receipt = {
                **prev_delta,
                **payload,
                "kind": "delta",
                "status": ReceiptPhase.FINISHED.value,
            }
            if offset > 0:
                renumbered: list[dict[str, Any]] = []
                for f in extract_findings(merged_delta):
                    raw_n = f.get("n")
                    if isinstance(raw_n, int) and not isinstance(raw_n, bool):
                        renumbered.append({**f, "n": raw_n + offset})
                    else:
                        renumbered.append(dict(f))
                merged_delta["findings"] = renumbered
                raw_panel: Any = merged_delta.get("panel")
                if isinstance(raw_panel, list):
                    panel_copy: list[Any] = []
                    for entry in cast(list[Any], raw_panel):
                        if isinstance(entry, dict):
                            entry_dict = dict(cast(dict[str, Any], entry))
                            raw_nums: Any = entry_dict.get("numbers")
                            if isinstance(raw_nums, list):
                                entry_dict["numbers"] = [
                                    (n + offset if isinstance(n, int) and not isinstance(n, bool) else n)
                                    for n in cast(list[Any], raw_nums)
                                ]
                            panel_copy.append(entry_dict)
                        else:
                            panel_copy.append(entry)
                    merged_delta["panel"] = panel_copy
            merged_delta.setdefault("fallback", None)
            if finished is not None or "finished" not in merged_delta:
                merged_delta["finished"] = finished or journal.now()
            return {**existing, "delta": merged_delta}

        return cls._transition(
            store,
            label,
            ReviewEvent.DELTA_FINISH,
            _build,
            owner_pid=owner_pid,
            lane_created=lane_created,
            record=record,
        )

    @classmethod
    def fail_delta(
        cls,
        store: state.Store,
        label: str,
        error: str,
        *,
        extra: dict[str, Any] | None = None,
        owner_pid: int | None = None,
        finished: str | None = None,
        record: Record | None = None,
    ) -> ReviewRecord:
        lane_created = state.lane_created_of(record)

        def _build(existing: Receipt | None) -> Receipt:
            assert existing is not None
            raw_prev: Any = existing.get("delta")
            prev_delta = cast(Receipt, raw_prev) if isinstance(raw_prev, dict) else {}
            merged_delta: Receipt = {
                **prev_delta,
                **(extra or {}),
                "kind": "delta",
                "status": ReceiptPhase.FAILED.value,
                "error": error,
            }
            merged_delta.setdefault("fallback", None)
            if finished is not None or "finished" not in merged_delta:
                merged_delta["finished"] = finished or journal.now()
            return {**existing, "delta": merged_delta}

        return cls._transition(
            store,
            label,
            ReviewEvent.DELTA_FAIL,
            _build,
            owner_pid=owner_pid,
            lane_created=lane_created,
            record=record,
        )

    @classmethod
    def mark(
        cls,
        store: state.Store,
        record: Record,
        raw_marks: Sequence[tuple[str, str]],
    ) -> MarkResult:
        return cls.mark_label(
            store,
            str(record["id"]),
            str(record["common_dir"]),
            raw_marks,
            record=record,
        )

    @classmethod
    def mark_label(
        cls,
        store: state.Store,
        label: str,
        common: str,
        raw_marks: Sequence[tuple[str, str]],
        *,
        record: Record | None = None,
    ) -> MarkResult:
        applied: list[MarkApplication] = []
        lane_created = state.lane_created_of(record) if record is not None else None
        marking_lane_id = str(record["id"]) if record is not None else None
        with store.locked(f"review-{label}"):
            loaded = cls.load(store, label, record=record)
            if not loaded.has_finished_receipt or loaded.raw is None:
                subject = f"lane {label}" if record is not None else f"review {label}"
                raise LaneError(f"{subject} has no finished review to mark ({loaded.summary()[1]})")
            pre_land = loaded.pre_land_fix(record)

            def _build(existing: Receipt | None) -> Receipt:
                assert existing is not None
                findings_copy = [dict(f) for f in extract_findings(existing)]
                target_tip, target_tree, _is_active, landed_info, _main = _target_tip_and_tree(record)
                raw_reviewed_tip = existing.get("tip")
                reviewed_tip_str = raw_reviewed_tip if isinstance(raw_reviewed_tip, str) else ""
                matched_delta = matching_delta(existing, reviewed_tip_str, target_tip, target_tree=target_tree)
                has_finished_delta = (
                    matched_delta is not None
                    and matched_delta.get("status") == ReceiptPhase.FINISHED.value
                )
                delta_copy = dict(matched_delta) if has_finished_delta and matched_delta is not None else None
                delta_findings_copy = (
                    [dict(f) for f in extract_findings(delta_copy)] if delta_copy is not None else []
                )
                # Map finding number -> (finding_dict, is_delta); validate strict ints and refuse collisions.
                by_number: dict[int, tuple[dict[str, Any], bool]] = {}
                for f in findings_copy:
                    raw_n = f.get("n")
                    if isinstance(raw_n, int) and not isinstance(raw_n, bool):
                        by_number[raw_n] = (f, False)
                for f in delta_findings_copy:
                    raw_n = f.get("n")
                    if isinstance(raw_n, int) and not isinstance(raw_n, bool):
                        if raw_n in by_number:
                            raise LaneError(
                                f"review {label}: delta finding #{raw_n} collides with primary finding #{raw_n}"
                            )
                        by_number[raw_n] = (f, True)
                is_landed = record is not None and lifecycle.landed(record)
                landed_merge = (
                    landed_info["merge"]
                    if landed_info is not None and isinstance(landed_info.get("merge"), str) and landed_info["merge"]
                    else ""
                )
                for number, value in raw_marks:
                    found_entry = by_number.get(int(number)) if number.isdigit() else None
                    if found_entry is None:
                        available = ", ".join(str(n) for n in by_number) or "none"
                        raise LaneError(f"--mark {number}: the review of {label} has findings {available}")
                    finding, is_delta_finding = found_entry
                    kind, _, detail = value.partition(":")
                    detail = detail.strip()
                    if kind == "fixed":
                        fix_sha = _resolve_fix_merge(store, common, detail, marking_lane_id=marking_lane_id)
                        main = main_checkout(common)
                        if is_delta_finding:
                            delta_tip = (
                                delta_copy["tip"]
                                if delta_copy is not None and isinstance(delta_copy.get("tip"), str)
                                else ""
                            )
                            refs = [r for r in (delta_tip, landed_merge) if r and OID_RE.fullmatch(r)]
                            for ref_sha in dict.fromkeys(refs):
                                if fix_sha == ref_sha or not git_ok(
                                    main, "merge-base", "--is-ancestor", ref_sha, fix_sha
                                ):
                                    raise LaneError(
                                        f"--mark {number} fixed:{detail}: commit {fix_sha[:10]} does not advance past "
                                        f"the delta-reviewed commit {ref_sha[:10]}; commit the fix on top of it first"
                                    )
                                ref_tree = gitops.git(
                                    main, "rev-parse", "--verify", "--quiet", f"{ref_sha}^{{tree}}", check=False
                                )
                                fix_tree = gitops.git(
                                    main, "rev-parse", "--verify", "--quiet", f"{fix_sha}^{{tree}}", check=False
                                )
                                if ref_tree and fix_tree == ref_tree:
                                    raise LaneError(
                                        f"--mark {number} fixed:{detail}: commit {fix_sha[:10]} has the same tree as "
                                        f"the delta-reviewed commit {ref_sha[:10]}; commit a real fix first"
                                    )
                        else:
                            if is_landed and (
                                (target_tip and fix_sha == target_tip)
                                or (landed_merge and fix_sha == landed_merge)
                            ):
                                raise LaneError(
                                    f"--mark {number} fixed:{detail}: commit {fix_sha[:10]} is the landed lane "
                                    "itself; give the follow-up fix commit or lane"
                                )
                            reviewed_sha = (
                                pre_land.reviewed_sha
                                if pre_land is not None
                                else (loaded.reviewed_tip if loaded.is_pre_land else "")
                            )
                            if reviewed_sha:
                                if fix_sha == reviewed_sha or not git_ok(
                                    main, "merge-base", "--is-ancestor", reviewed_sha, fix_sha
                                ):
                                    raise LaneError(
                                        f"--mark {number} fixed:{detail}: commit {fix_sha[:10]} does not advance past "
                                        f"the reviewed commit {reviewed_sha[:10]}; commit the fix on top of it first"
                                    )
                                reviewed_tree = gitops.git(
                                    main, "rev-parse", "--verify", "--quiet", f"{reviewed_sha}^{{tree}}", check=False
                                )
                                fix_tree = gitops.git(
                                    main, "rev-parse", "--verify", "--quiet", f"{fix_sha}^{{tree}}", check=False
                                )
                                if reviewed_tree and fix_tree == reviewed_tree:
                                    raise LaneError(
                                        f"--mark {number} fixed:{detail}: commit {fix_sha[:10]} has the same tree as "
                                        f"the reviewed commit {reviewed_sha[:10]}; commit a real fix first"
                                    )
                        applied.append(
                            MarkApplication(
                                finding_n=int(finding["n"]),
                                kind="fixed",
                                merge=fix_sha,
                                at=journal.now(),
                                post_land=is_landed,
                            )
                        )
                    elif kind == "rejected":
                        if not detail:
                            raise LaneError(f"--mark {number} rejected: give the reason, rejected:<reason>")
                        applied.append(
                            MarkApplication(
                                finding_n=int(finding["n"]),
                                kind="rejected",
                                reason=detail,
                                at=journal.now(),
                            )
                        )
                    else:
                        raise LaneError(f"--mark {number} {value}: use fixed:<merge> or rejected:<reason>")
                for app in applied:
                    by_number[app.finding_n][0]["mark"] = app.to_dict()
                updated_receipt: Receipt = {**existing, "findings": findings_copy}
                if delta_copy is not None:
                    updated_receipt["delta"] = {**delta_copy, "findings": delta_findings_copy}
                return updated_receipt

            updated_rev = cls._transition(
                store, label, ReviewEvent.MARK, _build, lane_created=lane_created, record=record
            )
        return MarkResult(review=updated_rev, applied=tuple(applied))

    @classmethod
    def open_pre_land_fix(
        cls,
        store: state.Store,
        record: Record,
        worktree: str,
    ) -> tuple[ReviewRecord, PreLandFix]:
        """Open one truthful pre-land fix round on `.review.json` without writing `<id>.json`."""
        lane_id = str(record["id"])
        common = str(record["common_dir"])
        lane_created = state.lane_created_of(record)
        with store.locked(f"review-{lane_id}"):
            loaded = cls.load(store, lane_id, record=record)
            if isinstance(loaded.verdict, Stale):
                raise LaneError(
                    f"hand-over refused: lane {lane_id}'s review is stale "
                    f"(reviewed {loaded.verdict.reviewed_tip[:10]}, current {loaded.verdict.landed_tip[:10]}); "
                    f"rerun {store.prog} review {lane_id} --force-reason <why>"
                )
            open_items = loaded.open_findings
            if not open_items or loaded.raw is None:
                raise LaneError(f"hand-over refused: lane {lane_id} has no finished review with unmarked findings")
            existing_fix = loaded.pre_land_fix(record)
            if existing_fix is not None:
                sha = (existing_fix.reviewed_sha or str(loaded.raw.get("tip") or ""))[:10]
                raise LaneError(
                    f"hand-over refused: lane {lane_id} already has a pre-land fix round "
                    f"(reviewed commit {sha}); only one pre-land fix round is allowed per review"
                )
            main = main_checkout(common)
            reviewed_sha = str(loaded.raw.get("tip") or "")
            if not reviewed_sha or not git_ok(main, "rev-parse", "--verify", "--quiet", reviewed_sha + "^{commit}"):
                raise LaneError(
                    f"hand-over refused: lane {lane_id}'s reviewed commit {reviewed_sha[:10] or '(none)'} "
                    "is missing from Git"
                )
            fix_round = PreLandFix(
                reviewed_sha=reviewed_sha,
                branch=str(record["branch"]),
                worktree=worktree,
                findings=tuple(int(f["n"]) for f in open_items),
                started=journal.now(),
            )
            updated_rev = cls._transition(
                store,
                lane_id,
                ReviewEvent.PRE_LAND_FIX,
                lambda existing: {**(existing or {}), "pre_land_fix": fix_round.to_dict()},
                lane_created=lane_created,
            )
        return updated_rev, fix_round

    @classmethod
    def attach_fix_lane(
        cls,
        store: state.Store,
        lane_id: str,
        fix_id: str,
        *,
        record: Record | None = None,
        lane_created: str | None = None,
    ) -> ReviewRecord:
        expected = lane_created if lane_created is not None else state.lane_created_of(record)
        return cls._transition(
            store,
            lane_id,
            ReviewEvent.FIX_LANE,
            lambda existing: {**(existing or {}), "fix_lane": fix_id},
            lane_created=expected,
        )

    @classmethod
    def record_ticket(
        cls,
        store: state.Store,
        label: str,
        *,
        issue: str | None = None,
        issue_error: str | None = None,
    ) -> ReviewRecord:
        def _build(existing: Receipt | None) -> Receipt:
            updated: Receipt = dict(existing or {})
            if issue:
                updated["issue"] = issue
                updated.pop("issue_error", None)
            else:
                updated["issue_error"] = issue_error
            return updated

        return cls._transition(store, label, ReviewEvent.TICKET, _build)

    @classmethod
    def clear_stale(
        cls,
        store: state.Store,
        label: str,
        *,
        lane_created: str | None = None,
    ) -> bool:
        """Remove a superseded pre-land `<label>.review.json` under the review lock so a fresh post-land review runs."""
        with store.locked(f"review-{label}"):
            path = store.receipt_path(label, "review")
            raw = state.read_json(path)
            if raw is None or not state.receipt_matches_lane(raw, lane_created):
                return False
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
            return True
