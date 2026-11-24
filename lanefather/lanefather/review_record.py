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


ReviewVerdict = Absent | Running | Died | Failed | PartialPanel | Exempt | Unmarked | Complete


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


def derive_verdict(raw: Receipt | None, *, panel_required: bool = False) -> ReviewVerdict:
    """Evaluate a raw review receipt into one of the 8 `ReviewVerdict` variants."""
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
    if missing and panel_required:
        return PartialPanel(
            missing_members=missing,
            findings=findings,
            unmarked=unmarked,
            reviewer=reviewer,
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

    def to_dict(self) -> dict[str, Any]:
        if self.kind == "fixed":
            return {"kind": "fixed", "merge": self.merge, "at": self.at}
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
        return isinstance(self.verdict, (PartialPanel, Unmarked, Complete))

    @property
    def findings(self) -> tuple[dict[str, Any], ...]:
        if isinstance(self.verdict, (PartialPanel, Unmarked, Complete)):
            return self.verdict.findings
        return ()

    @property
    def open_findings(self) -> tuple[dict[str, Any], ...]:
        if isinstance(self.verdict, (PartialPanel, Unmarked)):
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
    ) -> ReviewRecord:
        return cls(
            label=label,
            raw=raw,
            phase=receipt_phase(raw),
            verdict=derive_verdict(raw, panel_required=panel_required),
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
        return cls.from_raw(label, raw, panel_required=required, prog=store.project.command)

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
                by_number = {int(f["n"]): f for f in findings_copy if "n" in f}
                for number, value in raw_marks:
                    finding = by_number.get(int(number)) if number.isdigit() else None
                    if finding is None:
                        available = ", ".join(str(n) for n in by_number) or "none"
                        raise LaneError(f"--mark {number}: the review of {label} has findings {available}")
                    kind, _, detail = value.partition(":")
                    detail = detail.strip()
                    if kind == "fixed":
                        fix_sha = _resolve_fix_merge(store, common, detail, marking_lane_id=marking_lane_id)
                        reviewed_sha = (
                            pre_land.reviewed_sha
                            if pre_land is not None
                            else (loaded.reviewed_tip if loaded.is_pre_land else "")
                        )
                        if reviewed_sha:
                            main = main_checkout(common)
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
                    by_number[app.finding_n]["mark"] = app.to_dict()
                return {**existing, "findings": findings_copy}

            updated_rev = cls._transition(store, label, ReviewEvent.MARK, _build, lane_created=lane_created)
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
