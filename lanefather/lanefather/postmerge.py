"""Post-merge smoke, recheck, clearing, and pure lane-completion decision model (fathers#167, fathers#227).

Owns `<state>/<id>.post-merge.json` and `<state>/<id>.recheck.json` writes, the `PostMergePhase x PostMergeEvent`
state machine, stale worker fencing (`StalePostMergeError`), crashed green `<id>.recheck.json` recovery, cross-project
clearing evidence lookup against `store.directory / "results"`, and the pure zero-I/O `decide_completion(...)` algebra
shared by `done`, `done --finished`, post-smoke auto-done, and post-review auto-done.

Never consults `config.current()` or `state.state_dir()`.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import os
from pathlib import Path
from typing import Any, Literal, cast

from . import lifecycle, receipts, review_record, state
from . import results as result_store
from .errors import LaneError
from .gitops import git, git_ok
from .state import Record

Receipt = dict[str, Any]
PostMergeReceiptKind = Literal["post-merge", "recheck"]


class PostMergePhase(StrEnum):
    """Lifecycle phase of a lane's post-merge verification."""

    UNRUN = "unrun"
    GREEN = "green"
    RED = "red"
    CLEARED = "cleared"


class PostMergeEvent(StrEnum):
    """State-machine transitions permitted on a lane's post-merge verification."""

    SMOKE_GREEN = "smoke_green"
    SMOKE_RED = "smoke_red"
    RECHECK_GREEN = "recheck_green"
    RECHECK_RED = "recheck_red"
    CLEAR = "clear"


POST_MERGE_TRANSITIONS: dict[tuple[PostMergePhase, PostMergeEvent], PostMergePhase] = {
    (PostMergePhase.UNRUN, PostMergeEvent.SMOKE_GREEN): PostMergePhase.GREEN,
    (PostMergePhase.UNRUN, PostMergeEvent.SMOKE_RED): PostMergePhase.RED,
    (PostMergePhase.RED, PostMergeEvent.RECHECK_GREEN): PostMergePhase.CLEARED,
    (PostMergePhase.RED, PostMergeEvent.RECHECK_RED): PostMergePhase.RED,
    (PostMergePhase.RED, PostMergeEvent.CLEAR): PostMergePhase.CLEARED,
    (PostMergePhase.CLEARED, PostMergeEvent.CLEAR): PostMergePhase.CLEARED,
}


class StalePostMergeError(LaneError):
    """Raised when a late smoke or recheck attempts to write after `done` or for a mismatched merge SHA."""


# ---------------------------------------------------------------------------
# Pure completion algebra (`CompletionMode`, `CompletionAction`, `decide_completion`)
# ---------------------------------------------------------------------------


class CompletionMode(StrEnum):
    """Trigger context for evaluating whether a lane should finish, release, hold, or refuse."""

    INTERACTIVE = "interactive"
    FINISHED = "finished"
    AUTO_AFTER_SMOKE = "auto_after_smoke"
    AUTO_AFTER_REVIEW = "auto_after_review"


class CompletionAction(StrEnum):
    """Outcome selected by `decide_completion`."""

    CLEANUP = "cleanup"
    RELEASE = "release"
    HOLD_KEPT = "hold_kept"
    HOLD_REVIEW = "hold_review"
    HOLD_AUTO_OFF = "hold_auto_off"
    NOOP = "noop"
    REFUSE = "refuse"


@dataclass(frozen=True)
class CompletionDecision:
    """Pure decision produced by `decide_completion`."""

    action: CompletionAction
    mode: CompletionMode
    ready: bool
    already_done: bool = False
    reason: str | None = None
    refusal: str | None = None
    cleared_by: dict[str, Any] | None = None
    worktree_lock: str | None = None


def decide_completion(
    stage: str | None,
    smoke: PostMergePhase,
    review_verdict: review_record.ReviewVerdict,
    *,
    kept: bool,
    done_auto: bool,
    worktree_lock: str | None,
    mode: CompletionMode,
    lane_id: str = "{id}",
    prog: str = "lanefather",
    raw_smoke: str | None = None,
    cleared_by: dict[str, Any] | None = None,
) -> CompletionDecision:
    """Pure, zero-I/O decision over `(stage, smoke, review_verdict, kept, done_auto, worktree_lock, mode)`."""
    effective_cleared = cleared_by if smoke is PostMergePhase.CLEARED else None
    is_auto = mode in (CompletionMode.AUTO_AFTER_SMOKE, CompletionMode.AUTO_AFTER_REVIEW)

    if stage == "done":
        if mode is CompletionMode.INTERACTIVE:
            return CompletionDecision(
                action=CompletionAction.REFUSE,
                mode=mode,
                ready=False,
                already_done=True,
                reason="it is done",
                refusal=f"lane {lane_id} is already done",
            )
        return CompletionDecision(
            action=CompletionAction.NOOP,
            mode=mode,
            ready=False,
            already_done=True,
            reason="it is done",
            refusal=f"lane {lane_id} is done, not landed",
        )

    if stage not in lifecycle.AWAITING_DONE:
        reason = f"it is {stage}"
        refusal = f"lane {lane_id} is {stage}, not landed"
        return CompletionDecision(
            action=CompletionAction.NOOP if is_auto else CompletionAction.REFUSE,
            mode=mode,
            ready=False,
            reason=reason,
            refusal=refusal,
        )

    if smoke is PostMergePhase.RED:
        reason = "its post-merge smoke is red"
        refusal = (
            "its post-merge check is red: fix forward (a follow-up commit on the lane) first, or "
            f"rerun its smoke on the integration tip with {prog} recheck {lane_id}"
        )
        return CompletionDecision(
            action=CompletionAction.NOOP if is_auto else CompletionAction.REFUSE,
            mode=mode,
            ready=False,
            reason=reason,
            refusal=refusal,
        )

    if smoke is PostMergePhase.UNRUN:
        smoke_words = f"its post-merge smoke is {raw_smoke or 'not run'}"
        return CompletionDecision(
            action=CompletionAction.NOOP if is_auto else CompletionAction.REFUSE,
            mode=mode,
            ready=False,
            reason=smoke_words,
            refusal=smoke_words,
        )

    # Smoke is GREEN or CLEARED and stage is landed or released.
    if mode is CompletionMode.AUTO_AFTER_SMOKE and kept:
        return CompletionDecision(
            action=CompletionAction.HOLD_KEPT,
            mode=mode,
            ready=False,
            reason="kept",
            refusal=f"lane {lane_id} is kept ({prog} keep {lane_id} --off releases it)",
            cleared_by=effective_cleared,
        )

    allowed_verdicts: tuple[type[Any], ...] = (
        (review_record.Absent, review_record.Exempt, review_record.Complete)
        if mode is CompletionMode.INTERACTIVE
        else (review_record.Exempt, review_record.Complete)
    )
    if not isinstance(review_verdict, allowed_verdicts):
        _, words = review_record.review_words(lane_id, review_verdict, prog=prog)
        refusal = (
            words
            if isinstance(review_verdict, review_record.Absent)
            else f"its {words}: mark each finding with {review_record.mark_help(lane_id, prog)}"
        )
        if mode is CompletionMode.AUTO_AFTER_SMOKE:
            return CompletionDecision(
                action=CompletionAction.HOLD_REVIEW,
                mode=mode,
                ready=False,
                reason=words,
                refusal=refusal,
                cleared_by=effective_cleared,
            )
        if mode is CompletionMode.AUTO_AFTER_REVIEW:
            return CompletionDecision(
                action=CompletionAction.NOOP,
                mode=mode,
                ready=False,
                reason=words,
                refusal=refusal,
                cleared_by=effective_cleared,
            )
        return CompletionDecision(
            action=CompletionAction.REFUSE,
            mode=mode,
            ready=False,
            reason=words,
            refusal=refusal,
            cleared_by=effective_cleared,
        )

    if kept and mode is not CompletionMode.INTERACTIVE:
        kept_refusal = f"lane {lane_id} is kept ({prog} keep {lane_id} --off releases it)"
        return CompletionDecision(
            action=CompletionAction.HOLD_KEPT if is_auto else CompletionAction.REFUSE,
            mode=mode,
            ready=False,
            reason="kept",
            refusal=kept_refusal,
            cleared_by=effective_cleared,
        )

    if is_auto and not done_auto:
        return CompletionDecision(
            action=CompletionAction.HOLD_AUTO_OFF,
            mode=mode,
            ready=False,
            reason="auto-done is off",
            cleared_by=effective_cleared,
        )

    if worktree_lock is not None:
        if is_auto:
            return CompletionDecision(
                action=CompletionAction.RELEASE,
                mode=mode,
                ready=False,
                reason=f"locked ({worktree_lock})",
                cleared_by=effective_cleared,
                worktree_lock=worktree_lock,
            )
        lock_refusal = (
            f"an agent still holds the worktree (locked: {worktree_lock}); "
            f"run {prog} done {lane_id} once it exits"
        )
        return CompletionDecision(
            action=CompletionAction.REFUSE,
            mode=mode,
            ready=False,
            reason=f"locked ({worktree_lock})",
            refusal=lock_refusal,
            cleared_by=effective_cleared,
            worktree_lock=worktree_lock,
        )

    return CompletionDecision(
        action=CompletionAction.CLEANUP,
        mode=mode,
        ready=True,
        cleared_by=effective_cleared,
    )


# ---------------------------------------------------------------------------
# Record projections, receipt writer, and clearing evidence
# ---------------------------------------------------------------------------


def landed(record: Record) -> dict[str, Any]:
    value: Any = record.get("landed")
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def post_merge(record: Record) -> dict[str, Any]:
    value: Any = record.get("post_merge")
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def persisted_phase(record: Record) -> PostMergePhase:
    """Derive the persisted `PostMergePhase` on `record` without Git or external receipt I/O."""
    post = post_merge(record)
    raw_status = post.get("status")
    raw_cleared: Any = post.get("cleared_by")
    cleared_dict = cast(dict[str, Any], raw_cleared) if isinstance(raw_cleared, dict) else None
    has_cleared = bool(cleared_dict)
    if raw_status == "green":
        return PostMergePhase.CLEARED if has_cleared else PostMergePhase.GREEN
    if raw_status == "red":
        return PostMergePhase.CLEARED if has_cleared else PostMergePhase.RED
    return PostMergePhase.UNRUN


def apply_post_merge_event(lane_id: str, phase: PostMergePhase, event: PostMergeEvent) -> PostMergePhase:
    """Validate `(phase, event)` against `POST_MERGE_TRANSITIONS`, raising `LaneError` when disallowed."""
    target = POST_MERGE_TRANSITIONS.get((phase, event))
    if target is None:
        raise LaneError(f"post-merge {lane_id}: cannot apply {event.value!r} in phase {phase.value!r}")
    return target


def _write_receipt(
    store: state.Store,
    lane_id: str,
    kind: PostMergeReceiptKind,
    data: Receipt,
) -> Path:
    """Single writer for `<id>.post-merge.json` and `<id>.recheck.json`."""
    store.directory.mkdir(parents=True, exist_ok=True)
    target = store.receipt_path(lane_id, kind)
    state.write_json(target, data)
    return target


def _check_not_stale(record: Record, merge_sha: str, *, action: str) -> None:
    """Fence late smoke or recheck writes after `done`, non-landed status, or a superseded merge SHA."""
    lane_id = str(record.get("id") or "?")
    stage = record.get("status")
    if lifecycle.finished(record):
        raise StalePostMergeError(f"lane {lane_id}: cannot {action} after lane is already done")
    if not lifecycle.awaiting_done(record):
        raise StalePostMergeError(f"lane {lane_id}: cannot {action} while status is {stage!r}")
    recorded_merge = landed(record).get("merge")
    if not merge_sha or recorded_merge != merge_sha:
        raise StalePostMergeError(
            f"lane {lane_id}: stale {action} for merge {merge_sha[:10]} "
            f"(record holds {str(recorded_merge or '')[:10] or 'none'})"
        )


def red_receipt(record: Record, store: state.Store) -> Receipt | None:
    """The lane's red post-merge receipt, when it is the smoke of the merge the lane landed as."""
    receipt = receipts.read(store, record["id"], "post-merge")
    merge = landed(record).get("merge")
    if not receipt or not receipts.is_red(receipt) or not merge or receipt.get("merge") != merge:
        return None
    return receipt


def integration_of(record: Record) -> str | None:
    """The integration worktree the lane landed into, when it still exists."""
    path = landed(record).get("integration") or record.get("integration")
    return os.path.realpath(path) if path and os.path.isdir(path) else None


def ran(red: Receipt) -> tuple[dict[str, list[str]] | None, list[str]]:
    """What the red smoke ran: its checks by suite, or None for whole suites; and its suites."""
    selected: Any = red.get("checks")
    named: list[Any] = red.get("suites") or []
    suites = [str(s) for s in named]
    if isinstance(selected, dict):
        return {str(k): [str(c) for c in v] for k, v in cast(dict[str, Any], selected).items()}, suites
    return None, suites


def uncovered(red: Receipt, green: Receipt) -> list[str]:
    """The checks (whole suites, for a smoke that ran them) of the red smoke that the green smoke did not pass on its
    own tree: fresh, or a pass recorded on that same tree. A result it took from another tree (the lane's receipt,
    the previous tip) stands for nothing here."""
    selected, suites = ran(red)
    if not suites:
        return ["(the red smoke names no suites)"]
    tree = receipts.tree_of(green)
    rows: list[dict[str, Any]] = green.get("results") or []
    whole = {str(r.get("suite")) for r in rows if r.get("status") == "passed" and "checks" not in r}
    results: dict[str, Any] = green.get("check_results") or {}

    def stands(check: str) -> bool:
        entry: dict[str, Any] = results.get(check) or {}
        return entry.get("status") == "passed" and entry.get("reused_from") in (None, tree)

    if selected is None:
        return [s for s in suites if s not in whole]
    return [c for suite, ids in selected.items() for c in ids if suite not in whole and not stands(c)]


def _tree_entries(store: state.Store, tree: str) -> list[dict[str, Any]]:
    """Read `<store.directory>/results/<tree>.json` without creating directories or consulting `state_dir()`."""
    if not result_store.TREE.match(tree):
        return []
    data = state.read_json(store.directory / "results" / f"{tree}.json") or {}
    raw_entries: Any = data.get("entries")
    if data.get("v") != result_store.VERSION or data.get("tree") != tree or not isinstance(raw_entries, list):
        return []
    return [
        cast(dict[str, Any], entry)
        for entry in cast(list[Any], raw_entries)
        if isinstance(entry, dict) and isinstance(cast(dict[str, Any], entry).get("checks"), dict)
    ]


def gate_platform(gate: Receipt, path: Any, *, store: state.Store) -> str | None:
    """The platform a batch gate ran on: its receipt's, else that of the full-gate result `store`'s results directory
    holds for this receipt (promote records both); None when neither names one."""
    if gate.get("platform"):
        return str(gate["platform"])
    tree = receipts.tree_of(gate)
    if tree is None:
        return None
    found = [e for e in _tree_entries(store, tree) if e.get("full") and e.get("receipt") == str(path)]
    return str(found[-1]["platform"]) if found and found[-1].get("platform") else None


def _crashed_green_recheck(
    record: Record,
    store: state.Store,
    integration: str,
    red: Receipt,
    *,
    head: str | None = None,
) -> tuple[Path, Receipt, dict[str, Any]] | None:
    """Return `(path, receipt, cleared_by)` when `<id>.recheck.json` is a valid green recheck of `red`."""
    lane_id = str(record["id"])
    recheck_path = receipts.path(store, lane_id, "recheck")
    recheck_rec = receipts.read(store, lane_id, "recheck")
    if not recheck_rec or not receipts.is_green(recheck_rec):
        return None
    merge = str(red["merge"])
    if recheck_rec.get("merge") != merge:
        return None
    expected_platform = red.get("platform") or result_store.host_platform(store.project)
    recheck_platform = recheck_rec.get("platform") or result_store.host_platform(store.project)
    if recheck_platform != expected_platform:
        return None
    after = str(red.get("finished") or "")
    finished_at = str(recheck_rec.get("finished") or "")
    if after and finished_at < after:
        return None
    if uncovered(red, recheck_rec):
        return None
    sha = str(recheck_rec.get("sha") or "")
    resolved_head = head if head is not None else git(integration, "rev-parse", "HEAD")
    tree_ok = (
        bool(sha)
        and git(integration, "rev-parse", "--verify", "--quiet", f"{sha}^{{tree}}", check=False)
        == receipts.tree_of(recheck_rec)
        and git_ok(integration, "merge-base", "--is-ancestor", merge, sha)
        and git_ok(integration, "merge-base", "--is-ancestor", sha, resolved_head)
    )
    if not tree_ok:
        return None
    red_path = str(receipts.path(store, lane_id, "post-merge"))
    cleared: dict[str, Any] = {
        "kind": "recheck",
        "receipt": str(recheck_path),
        "sha": sha,
        "at": finished_at,
        "red_receipt": red_path,
    }
    return recheck_path, recheck_rec, cleared


def candidates(record: Record, store: state.Store, integration: str, red: Receipt) -> list[dict[str, Any]]:
    """Every receipt that clears the red smoke (crashed green recheck, batch gate, or later smoke), oldest first."""
    merge = str(red["merge"])
    head = git(integration, "rev-parse", "HEAD")
    # The evidence must stand for the platform the red smoke ran on, and not come before it: a run that passed
    # before the smoke failed says nothing about that failure. The stamps have one-second resolution, so a run that
    # finished within the red smoke's second is not earlier (a fast runner lands the next lane in that second).
    platform = red.get("platform") or result_store.host_platform(store.project)
    after = str(red.get("finished") or "")
    red_path = str(receipts.path(store, record["id"], "post-merge"))

    def later(at: str) -> bool:
        return not after or at >= after

    def holds(sha: str, tree: Any) -> bool:
        return (
            bool(sha)
            and git(integration, "rev-parse", "--verify", "--quiet", f"{sha}^{{tree}}", check=False) == tree
            and git_ok(integration, "merge-base", "--is-ancestor", merge, sha)
            and git_ok(integration, "merge-base", "--is-ancestor", sha, head)
        )

    found: list[dict[str, Any]] = []
    recovered = _crashed_green_recheck(record, store, integration, red, head=head)
    if recovered is not None:
        found.append(recovered[2])
    for path, gate in receipts.gates(store):
        sha = str(gate.get("sha") or "")
        if (
            gate.get("full") is True
            and receipts.is_green(gate)
            and bool(after)
            and later(str(gate.get("finished") or ""))
            and gate_platform(gate, path, store=store) == platform
            and holds(sha, receipts.tree_of(gate))
        ):
            found.append({
                "kind": "gate",
                "receipt": str(path),
                "sha": sha,
                "at": str(gate.get("finished") or ""),
                "red_receipt": red_path,
            })
    for other in store.lanes():
        if other["id"] == record["id"] or integration_of(other) != integration:
            continue
        path = receipts.path(store, other["id"], "post-merge")
        smoke: Receipt = receipts.read(store, other["id"], "post-merge") or {}
        sha = str(smoke.get("merge") or "")
        if (
            not receipts.is_green(smoke)
            or sha != landed(other).get("merge")
            or smoke.get("platform") != platform
            or not bool(after)
            or not later(str(smoke.get("finished") or ""))
            or uncovered(red, smoke)
            or not holds(sha, receipts.tree_of(smoke))
        ):
            continue
        found.append({
            "kind": "post-merge",
            "lane": other["id"],
            "receipt": str(path),
            "sha": sha,
            "at": str(smoke.get("finished") or ""),
            "red_receipt": red_path,
        })
    return sorted(found, key=lambda entry: str(entry.get("at") or ""))


def clearing(record: Record, store: state.Store, integration: str | None = None) -> dict[str, Any] | None:
    """What clears the lane's red post-merge smoke, the oldest such receipt; None when nothing does."""
    red = red_receipt(record, store)
    resolved_int = (
        os.path.realpath(integration) if integration and os.path.isdir(integration) else integration_of(record)
    )
    if red is None or resolved_int is None:
        return None
    found = candidates(record, store, resolved_int, red)
    return found[0] if found else None


def words(cleared: dict[str, Any]) -> str:
    """The run that cleared a red smoke, for the ledger and the output."""
    sha = str(cleared.get("sha") or "")[:10]
    what = {
        "gate": f"the batch gate of {sha}",
        "recheck": f"the recheck of {sha}",
        "post-merge": f"the post-merge smoke of {cleared.get('lane')} {sha}",
    }.get(str(cleared.get("kind")), f"{cleared.get('kind')} {sha}")
    return f"{what} ({cleared.get('receipt')})"


# ---------------------------------------------------------------------------
# `RecheckPlan` and `PostMergeRecord`
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecoveredRecheck:
    """A crashed green `<id>.recheck.json` ready to be persisted onto `<id>.json` without rerunning checks."""

    path: Path
    sha: str
    cleared_by: dict[str, Any]


@dataclass(frozen=True)
class RecheckPlan:
    """Validated plan for `lanefather recheck <id>` under the state lock."""

    lane_id: str
    merge: str
    integration: str
    tip: str
    red_receipt: Receipt
    red_path: Path
    suites: list[str]
    selected: dict[str, list[str]] | None
    recovered: RecoveredRecheck | None = None


@dataclass(frozen=True)
class PostMergeRecord:
    """Domain record over `<id>.json["post_merge"]`, `<id>.post-merge.json`, `<id>.recheck.json`, and clearing runs."""

    lane_id: str
    record: Record
    phase: PostMergePhase
    raw_status: str | None
    receipt_path: str | None
    cleared_by: dict[str, Any] | None = None

    @classmethod
    def load(
        cls,
        store: state.Store,
        record: Record,
        *,
        integration: str | None = None,
    ) -> PostMergeRecord:
        """Load and evaluate post-merge state for `record` in `store` without mutating files."""
        lane_id = str(record["id"])
        post = post_merge(record)
        raw = post.get("status")
        raw_status = str(raw) if raw is not None else None
        rec_path = str(post["receipt"]) if post.get("receipt") is not None else None
        raw_cleared: Any = post.get("cleared_by")
        cleared_dict = cast(dict[str, Any], raw_cleared) if isinstance(raw_cleared, dict) else None
        saved_cleared = cleared_dict if cleared_dict else None
        disk_phase = persisted_phase(record)
        if disk_phase is PostMergePhase.GREEN:
            return cls(lane_id=lane_id, record=record, phase=PostMergePhase.GREEN,
                       raw_status=raw_status, receipt_path=rec_path, cleared_by=None)
        if disk_phase is PostMergePhase.CLEARED:
            return cls(lane_id=lane_id, record=record, phase=PostMergePhase.CLEARED,
                       raw_status=raw_status, receipt_path=rec_path, cleared_by=saved_cleared)
        if disk_phase is PostMergePhase.RED:
            found_cleared = clearing(record, store, integration)
            if found_cleared is not None:
                return cls(lane_id=lane_id, record=record, phase=PostMergePhase.CLEARED,
                           raw_status=raw_status, receipt_path=rec_path, cleared_by=found_cleared)
            return cls(lane_id=lane_id, record=record, phase=PostMergePhase.RED,
                       raw_status=raw_status, receipt_path=rec_path, cleared_by=None)
        return cls(lane_id=lane_id, record=record, phase=PostMergePhase.UNRUN,
                   raw_status=raw_status, receipt_path=rec_path, cleared_by=None)

    @classmethod
    def record_smoke(
        cls,
        store: state.Store,
        lane_id: str,
        *,
        merge_sha: str,
        status: str,
        payload: Receipt,
    ) -> tuple[PostMergeRecord, Path]:
        """Validate fencing & phase transition, write `<id>.post-merge.json`, and persist `record["post_merge"]`."""
        if status not in ("green", "red"):
            raise LaneError(f"post-merge {lane_id}: invalid smoke status {status!r}")
        event = PostMergeEvent.SMOKE_GREEN if status == "green" else PostMergeEvent.SMOKE_RED
        with store.locked("state"):
            latest = store.load(lane_id)
            _check_not_stale(latest, merge_sha, action="record post-merge smoke")
            target_phase = apply_post_merge_event(lane_id, persisted_phase(latest), event)
            receipt_data: Receipt = {**payload, "merge": merge_sha, "status": status}
            target = _write_receipt(store, lane_id, "post-merge", receipt_data)
            updated = lifecycle.transition(
                store,
                lane_id,
                "record_smoke",
                post_merge={"status": status, "receipt": str(target)},
            )
            return (
                cls(
                    lane_id=lane_id,
                    record=updated,
                    phase=target_phase,
                    raw_status=status,
                    receipt_path=str(target),
                    cleared_by=None,
                ),
                target,
            )

    @classmethod
    def record_recheck(
        cls,
        store: state.Store,
        lane_id: str,
        *,
        merge_sha: str,
        tip: str,
        status: str,
        payload: Receipt,
        red_receipt_path: str | None = None,
    ) -> tuple[PostMergeRecord, Path]:
        """Validate fencing & phase transition, write `<id>.recheck.json`, and clear `record["post_merge"]` if green."""
        if status not in ("green", "red"):
            raise LaneError(f"post-merge {lane_id}: invalid recheck status {status!r}")
        event = PostMergeEvent.RECHECK_GREEN if status == "green" else PostMergeEvent.RECHECK_RED
        with store.locked("state"):
            latest = store.load(lane_id)
            _check_not_stale(latest, merge_sha, action="record recheck")
            target_phase = apply_post_merge_event(lane_id, persisted_phase(latest), event)
            red_path = red_receipt_path or str(
                payload.get("red_receipt") or receipts.path(store, lane_id, "post-merge")
            )
            receipt_data: Receipt = {
                **payload,
                "kind": "recheck",
                "merge": merge_sha,
                "sha": tip,
                "red_receipt": red_path,
                "status": status,
            }
            target = _write_receipt(store, lane_id, "recheck", receipt_data)
            if target_phase is PostMergePhase.CLEARED:
                cleared_by: dict[str, Any] = {
                    "kind": "recheck",
                    "receipt": str(target),
                    "sha": tip,
                    "red_receipt": red_path,
                }
                updated = lifecycle.transition(
                    store,
                    lane_id,
                    "record_recheck",
                    post_merge={"status": "green", "receipt": str(target), "cleared_by": cleared_by},
                )
                return (
                    cls(
                        lane_id=lane_id,
                        record=updated,
                        phase=PostMergePhase.CLEARED,
                        raw_status="green",
                        receipt_path=str(target),
                        cleared_by=cleared_by,
                    ),
                    target,
                )
            existing_post = post_merge(latest)
            existing_path = str(existing_post["receipt"]) if existing_post.get("receipt") is not None else None
            return (
                cls(
                    lane_id=lane_id,
                    record=latest,
                    phase=PostMergePhase.RED,
                    raw_status="red",
                    receipt_path=existing_path,
                    cleared_by=None,
                ),
                target,
            )

    @classmethod
    def recheck_plan(cls, store: state.Store, record: Record) -> RecheckPlan:
        """Validate `recheck <id>` eligibility and detect any crashed green `<id>.recheck.json` to recover."""
        lane_id = str(record["id"])
        if not lifecycle.awaiting_done(record):
            raise LaneError(f"recheck refused: lane {lane_id} is {record.get('status')}, not landed")
        phase = persisted_phase(record)
        if phase is not PostMergePhase.RED:
            smoke = post_merge(record).get("status")
            shown = "green" if phase is PostMergePhase.CLEARED else (smoke or "not run")
            raise LaneError(f"recheck refused: lane {lane_id}'s post-merge smoke is {shown}: nothing to recheck")
        red = red_receipt(record, store)
        post_path = receipts.path(store, lane_id, "post-merge")
        if red is None:
            raise LaneError(
                f"recheck refused: {post_path} is not the red smoke of "
                f"the merge {str(landed(record).get('merge'))[:10]}"
            )
        selected, suites = ran(red)
        if not suites:
            raise LaneError(f"recheck refused: the red smoke {post_path} names no suites")
        integration = integration_of(record)
        if integration is None:
            raise LaneError(f"recheck refused: lane {lane_id}'s integration worktree is gone")
        merge = str(red["merge"])
        tip = git(integration, "rev-parse", "HEAD")
        if not git_ok(integration, "merge-base", "--is-ancestor", merge, tip):
            raise LaneError(f"recheck refused: the integration tip {tip[:10]} does not hold the merge {merge[:10]}")
        crashed = _crashed_green_recheck(record, store, integration, red, head=tip)
        recovered = (
            RecoveredRecheck(path=crashed[0], sha=str(crashed[1].get("sha") or tip), cleared_by=crashed[2])
            if crashed is not None
            else None
        )
        return RecheckPlan(
            lane_id=lane_id,
            merge=merge,
            integration=integration,
            tip=tip,
            red_receipt=red,
            red_path=post_path,
            suites=suites,
            selected=selected,
            recovered=recovered,
        )

    @classmethod
    def recover_crashed_recheck(
        cls,
        store: state.Store,
        lane_id: str,
        plan: RecheckPlan,
    ) -> tuple[PostMergeRecord, Path]:
        """Persist a crashed green `<id>.recheck.json` onto `<id>.json` under `store.locked("state")`."""
        if plan.recovered is None:
            raise LaneError(f"post-merge {lane_id}: no crashed green recheck to recover")
        rec_info = plan.recovered
        with store.locked("state"):
            latest = store.load(lane_id)
            _check_not_stale(latest, plan.merge, action="recover recheck")
            apply_post_merge_event(lane_id, persisted_phase(latest), PostMergeEvent.RECHECK_GREEN)
            cleared_by: dict[str, Any] = {
                "kind": "recheck",
                "receipt": str(rec_info.path),
                "sha": rec_info.sha,
                "red_receipt": str(plan.red_path),
            }
            updated = lifecycle.transition(
                store,
                lane_id,
                "record_recheck",
                post_merge={"status": "green", "receipt": str(rec_info.path), "cleared_by": cleared_by},
            )
            return (
                cls(
                    lane_id=lane_id,
                    record=updated,
                    phase=PostMergePhase.CLEARED,
                    raw_status="green",
                    receipt_path=str(rec_info.path),
                    cleared_by=cleared_by,
                ),
                rec_info.path,
            )


def recheckable(record: Record, store: state.Store) -> tuple[Receipt, str, str]:
    """Compatibility tuple `(red_receipt, integration, tip)` over `PostMergeRecord.recheck_plan`."""
    plan = PostMergeRecord.recheck_plan(store, record)
    return plan.red_receipt, plan.integration, plan.tip
