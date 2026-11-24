"""Lane, gate and perf receipts, their verdicts, and the one lane view (fathers#71).

Every lane receipt kind (`check`/`receipt`, `first`, `post-merge`, `recheck`), batch-gate receipt (`gate`)
and perf receipt (`perf`) is read here against an explicit `state.Store` — never `config.current()`.
Review receipt transitions and verdicts live in `review_record.py`, and post-merge smoke / recheck receipt
writes live in `postmerge.py`; `receipts.write` refuses `kind in ("review", "post-merge", "recheck")`.
`results.py` keeps the tree/plan results store and platform resolution; `receipts` calls it for the environment rule.

- `check_verdict(record, receipt, tip_tree, toolchain)` -> `NoReceipt | Green | Red | StaleTree | StaleToolchain`
- `lane_view(store, record)` -> `LaneView`, rendered by `list`, `show` and `status`
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
from typing import Any, Literal, cast

from . import config, lifecycle, review_record, scope, state
from . import results as result_store
from .gitops import main_checkout
from .state import Record

Receipt = dict[str, Any]
ReceiptKind = Literal["check", "receipt", "first", "post-merge", "recheck", "review", "gate", "perf"]
Stale = review_record.Stale
review_verdict = review_record.derive_verdict

LANE_KINDS: tuple[str, ...] = ("receipt", "first", "post-merge", "recheck", "review")
ALL_KINDS: tuple[str, ...] = ("check", "receipt", "first", "post-merge", "recheck", "review", "gate", "perf")
UNCHECKED: Any = object()

# Test files by name: a tests/ directory, test-*, test_*, *.test.*, *_test.*, *-test.*.
TEST_PATH = re.compile(r"(?:^|/)(?:tests?|__tests__)/|(?:^|/)test[-_][^/]*$|[._-]tests?\.[^/]+$")

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


def read(
    store: state.Store,
    name: str,
    kind: ReceiptKind = "receipt",
    *,
    lane_created: str | None = None,
    record: Record | None = None,
) -> Receipt | None:
    """Read a receipt from `store` without creating directories; `None` when absent, unreadable, or from an older
    lane incarnation (fathers#54)."""
    data = state.read_json(path(store, name, kind))
    if data is None:
        return None
    if kind == "check" or kind in LANE_KINDS:
        expected = lane_created if lane_created is not None else state.lane_created_of(record)
        if not state.receipt_matches_lane(data, expected):
            return None
    return data


def write(store: state.Store, name: str, data: Receipt, kind: ReceiptKind = "receipt") -> Path:
    """Atomically write a check, first, gate or perf receipt in `store` and return its path."""
    if kind == "review":
        raise ValueError("review receipts must be written through lanefather.review_record")
    if kind in ("post-merge", "recheck"):
        raise ValueError(f"{kind} receipts must be written through lanefather.postmerge")
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
    project: config.Config = config.DEFAULT,
) -> bool:
    """Whether `receipt` ran under `toolchain` and `platform` (this host's by default)."""
    if not receipt:
        return False
    expected = platform or result_store.host_platform(project)
    return (
        receipt.get("toolchain") == toolchain
        and result_store.receipt_platform(receipt, worktree, project=project) == expected
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
    created = state.lane_created_of(record)
    existing = read(store, lane_id, "first")
    if existing and existing.get("lane_created") == record.get("created"):
        return existing
    if previous is not None and not state.receipt_matches_lane(previous, created):
        previous = None
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


def lane_ran(
    store: state.Store,
    lane_id: str,
    *,
    lane_created: str | None = None,
    record: Record | None = None,
) -> tuple[set[str], set[str], set[str]]:
    """Checks, suites, and whole-run suites recorded in `lane_id`'s check and post-merge receipts."""
    ran_checks: set[str] = set()
    suites: set[str] = set()
    whole: set[str] = set()
    for kind in ("receipt", "post-merge"):
        found = read(store, lane_id, kind, lane_created=lane_created, record=record) or {}
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


def changed_red_tests(lane_plan: dict[str, Any], first: dict[str, Any]) -> list[dict[str, Any]]:
    """Test files the lane changed against its base (the check's plan) whose check was red in its first receipt."""
    raw_failed: Any = first.get("failed_checks") or []
    failed: set[str] = {str(c) for c in cast(list[Any], raw_failed)} if isinstance(raw_failed, list) else set()
    if not failed:
        return []
    raw_rows: Any = lane_plan.get("checks")
    if raw_rows is None:
        raw_paths: Any = lane_plan.get("paths") or []
        paths = [str(p) for p in cast(list[Any], raw_paths)] if isinstance(raw_paths, list) else []
        return [{"path": p, "check": None} for p in paths if TEST_PATH.search(p)]
    rows = [cast(dict[str, Any], r) for r in cast(list[Any], raw_rows) if isinstance(r, dict)]
    return [
        {"path": str(p), "check": str(row["id"])}
        for row in rows
        if str(row.get("id")) in failed
        for p in (cast(list[Any], row["paths"]) if isinstance(row.get("paths"), list) else [])
        if TEST_PATH.search(str(p))
    ]


def changed_red_tests_line(tests: list[dict[str, Any]]) -> str:
    named = ", ".join(f"{t['path']} ({t['check']})" if t["check"] else str(t["path"]) for t in tests)
    return (f"lane: test files the lane changed whose check was red in its first receipt: {named}. The reviewer "
            "confirms each change fixes the test rather than hides a regression (#568)")


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
    blocked: tuple[str, ...] = ()  # dependency-blocked checks: they did not run (fathers#195)
    prerequisites: tuple[str, ...] = ()  # the checks that ran and failed behind them


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
    project: config.Config = config.DEFAULT,
) -> CheckVerdict:
    """Evaluate a lane check receipt against `tip_tree` and, when given, `toolchain` and `platform`."""
    if not receipt or not state.receipt_matches_lane(receipt, state.lane_created_of(record)):
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
        blocked_entries = (
            [
                cast(dict[str, Any], e)
                for e in cast(list[Any], raw_cls_checks)
                if isinstance(e, dict) and cast(dict[str, Any], e).get("class") == "dependency-blocked"
            ]
            if isinstance(raw_cls_checks, list)
            else []
        )
        behind: list[str] = []
        for e in blocked_entries:
            raw_behind: Any = e.get("prerequisites") or e.get("blocked_by") or []
            for name in cast(list[Any], raw_behind) if isinstance(raw_behind, list) else []:
                if str(name) not in behind:
                    behind.append(str(name))
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
            blocked=tuple(str(e["check"]) for e in blocked_entries if "check" in e),
            prerequisites=tuple(behind),
            rerun_alone=rerun_alone,
            classification_base=cls_base,
            tree=tree_str,
            tree_match=matched,
            status=status_str,
        )
    if toolchain is not UNCHECKED:
        worktree = record.get("worktree") if record else None
        expected_platform = platform or result_store.host_platform(project)
        toolchain_ok = receipt.get("toolchain") == toolchain
        platform_ok = result_store.receipt_platform(receipt, worktree, project=project) == expected_platform
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
    platform: str | None = None,
    project: config.Config = config.DEFAULT,
) -> str | None:
    """Why `receipt` cannot stand for part of its merge's smoke (#640), or `None` when it can."""
    effective_record: Record | None = (
        {**record, "worktree": worktree}
        if worktree is not None and record is not None
        else {"worktree": worktree}
        if worktree is not None
        else record
    )
    verdict = check_verdict(effective_record, receipt, str(landed["tree"]), toolchain,
                            platform=platform, project=project)
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
    # Every ticket of the lane's scope (fathers#192), beside `ticket`, the primary one older readers use.
    tickets: list[dict[str, Any]]
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
    review: review_record.ReviewVerdict
    review_words: str
    findings_count: int
    open_findings_count: int
    logs: dict[str, str]
    review_pre_land: bool = False
    review_tip: str | None = None
    review_pre_land_current: bool = True
    review_has_delta: bool = False

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
        if isinstance(self.review, review_record.Absent):
            return "not started"
        if isinstance(self.review, (review_record.Running, review_record.Died)):
            return "running"
        if isinstance(self.review, review_record.Exempt):
            return "exempt"
        if isinstance(self.review, review_record.Stale):
            return "stale"
        if isinstance(self.review, (review_record.PartialPanel, review_record.Unmarked, review_record.Complete)):
            return "finished"
        return self.review.raw_status or "not started"

    def _review_status_for_status(self) -> str:
        if isinstance(self.review, review_record.Absent):
            return "not started"
        if isinstance(self.review, review_record.Running):
            return "running"
        if isinstance(self.review, review_record.Died):
            return "stopped"
        if isinstance(self.review, review_record.Exempt):
            return "exempt"
        if isinstance(self.review, review_record.Stale):
            return "stale"
        if isinstance(self.review, (review_record.PartialPanel, review_record.Unmarked, review_record.Complete)):
            return "finished"
        return self.review.raw_status or "not started"

    def pre_land_reviewed_text(self) -> str | None:
        """Short summary for a lane with a completed pre-land review (fathers#242, fathers#53)."""
        if self.review_pre_land and self.review_tip and not self.review_has_delta:
            if isinstance(self.review, review_record.Complete):
                return f"reviewed before land at {self.review_tip[:10]}"
            if self.is_active and isinstance(self.review, review_record.Stale):
                return f"reviewed before land at {self.review_tip[:10]} (stale)"
        return None

    def list_receipt_text(self) -> str:
        """The `receipt` summary string rendered by `list` and `list --json`."""
        pre_land_text = self.pre_land_reviewed_text()
        stale_active = self.is_active and isinstance(self.review, review_record.Stale)
        if isinstance(self.check, NoReceipt):
            if self.is_active and pre_land_text:
                return f"no receipt, {pre_land_text}"
            if stale_active:
                return f"no receipt, {self.review_words}"
            return "no receipt"
        text = self._check_status_word(default="?")
        if self.is_active and isinstance(self.check, StaleTree):
            text += " (stale)"
        if self.post_merge_present:
            text += f", post-merge {self.post_merge_status}"
        if self.landed_present and not self.is_finished:
            text += f", {pre_land_text or self.review_words}"
        elif self.is_active and pre_land_text:
            text += f", {pre_land_text}"
        elif stale_active:
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
        review_block: dict[str, Any] = {
            "status": self._review_status_for_show(),
            "findings": self.findings_count,
            "unresolved": self.open_findings_count,
        }
        if self.review_pre_land:
            review_block["pre_land"] = True
            if self.review_tip:
                review_block["tip"] = self.review_tip
            pre_land_text = self.pre_land_reviewed_text()
            if pre_land_text:
                review_block["summary"] = pre_land_text
        if isinstance(self.review, review_record.Stale):
            review_block["reviewed_tip"] = self.review.reviewed_tip
            review_block["landed_tip"] = self.review.landed_tip
            review_block.setdefault("summary", self.review_words)
        return {
            "version": 1,
            "id": self.id,
            "project": self.project,
            "repository": repo_url,
            "ticket": {"number": self.ticket_raw, "url": self.ticket_url},
            "tickets": self.tickets,
            "status": self.status,
            "branch": self.branch,
            "base": self.base,
            "worktree": self.worktree,
            "claims": self.claims,
            "after": self.after,
            "checks": checks,
            "agents": self.agents,
            "review": review_block,
        }

    def for_status(self) -> dict[str, Any]:
        """The per-lane entry rendered inside `status --json`."""
        review_block: dict[str, Any] = {
            "status": self._review_status_for_status(),
            "open": self.open_findings_count,
            "findings": self.findings_count,
        }
        if self.review_pre_land:
            review_block["pre_land"] = True
            if self.review_tip:
                review_block["tip"] = self.review_tip
            pre_land_text = self.pre_land_reviewed_text()
            if pre_land_text:
                review_block["summary"] = pre_land_text
        if isinstance(self.review, review_record.Stale):
            review_block["reviewed_tip"] = self.review.reviewed_tip
            review_block["landed_tip"] = self.review.landed_tip
            review_block.setdefault("summary", self.review_words)
        return {
            "id": self.id,
            "status": self.status,
            "ticket": {"number": self.ticket_number, "url": self.ticket_url},
            "tickets": self.tickets,
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
            "review": review_block,
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

    check_rec = read(store, lane_id, "receipt", record=record)
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

    rev = review_record.ReviewRecord.load(store, lane_id, record=record)
    review_v = rev.verdict
    _, words = rev.summary(prog=prog or store.project.command)
    findings = rev.findings
    open_count = len(rev.open_findings)
    pre_land_current = not isinstance(review_v, review_record.Stale)

    raw_post = record.get("post_merge")
    post_dict = cast(dict[str, Any], raw_post) if isinstance(raw_post, dict) else None
    post_status = post_dict.get("status") if post_dict is not None else None
    if post_status == "running":
        from . import postmerge

        post_status = postmerge.effective_status(record)

    ticket_raw = str(record.get("ticket", "")).lstrip("#")
    ticket_number = ticket_raw if ticket_raw and ticket_raw != "none" else None
    ticket_url = (
        f"https://github.com/{repo_slug}/issues/{ticket_raw}"
        if repo_slug and ticket_raw.isdigit()
        else None
    )
    tickets = [
        {
            "number": issue.number,
            "url": (f"https://github.com/{repo_slug}/issues/{issue.number}"
                    if repo_slug and issue.number.isdigit() else None),
            "partial": issue.partial,
        }
        for issue in scope.issues(record)
    ]
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
        tickets=tickets,
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
        post_merge_status=post_status,
        landed_present=bool(record.get("landed")),
        is_active=lifecycle.active(record),
        is_finished=lifecycle.finished(record),
        review=review_v,
        review_words=words,
        findings_count=len(findings),
        open_findings_count=open_count,
        logs=dict(logs) if logs is not None else {},
        review_pre_land=rev.is_pre_land,
        review_tip=rev.reviewed_tip or None,
        review_pre_land_current=pre_land_current,
        review_has_delta=rev.matching_delta(record) is not None,
    )
