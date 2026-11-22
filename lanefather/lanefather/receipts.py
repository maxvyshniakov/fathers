"""Lane, gate and perf receipts, their verdicts, and the one lane view (fathers#71).

Every lane receipt kind (`check`/`receipt`, `first`, `post-merge`, `recheck`, `review`), batch-gate receipt (`gate`)
and perf receipt (`perf`) is read and written here against an explicit `state.Store` — never `config.current()`.
`results.py` keeps the tree/plan results store and platform resolution; `receipts` calls it for the environment rule.

- `check_verdict(record, receipt, tip_tree, toolchain)` -> `NoReceipt | Green | Red | StaleTree | StaleToolchain`
- `review_verdict(record, receipt)` -> `Absent | Running | Died | Failed | Exempt | Unmarked | Complete`
- `lane_view(store, record)` -> `LaneView`, rendered by `list`, `show` and `status`
"""
from __future__ import annotations

from collections.abc import Iterable, Iterator
import contextlib
from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Literal, cast

from . import config, lifecycle, state
from . import results as result_store
from .errors import LaneError
from .gitops import main_checkout
from .state import Record

Receipt = dict[str, Any]
ReceiptKind = Literal["check", "receipt", "first", "post-merge", "recheck", "review", "gate", "perf"]

LANE_KINDS: tuple[str, ...] = ("receipt", "first", "post-merge", "recheck", "review")
ALL_KINDS: tuple[str, ...] = ("check", "receipt", "first", "post-merge", "recheck", "review", "gate", "perf")
UNCHECKED: Any = object()

_GITHUB_REMOTE = re.compile(
    r"(?:git@github\.com:|https://github\.com/|ssh://git@github\.com/)([\w.-]+/[\w.-]+?)(?:\.git)?/?"
)


# ---------------------------------------------------------------------------
# Receipt paths, reads, writes and directory listings (explicit state.Store)
# ---------------------------------------------------------------------------


def path(store: state.Store, name: str, kind: ReceiptKind = "receipt") -> Path:
    """Path of a receipt in `store` without creating directories."""
    if kind in ("check", "receipt"):
        return store.receipt_path(name, "receipt")
    if kind in ("first", "post-merge", "recheck", "review"):
        return store.receipt_path(name, kind)
    if kind == "gate":
        stem = name if name.startswith("promote-") else f"promote-{name[:10]}"
        return store.directory / f"{stem}.gate.json"
    if kind == "perf":
        stem = name if name.startswith("perf-") else f"perf-{name[:10]}"
        return store.directory / f"{stem}.perf.json"
    raise ValueError(f"unknown receipt kind: {kind!r}")


def read(store: state.Store, name: str, kind: ReceiptKind = "receipt") -> Receipt | None:
    """Read a receipt from `store` without creating directories; `None` when absent or unreadable."""
    return state.read_json(path(store, name, kind))


def write(store: state.Store, name: str, data: Receipt, kind: ReceiptKind = "receipt") -> Path:
    """Atomically write a receipt in `store` and return its path."""
    store.directory.mkdir(parents=True, exist_ok=True)
    target = path(store, name, kind)
    state.write_json(target, data)
    return target


def gates(store: state.Store, *, prefix: str = "") -> list[tuple[Path, Receipt]]:
    """Every readable `<prefix>*.gate.json` receipt in `store`, sorted by filename."""
    found: list[tuple[Path, Receipt]] = []
    with contextlib.suppress(OSError):
        for candidate in sorted(store.directory.glob(f"{prefix}*.gate.json")):
            gate = state.read_json(candidate)
            if gate is not None:
                found.append((candidate, gate))
    return found


def reviews(store: state.Store) -> list[tuple[Path, Receipt]]:
    """Every readable `*.review.json` receipt in `store`, sorted by filename."""
    found: list[tuple[Path, Receipt]] = []
    with contextlib.suppress(OSError):
        for candidate in sorted(store.directory.glob("*.review.json")):
            rev = state.read_json(candidate)
            if rev is not None:
                found.append((candidate, rev))
    return found


def newest_gate(store: state.Store) -> dict[str, Any] | None:
    """The newest batch gate's result (`promote-<sha>.gate.json`) by its finish time."""
    newest: dict[str, Any] | None = None
    for _, gate in gates(store, prefix="promote-"):
        finished = gate.get("finished")
        if isinstance(finished, str) and (newest is None or finished > newest["at"]):
            newest = {"sha": gate.get("sha"), "status": gate.get("status"), "at": finished}
    return newest


# ---------------------------------------------------------------------------
# Receipt field and reuse helpers
# ---------------------------------------------------------------------------


def status_of(receipt: Receipt | None, default: str | None = None) -> str | None:
    """The string `status` field of `receipt`, or `default` when absent."""
    if not receipt:
        return default
    raw = receipt.get("status")
    return str(raw) if isinstance(raw, str) else (str(raw) if raw is not None else default)


def tree_of(receipt: Receipt | None) -> str | None:
    """The `tree` field of `receipt`, or `None` when absent."""
    if not receipt:
        return None
    raw = receipt.get("tree")
    return str(raw) if isinstance(raw, str) and raw else None


def is_green(receipt: Receipt | None) -> bool:
    """Whether `receipt` has `status == 'green'`."""
    return bool(receipt) and receipt.get("status") == "green"


def is_red(receipt: Receipt | None) -> bool:
    """Whether `receipt` has `status == 'red'`."""
    return bool(receipt) and receipt.get("status") == "red"


def same_environment(
    receipt: Receipt | None,
    toolchain: Any,
    *,
    worktree: Any = None,
    platform: str | None = None,
) -> bool:
    """Whether `receipt` ran under `toolchain` and `platform` (this host's by default)."""
    if not receipt:
        return False
    expected = platform or result_store.host_platform()
    return (
        receipt.get("toolchain") == toolchain
        and result_store.receipt_platform(receipt, worktree) == expected
    )


def _red_checks_of(results_rows: Any) -> list[str]:
    found: list[str] = []
    if not isinstance(results_rows, list):
        return found
    for raw_row in cast(list[Any], results_rows):
        if not isinstance(raw_row, dict):
            continue
        row = cast(dict[str, Any], raw_row)
        if row.get("status") != "passed":
            raw_failed: Any = row.get("failed_checks") or []
            if isinstance(raw_failed, list):
                for cid in cast(list[Any], raw_failed):
                    name = str(cid)
                    if name not in found:
                        found.append(name)
    return found


def ensure_first(
    store: state.Store,
    record: Record,
    previous: Receipt | None,
    head: str,
    tree: str,
    failed_checks: list[str],
) -> Receipt:
    """Return `<id>.first.json` for `record`'s incarnation, creating or replacing an older incarnation's file."""
    lane_id = str(record["id"])
    existing = read(store, lane_id, "first")
    if existing and existing.get("lane_created") == record.get("created"):
        return existing
    prior_first: Any = (previous or {}).get("first")
    if not isinstance(prior_first, dict):
        prior_tree = tree_of(previous)
        prior_first = (
            {
                "head": previous.get("head"),
                "tree": prior_tree,
                "failed_checks": _red_checks_of(previous.get("results")),
            }
            if previous and prior_tree
            else None
        )
    saved: Receipt = dict(
        cast(dict[str, Any], prior_first)
        if isinstance(prior_first, dict)
        else {"head": head, "tree": tree, "failed_checks": list(failed_checks)},
        lane_created=record.get("created"),
    )
    write(store, lane_id, saved, "first")
    return saved


def reusable_checks(receipt: Receipt, delta: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Passed check results from `receipt` that `delta` does not re-select."""
    if delta.get("full") or delta.get("selection") is None:
        return {}
    selection_rows = cast(list[dict[str, Any]], delta["selection"])
    changed = {str(row["id"]) for row in selection_rows}
    raw_results: Any = receipt.get("check_results")
    if not isinstance(raw_results, dict):
        return {}
    origin_tree = receipt.get("tree")
    reused: dict[str, dict[str, Any]] = {}
    for check_id, raw_entry in cast(dict[str, Any], raw_results).items():
        if not isinstance(raw_entry, dict):
            continue
        entry = cast(dict[str, Any], raw_entry)
        if entry.get("status") == "passed" and check_id not in changed:
            reused[str(check_id)] = dict(entry, reused_from=entry.get("reused_from") or origin_tree)
    return reused


def lane_ran(store: state.Store, lane_id: str) -> tuple[set[str], set[str], set[str]]:
    """Checks, suites, and whole-run suites recorded in `lane_id`'s check and post-merge receipts."""
    ran_checks: set[str] = set()
    suites: set[str] = set()
    whole: set[str] = set()
    for kind in ("receipt", "post-merge"):
        found = read(store, lane_id, kind) or {}
        raw_rows: Any = found.get("results")
        if not isinstance(raw_rows, list):
            continue
        for raw_row in cast(list[Any], raw_rows):
            if not isinstance(raw_row, dict):
                continue
            row = cast(dict[str, Any], raw_row)
            suite = str(row["suite"])
            suites.add(suite)
            if "checks" in row and isinstance(row["checks"], list):
                ran_checks |= {str(c) for c in cast(list[Any], row["checks"])}
            else:
                whole.add(suite)
    return ran_checks, suites, whole


def perf_reusable(store: state.Store, sha: str, tree: str) -> Receipt | None:
    """Return `perf-<sha[:10]>.perf.json` when it already passed for `tree`, else `None`."""
    previous = read(store, sha, "perf")
    if previous and previous.get("status") == "green" and previous.get("tree") == tree:
        return previous
    return None


def check_receipt_line(receipt: Receipt | None, record: Record) -> str:
    """Summary of a lane's check receipt for ticket comments."""
    if not receipt:
        return "lane receipt: none"
    raw_rows: Any = receipt.get("results") or []
    rows = (
        [cast(dict[str, Any], r) for r in cast(list[Any], raw_rows) if isinstance(r, dict)]
        if isinstance(raw_rows, list)
        else []
    )
    suites = ", ".join(str(r["suite"]) for r in rows if r.get("status") == "passed" and "suite" in r) or "none"
    raw_checks: Any = receipt.get("check_results")
    count = len(cast(dict[str, Any], raw_checks)) if isinstance(raw_checks, dict) else 0
    line = (
        f"lane receipt {receipt.get('status', '?')} on tree `{str(receipt.get('tree', ''))[:10]}`: suites "
        f"{suites}" + (f" ({count} checks)" if count else "")
    )
    raw_overrides: Any = record.get("overrides") or []
    overrides = (
        [cast(dict[str, Any], o) for o in cast(list[Any], raw_overrides) if isinstance(o, dict)]
        if isinstance(raw_overrides, list)
        else []
    )
    forced = [
        str(o["reason"])
        for o in overrides
        if isinstance(o.get("refusals"), list)
        and any("receipt" in str(refusal) for refusal in cast(list[Any], o["refusals"]))
    ]
    return line + (f"; landed with --force-reason: {forced[-1]}" if forced else "")


# ---------------------------------------------------------------------------
# Check verdict (fathers#59 seam)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NoReceipt:
    """The lane has no check receipt."""


@dataclass(frozen=True)
class Green:
    """The check receipt is green for the tree (and environment, when checked)."""
    tree: str | None = None
    tree_match: bool | None = True


@dataclass(frozen=True)
class Red:
    """The check receipt covers the tree (or no tip tree was given) and is not green."""
    failed: tuple[str, ...] = ()
    unstarted: tuple[str, ...] = ()
    regressions: tuple[str, ...] = ()
    rerun_alone: bool = False
    classification_base: str | None = None
    tree: str | None = None
    tree_match: bool | None = True
    status: str | None = "red"


@dataclass(frozen=True)
class StaleTree:
    """The check receipt's tree differs from the branch's tip tree."""
    receipt_tree: Any = None
    tip_tree: str = ""
    status: str | None = None


@dataclass(frozen=True)
class StaleToolchain:
    """The check receipt is green for the tree, but its toolchain or platform does not match."""
    tree: str | None = None
    tree_match: bool | None = True
    toolchain_changed: bool = True
    platform: str = ""


CheckVerdict = NoReceipt | Green | Red | StaleTree | StaleToolchain


def check_verdict(
    record: Record | None,
    receipt: Receipt | None,
    tip_tree: str | None,
    toolchain: Any = UNCHECKED,
    *,
    platform: str | None = None,
) -> CheckVerdict:
    """Evaluate a lane check receipt against `tip_tree` and, when given, `toolchain` and `platform`."""
    if not receipt:
        return NoReceipt()
    raw_status = receipt.get("status")
    status_str = str(raw_status) if raw_status is not None else None
    raw_tree = receipt.get("tree")
    tree_str = str(raw_tree) if isinstance(raw_tree, str) else None
    if tip_tree and raw_tree != tip_tree:
        return StaleTree(receipt_tree=raw_tree, tip_tree=tip_tree, status=status_str)
    matched: bool | None = True if tip_tree else None
    if raw_status != "green":
        raw_results: Any = receipt.get("results") or []
        suite_rows = (
            [cast(dict[str, Any], r) for r in cast(list[Any], raw_results) if isinstance(r, dict)]
            if isinstance(raw_results, list)
            else []
        )
        failed = tuple(
            str(r["suite"]) for r in suite_rows if r.get("status") not in ("passed", "not_run") and "suite" in r
        )
        unstarted = tuple(
            str(r["suite"]) for r in suite_rows if r.get("status") == "not_run" and "suite" in r
        )
        raw_cls: Any = receipt.get("classification")
        classification = cast(dict[str, Any], raw_cls) if isinstance(raw_cls, dict) else {}
        raw_cls_checks: Any = classification.get("checks") or []
        reg_entries = (
            [
                cast(dict[str, Any], e)
                for e in cast(list[Any], raw_cls_checks)
                if isinstance(e, dict) and cast(dict[str, Any], e).get("class") == "regression"
            ]
            if isinstance(raw_cls_checks, list)
            else []
        )
        regressions = tuple(str(e["check"]) for e in reg_entries if "check" in e)
        rerun_alone = bool(reg_entries) and all(
            (cast(dict[str, Any], alone) if isinstance(alone := e.get("alone"), dict) else {}).get("status")
            != "not rerun"
            for e in reg_entries
        )
        cls_base = str(classification["base"]) if classification.get("base") is not None else None
        return Red(
            failed=failed,
            unstarted=unstarted,
            regressions=regressions,
            rerun_alone=rerun_alone,
            classification_base=cls_base,
            tree=tree_str,
            tree_match=matched,
            status=status_str,
        )
    if toolchain is not UNCHECKED:
        worktree = record.get("worktree") if record else None
        expected_platform = platform or result_store.host_platform()
        toolchain_ok = receipt.get("toolchain") == toolchain
        platform_ok = result_store.receipt_platform(receipt, worktree) == expected_platform
        if not (toolchain_ok and platform_ok):
            return StaleToolchain(
                tree=tree_str,
                tree_match=matched,
                toolchain_changed=not toolchain_ok,
                platform=expected_platform,
            )
    return Green(tree=tree_str, tree_match=matched)


def smoke_basis(
    record: Record | None,
    receipt: Receipt | None,
    landed: dict[str, Any],
    toolchain: Any,
    worktree: Any = None,
    *,
    toolchain_name: str = "toolchain",
) -> str | None:
    """Why `receipt` cannot stand for part of its merge's smoke (#640), or `None` when it can."""
    effective_record: Record | None = (
        {"worktree": worktree} if worktree is not None else record
    )
    verdict = check_verdict(effective_record, receipt, str(landed["tree"]), toolchain)
    if isinstance(verdict, NoReceipt) or receipt is None:
        return "the lane has no receipt"
    if isinstance(verdict, Red) or (isinstance(verdict, StaleTree) and verdict.status != "green"):
        return "the lane's receipt is red"
    if isinstance(verdict, StaleTree):
        return (
            f"the lane's receipt covers tree {str(verdict.receipt_tree)[:10]}, not the landed tree "
            f"{str(landed['tree'])[:10]}"
        )
    if receipt.get("checks") is None or receipt.get("check_results") is None:
        return "the lane's receipt has no per-check results"
    if isinstance(verdict, StaleToolchain):
        if verdict.toolchain_changed:
            return f"the {toolchain_name} changed since the lane check"
        return f"the lane check did not run on {verdict.platform}"
    return None


# ---------------------------------------------------------------------------
# Review verdict (fathers#53 / fathers#60 seam)
# ---------------------------------------------------------------------------


def alive(pid: Any) -> bool:
    """Whether `pid` names a live local process (bare PID until fathers#60 adds start time)."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def extract_findings(receipt: Receipt | None) -> list[dict[str, Any]]:
    """Normalized finding dictionaries on `receipt`."""
    if not receipt:
        return []
    raw: Any = receipt.get("findings")
    if not isinstance(raw, list):
        return []
    return [cast(dict[str, Any], f) for f in cast(list[Any], raw) if isinstance(f, dict)]


def unmarked_findings(receipt: Receipt | None) -> list[dict[str, Any]]:
    """Findings on `receipt` that carry no mark."""
    return [finding for finding in extract_findings(receipt) if not finding.get("mark")]


def missing_members(receipt: Receipt | None) -> list[str]:
    """Panel members of a finished review whose run failed."""
    if not receipt:
        return []
    raw_panel: Any = receipt.get("panel")
    if not isinstance(raw_panel, list):
        return []
    return [
        str(cast(dict[str, Any], entry).get("member"))
        for entry in cast(list[Any], raw_panel)
        if isinstance(entry, dict) and cast(dict[str, Any], entry).get("status") == "failed"
    ]


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
    """The review failed, or a required panel member failed."""
    error: Any = None
    missing_members: tuple[str, ...] = ()
    findings: tuple[dict[str, Any], ...] = ()
    raw_status: str | None = "failed"


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


ReviewVerdict = Absent | Running | Died | Failed | Exempt | Unmarked | Complete


def review_verdict(
    record: Record | None,
    receipt: Receipt | None,
    *,
    panel_required: bool = False,
) -> ReviewVerdict:
    """Evaluate a review receipt's state (`record` reserved for fathers#54/#53)."""
    _ = record
    if receipt is None:
        return Absent()
    raw_status = receipt.get("status")
    if raw_status == "exempt":
        return Exempt(reason=receipt.get("exempt"))
    if raw_status == "running":
        pid = receipt.get("pid")
        started = str(receipt["started"]) if receipt.get("started") is not None else None
        log = str(receipt["log"]) if receipt.get("log") is not None else None
        if alive(pid):
            return Running(pid=pid, started=started, log=log)
        return Died(pid=pid, started=started, log=log)
    status_str = str(raw_status) if isinstance(raw_status, str) else None
    if raw_status != "finished":
        return Failed(error=receipt.get("error"), raw_status=status_str)
    findings = tuple(extract_findings(receipt))
    missing = tuple(missing_members(receipt))
    if missing and panel_required:
        return Failed(
            error=receipt.get("error"),
            missing_members=missing,
            findings=findings,
            raw_status="finished",
        )
    unmarked = tuple(f for f in findings if not f.get("mark"))
    if unmarked:
        return Unmarked(
            findings=findings,
            unmarked=unmarked,
            reviewer=receipt.get("reviewer"),
            missing_members=missing,
        )
    return Complete(
        findings=findings,
        reviewer=receipt.get("reviewer"),
        missing_members=missing,
    )


def has_finished_receipt(verdict: ReviewVerdict) -> bool:
    """Whether `verdict` comes from a finished review receipt (even when `panel_required` failed it)."""
    return isinstance(verdict, (Unmarked, Complete)) or (
        isinstance(verdict, Failed) and bool(verdict.missing_members)
    )


def open_findings_of(verdict: ReviewVerdict) -> tuple[dict[str, Any], ...]:
    """Unmarked findings on a finished review receipt, including partial required panels."""
    if isinstance(verdict, Unmarked):
        return verdict.unmarked
    if isinstance(verdict, Failed) and verdict.missing_members:
        return tuple(f for f in verdict.findings if not f.get("mark"))
    return ()


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
    if isinstance(verdict, Failed):
        if verdict.missing_members:
            return False, (
                f"panel member {', '.join(verdict.missing_members)} gave no review ([review] panel_required): "
                f"{prog} review {label} --force-reason <why> reviews again"
            )
        return False, f"review failed: {verdict.error}; rerun {prog} review {label}"
    if isinstance(verdict, Unmarked):
        return False, (
            f"{len(verdict.unmarked)} of {len(verdict.findings)} review findings unmarked "
            f"({', '.join('#' + str(f['n']) for f in verdict.unmarked)})"
        )
    return True, f"reviewed by {verdict.reviewer}: {len(verdict.findings)} findings, all marked"


def review_state(
    store: state.Store,
    label: str,
    *,
    record: Record | None = None,
    prog: str | None = None,
) -> tuple[bool, str]:
    """Whether `label`'s review in `store` is complete or exempt, and its wording."""
    found = read(store, label, "review")
    verdict = review_verdict(record, found, panel_required=store.project.review_panel_required)
    return review_words(label, verdict, prog=prog or store.project.command)


def mark_help(lane_id: str = "{id}", prog: str | None = None) -> str:
    return f"{prog or config.prog()} review {lane_id} --mark <n> fixed:<merge>|rejected:<reason>"


def fix_parent_review(lane_id: str, common: str, refusal: str, *,
                      store: state.Store) -> dict[str, Any]:
    """A landed lane in this repository with a finished review and an open finding."""
    parent_record = state.read_json(store.record_path(lane_id))
    if parent_record is None:
        raise LaneError(f"{refusal}: no lane by that name")
    landed: dict[str, Any] = parent_record.get("landed") or {}
    if not lifecycle.landed(parent_record) or not landed.get("merge"):
        raise LaneError(f"{refusal}: lane {lane_id} is {parent_record.get('status')}, not landed; a fix lane fixes "
                        "the review findings of landed lanes")
    if parent_record.get("common_dir") != common:
        raise LaneError(f"{refusal}: lane {lane_id} is from another repository")
    receipt = read(store, lane_id, "review")
    verdict = review_verdict(parent_record, receipt, panel_required=store.project.review_panel_required)
    if not open_findings_of(verdict) or receipt is None:
        raise LaneError(f"{refusal}: lane {lane_id} has no finished review with unmarked findings")
    return receipt


def unmarked_reviews(records: Iterable[Record], store: state.Store) -> Iterator[tuple[Record, list[dict[str, Any]]]]:
    """Open findings in each finished lane review, in record order."""
    for record in records:
        found = read(store, str(record["id"]), "review")
        verdict = review_verdict(record, found, panel_required=store.project.review_panel_required)
        if pending := open_findings_of(verdict):
            yield record, list(pending)


# ---------------------------------------------------------------------------
# The one lane view for `list`, `show` and `status`
# ---------------------------------------------------------------------------


def _git_read(root: str, *args: str) -> str:
    try:
        result = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, timeout=2)
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _origin_repository(root: str) -> str | None:
    if not root:
        return None
    origin = _git_read(root, "remote", "get-url", "origin")
    match = _GITHUB_REMOTE.fullmatch(origin)
    return match[1] if match else None


@dataclass(frozen=True)
class LaneView:
    """One read model of a lane record and its receipts, projected by `list`, `show` and `status`."""
    id: str
    project: str
    repository: str | None
    status: str | None
    ticket_raw: str
    ticket_number: str | None
    ticket_url: str | None
    branch: str | None
    base: str | None
    worktree: str | None
    claims: list[Any]
    after: list[Any]
    created: str | None
    agents: list[Any]
    outcome: Any
    check: CheckVerdict
    check_receipt_path: str | None
    post_merge_present: bool
    post_merge_status: Any
    landed_present: bool
    is_active: bool
    is_finished: bool
    review: ReviewVerdict
    review_words: str
    findings_count: int
    open_findings_count: int
    logs: dict[str, str]

    def _check_status_word(self, *, default: str) -> str:
        if isinstance(self.check, NoReceipt):
            return "not run"
        if isinstance(self.check, (Green, StaleToolchain)):
            return "green"
        return self.check.status if self.check.status is not None else default

    def _tree_match(self) -> bool | None:
        if isinstance(self.check, NoReceipt):
            return None
        if isinstance(self.check, StaleTree):
            return False
        return self.check.tree_match

    def _review_status_for_show(self) -> str:
        # Behaviour preserved in Tidy 3 (#71): show reports raw 'running' on a dead review until #60.
        if isinstance(self.review, Absent):
            return "not started"
        if isinstance(self.review, (Running, Died)):
            return "running"
        if isinstance(self.review, Exempt):
            return "exempt"
        if isinstance(self.review, (Unmarked, Complete)):
            return "finished"
        return self.review.raw_status or "not started"

    def _review_status_for_status(self) -> str:
        if isinstance(self.review, Absent):
            return "not started"
        if isinstance(self.review, Running):
            return "running"
        if isinstance(self.review, Died):
            return "stopped"
        if isinstance(self.review, Exempt):
            return "exempt"
        if isinstance(self.review, (Unmarked, Complete)):
            return "finished"
        return self.review.raw_status or "not started"

    def list_receipt_text(self) -> str:
        """The `receipt` summary string rendered by `list` and `list --json`."""
        if isinstance(self.check, NoReceipt):
            return "no receipt"
        text = self._check_status_word(default="?")
        if self.is_active and isinstance(self.check, StaleTree):
            text += " (stale)"
        if self.post_merge_present:
            text += f", post-merge {self.post_merge_status}"
        if self.landed_present and not self.is_finished:
            text += f", {self.review_words}"
        return text

    def for_list(self, record: Record) -> dict[str, Any]:
        """The per-lane object rendered in `list --json`."""
        return dict(record, receipt=self.list_receipt_text())

    def for_show(self) -> dict[str, Any]:
        """The version-1 summary object rendered by `show --json` and `show`."""
        tree_match = self._tree_match()
        # A branch-tree mismatch proves staleness. A match cannot certify the toolchain or other
        # verification conditions without executing project code, which this read-only query never does.
        checks = {
            "status": self._check_status_word(default="unknown"),
            "current": False if tree_match is False else None,
            "tree_match": tree_match,
        }
        repo_url = f"https://github.com/{self.repository}" if self.repository else None
        return {
            "version": 1,
            "id": self.id,
            "project": self.project,
            "repository": repo_url,
            "ticket": {"number": self.ticket_raw, "url": self.ticket_url},
            "status": self.status,
            "branch": self.branch,
            "base": self.base,
            "worktree": self.worktree,
            "claims": self.claims,
            "after": self.after,
            "checks": checks,
            "agents": self.agents,
            "review": {
                "status": self._review_status_for_show(),
                "findings": self.findings_count,
                "unresolved": self.open_findings_count,
            },
        }

    def for_status(self) -> dict[str, Any]:
        """The per-lane entry rendered inside `status --json`."""
        return {
            "id": self.id,
            "status": self.status,
            "ticket": {"number": self.ticket_number, "url": self.ticket_url},
            "branch": self.branch,
            "base": self.base,
            "worktree": self.worktree,
            "claims": self.claims,
            "after": self.after,
            "created": self.created,
            "receipt": {
                "status": self._check_status_word(default="unknown"),
                "tree_match": self._tree_match(),
                "path": self.check_receipt_path,
            },
            "post_merge": self.post_merge_status if self.post_merge_present else None,
            "review": {
                "status": self._review_status_for_status(),
                "open": self.open_findings_count,
                "findings": self.findings_count,
            },
            "logs": self.logs,
            "outcome": self.outcome,
        }


def lane_view(
    store: state.Store,
    record: Record,
    *,
    tip_tree: Any = UNCHECKED,
    repository: Any = UNCHECKED,
    logs: dict[str, str] | None = None,
    prog: str | None = None,
) -> LaneView:
    """Build the read-only `LaneView` for `record` in `store` without creating files, taking locks or running hooks."""
    lane_id = str(record["id"])
    common = str(record.get("common_dir", ""))
    root = main_checkout(common) if common else ""
    branch = record.get("branch")
    branch_str = str(branch) if isinstance(branch, str) else None

    check_rec = read(store, lane_id, "receipt")
    if tip_tree is UNCHECKED:
        resolved_tree = (
            _git_read(root, "rev-parse", "--verify", "--quiet", f"{branch_str}^{{tree}}") or None
            if root and branch_str
            else None
        )
    else:
        resolved_tree = str(tip_tree) if tip_tree else None

    if repository is UNCHECKED:
        repo_slug = _origin_repository(root)
    else:
        repo_slug = str(repository) if isinstance(repository, str) and repository else None
    if repo_slug and repo_slug.startswith("https://github.com/"):
        repo_slug = repo_slug.removeprefix("https://github.com/").strip("/")

    check_v = check_verdict(record, check_rec, resolved_tree)
    check_path = str(path(store, lane_id, "receipt")) if check_rec else None

    review_rec = read(store, lane_id, "review")
    review_v = review_verdict(record, review_rec, panel_required=store.project.review_panel_required)
    _, words = review_words(lane_id, review_v, prog=prog or store.project.command)
    findings = extract_findings(review_rec)
    open_count = sum(1 for f in findings if not f.get("mark"))

    raw_post = record.get("post_merge")
    post_dict = cast(dict[str, Any], raw_post) if isinstance(raw_post, dict) else None

    ticket_raw = str(record.get("ticket", "")).lstrip("#")
    ticket_number = ticket_raw if ticket_raw and ticket_raw != "none" else None
    ticket_url = (
        f"https://github.com/{repo_slug}/issues/{ticket_raw}"
        if repo_slug and ticket_raw.isdigit()
        else None
    )
    project_name = store.project.project if store.project.project != "this project" else Path(root).name

    raw_claims: Any = record.get("claims", [])
    raw_after: Any = record.get("after", [])
    raw_agents: Any = record.get("agents", [])

    return LaneView(
        id=lane_id,
        project=project_name,
        repository=repo_slug,
        status=str(record["status"]) if record.get("status") is not None else None,
        ticket_raw=ticket_raw,
        ticket_number=ticket_number,
        ticket_url=ticket_url,
        branch=branch_str,
        base=str(record["base"]) if record.get("base") is not None else None,
        worktree=str(record["worktree"]) if record.get("worktree") is not None else None,
        claims=list(cast(list[Any], raw_claims)) if isinstance(raw_claims, list) else [],
        after=list(cast(list[Any], raw_after)) if isinstance(raw_after, list) else [],
        created=str(record["created"]) if record.get("created") is not None else None,
        agents=list(cast(list[Any], raw_agents)) if isinstance(raw_agents, list) else [],
        outcome=record.get("outcome"),
        check=check_v,
        check_receipt_path=check_path,
        post_merge_present=bool(post_dict),
        post_merge_status=post_dict.get("status") if post_dict is not None else None,
        landed_present=bool(record.get("landed")),
        is_active=lifecycle.active(record),
        is_finished=lifecycle.finished(record),
        review=review_v,
        review_words=words,
        findings_count=len(findings),
        open_findings_count=open_count,
        logs=dict(logs) if logs is not None else {},
    )
