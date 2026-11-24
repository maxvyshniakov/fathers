"""The check contract: what a project's verify command and plan module answer (#649, #422, #640).

  <verify> --changed --base B --plan --json   the plan: {full, paths, unowned, checks: [{id, suite, paths,
                                             producer}], suites, reasons, capabilities?}; a tree from before the
                                             verification graph has no `checks` and names whole suites
  <verify> --checks A,B [--only T1,T2] | --suites S,T [--keep-going] --jobs N
                                             a run; it prints `Verification report: <path>` to a report.json of
                                             {checks: [{id, status, log, blocked_by, failed_tests?}], stopped,
                                             environment, capabilities?}
  <verify> (no selection)                    the full gate, which stamps the tree (`Verified tree <tree> stamped`)

The plan module (`plan_module`, loaded from the tree it plans) answers `SUITES`, `checks(root, scratch, suites,
keep_going, native_only, jobs, bundle=...)` with objects carrying .id and .suite, and `smoke(root)`, the check ids
of the post-merge smoke. `PERF = "<suite>"` in it names the perf suite; `CAPABILITIES` (or `SUPPORTS_ONLY`) can
advertise `"only"` subset reruns (fathers#147). Each answer of a tree without the module is None, and lanefather falls
back to whole suites.
"""
from __future__ import annotations

from collections.abc import Iterable
import fnmatch
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
from types import ModuleType
from typing import Any, cast

from . import config, gitops, receipts, state
from . import results as result_store
from .errors import LaneError

Rows = list[dict[str, Any]]
UNRESOLVED: Any = object()
_IMPORT_LOCK = threading.Lock()


class Checkout:
    """One checkout's verification plan adapter, caching its plan module, check definitions and toolchain."""

    def __init__(self, project: config.Config, root: Any) -> None:
        self.project = project
        self.root = Path(root)
        self._module_loaded = False
        self._module: ModuleType | None = None
        self._all_checks_loaded = False
        self._all_checks: list[Any] | None = None
        self._smoke_loaded = False
        self._smoke_ids: list[str] | None = None
        self._toolchain_loaded = False
        self._toolchain: dict[str, Any] | None = None

    def verify_path(self) -> str:
        return os.path.join(self.root, self.project.verify)

    def module(self) -> ModuleType | None:
        if not self._module_loaded:
            self._module = _load_module(self.root, self.project)
            self._module_loaded = True
        return self._module

    def all_checks(self) -> list[Any] | None:
        if not self._all_checks_loaded:
            found = self.module()
            try:
                self._all_checks = (
                    list(found.checks(self.root, Path("/nonexistent"), set(found.SUITES), True, False, 1, bundle=False))
                    if found is not None
                    else None
                )
            except Exception:
                self._all_checks = None
            self._all_checks_loaded = True
        return self._all_checks

    def smoke_checks(self) -> list[str] | None:
        if not self._smoke_loaded:
            found = self.module()
            try:
                self._smoke_ids = (
                    list(found.smoke(self.root)) if found is not None and hasattr(found, "smoke") else None
                )
            except Exception:
                self._smoke_ids = None
            self._smoke_loaded = True
        return self._smoke_ids

    def known_checks(self) -> set[str] | None:
        every = self.all_checks()
        return {str(c.id) for c in every} if every is not None else None

    def suite_owners(self, suites: Iterable[str]) -> dict[str, set[str]] | None:
        found = self.module()
        try:
            return (
                {suite: {str(c.id) for c in found.checks(self.root, Path("/nonexistent"), {suite}, True, False, 1)}
                 for suite in suites}
                if found is not None
                else None
            )
        except Exception:
            return None

    def smoke_rows(self, why: dict[str, list[str]] | None = None) -> Rows | None:
        ids = self.smoke_checks()
        found = self.module()
        every = self.all_checks()
        if not ids or found is None or every is None:
            return None
        suite = {str(check.id): str(check.suite) for check in every}
        order = {str(s): n for n, s in enumerate(found.SUITES)}
        paths = why or {}
        rows: Rows = [{"id": i, "suite": suite[i], "paths": paths.get(i, [])} for i in ids if i in suite]
        return sorted(rows, key=lambda row: order.get(str(row["suite"]), len(order)))

    def supports_only(self, *, lane_plan: dict[str, Any] | None = None,
                      report: dict[str, Any] | None = None) -> bool:
        if report and _has_only_capability(report.get("capabilities")):
            return True
        if lane_plan and _has_only_capability(lane_plan.get("capabilities")):
            return True
        found = self.module()
        if found is None:
            return False
        return bool(getattr(found, "SUPPORTS_ONLY", False)
                    or _has_only_capability(getattr(found, "CAPABILITIES", None)))

    def plan_for(self, base: str) -> dict[str, Any]:
        return plan_for(self, base, self.project)

    def post_merge_selection(self, merged_plan: dict[str, Any]) -> tuple[str, Rows | None]:
        return post_merge_selection(self, merged_plan, self.project)

    def toolchain(self) -> dict[str, Any] | None:
        if not self._toolchain_loaded:
            self._toolchain = _resolve_toolchain(self.root, self.project)
            self._toolchain_loaded = True
        return self._toolchain

    def triage(self, run: Path) -> str | None:
        return triage(self.root, run, self.project)


def _as_checkout(target: Checkout | Any, project: config.Config = config.DEFAULT) -> Checkout:
    return target if isinstance(target, Checkout) else Checkout(project, target)


def verify_path(root: Any, project: config.Config = config.DEFAULT) -> str:
    if isinstance(root, Checkout):
        return root.verify_path()
    return os.path.join(root, project.verify)


def verifier_error(path: str, error: OSError) -> LaneError:
    """Clear refusal when the configured `[checks].verify` script or command cannot be executed."""
    if isinstance(error, FileNotFoundError) and not os.path.exists(path):
        return LaneError(f"verifier not found: {path} (configure [checks].verify in {config.CONFIG_NAME})")
    return LaneError(
        f"cannot run verifier {path}: {error.strerror or error} (configure [checks].verify in {config.CONFIG_NAME})"
    )


def declared_slots(row: dict[str, Any]) -> int | None:
    """Minimum slots a check row declares (`min_slots` or `slots`, fathers#173), or None."""
    for key in ("min_slots", "slots"):
        value = row.get(key)
        if type(value) is int and value > 0:
            return value
    return None


def required_min_slots(
    lane_plan: dict[str, Any],
    remaining: dict[str, list[str]] | None,
    suites: list[str],
    *,
    project: config.Config = config.DEFAULT,
) -> tuple[int, list[str]]:
    """The minimum slot floor declared by the checks/suites that will actually run (fathers#173), from the plan's
    per-check `min_slots`/`slots` fields or `.lanefather.toml` `[checks.min_slots]` patterns."""
    needs: dict[str, int] = {}
    patterns = project.min_slots
    if remaining is None:
        top = declared_slots(lane_plan)
        if top is not None and suites:
            needs["plan"] = top
        for suite in suites:
            for pattern, count in patterns:
                if fnmatch.fnmatchcase(suite, pattern):
                    needs[suite] = max(needs.get(suite, 0), count)
    else:
        running = {c for ids in remaining.values() for c in ids}
        for row in lane_plan.get("selection") or ():
            cid = str(row["id"])
            if cid not in running:
                continue
            declared = declared_slots(row)
            if declared is not None:
                needs[cid] = max(needs.get(cid, 0), declared)
            for pattern, count in patterns:
                if fnmatch.fnmatchcase(cid, pattern) or fnmatch.fnmatchcase(str(row["suite"]), pattern):
                    needs[cid] = max(needs.get(cid, 0), count)
    if not needs:
        return 1, []
    floor = max(needs.values())
    return floor, [name for name, count in needs.items() if count == floor]


def _has_only_capability(value: Any) -> bool:
    if isinstance(value, dict):
        return bool(cast(dict[Any, Any], value).get("only"))
    if isinstance(value, (list, tuple, set)):
        return "only" in cast(Any, value)
    return False


def supports_only(root: Any, *, lane_plan: dict[str, Any] | None = None,
                  report: dict[str, Any] | None = None,
                  project: config.Config = config.DEFAULT) -> bool:
    """Whether the project's verifier advertises `--only <tests>` subset reruns (fathers#147)."""
    return _as_checkout(root, project).supports_only(lane_plan=lane_plan, report=report)


def plan_for(root: Any, base: str, project: config.Config = config.DEFAULT) -> dict[str, Any]:
    """The tree's own verification plan since `base`.

    `selection` lists the checks it selects, [{id, suite, paths}] in plan order, each with the changed paths that
    chose it: every check of the gate for a full plan, and None for a tree whose plan names whole suites. `selected`
    names the suites those checks belong to in the tree's order, `by_suite` the checks of each."""
    inspector = _as_checkout(root, project)
    verifier = verify_path(inspector.root, inspector.project)
    argv = [verifier, "--changed", "--base", base, "--plan", "--json"]
    try:
        result = subprocess.run(argv, cwd=inspector.root, capture_output=True, text=True)
    except OSError as error:
        raise verifier_error(verifier, error) from error
    if result.returncode:
        raise LaneError(f"{inspector.project.verify} --changed --plan in {inspector.root} failed: "
                        f"{result.stderr.strip()}")
    plan: dict[str, Any] = json.loads(result.stdout)
    rows = plan.get("checks")
    if rows is None:
        plan.update(selection=None, selected=list(plan["suites"]), by_suite=None)
        return plan
    # A full plan (a path no check reads, or a change that selects every check) runs every check of the gate; its
    # rows that no changed path chose run for the paths no check reads, or for every changed path.
    full_why = plan.get("unowned") or plan["paths"]
    selection: Rows = [{"id": row["id"], "suite": row["suite"],
                        "paths": row["paths"] or (full_why if plan.get("full") else []),
                        **({"min_slots": need} if (need := declared_slots(row)) is not None else {})}
                       for row in rows if plan.get("full") or not row["producer"]]
    plan["selection"] = selection
    plan["selected"] = suites_of(selection)
    plan["by_suite"] = by_suite(selection)
    return plan


def suites_of(rows: Rows) -> list[str]:
    """The suites of selected checks, in the order their first checks come."""
    return list(dict.fromkeys(row["suite"] for row in rows))


def by_suite(rows: Rows | None) -> dict[str, list[str]] | None:
    """The selected checks of each suite, in plan order; None for a selection of whole suites."""
    if rows is None:
        return None
    return {suite: [row["id"] for row in rows if row["suite"] == suite] for suite in suites_of(rows)}


def _load_module(root: Path, project: config.Config) -> ModuleType | None:
    relative = project.plan_module
    if not relative:
        return None
    try:
        path = root / relative
        if not path.is_file():
            return None
        loader = importlib.machinery.SourceFileLoader(f"lane_plan_{abs(hash(str(path)))}", str(path))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        assert spec is not None
        found: ModuleType = importlib.util.module_from_spec(spec)
        parent = str(path.parent)
        with _IMPORT_LOCK:
            sys.path.insert(0, parent)
            try:
                loader.exec_module(found)
            finally:
                if parent in sys.path:
                    sys.path.remove(parent)
        return found
    except Exception:
        return None


def module(root: Any, project: config.Config = config.DEFAULT) -> Any:
    """The tree's own plan module; None for a tree without it."""
    return _as_checkout(root, project).module()


def suite_owners(root: Any, suites: Iterable[str],
                 project: config.Config = config.DEFAULT) -> dict[str, set[str]] | None:
    """Each suite's check ids, from the tree's own plan module; None for a tree without it."""
    return _as_checkout(root, project).suite_owners(suites)


def smoke_checks(root: Any, project: config.Config = config.DEFAULT) -> list[str] | None:
    """The tree's post-merge smoke (smoke() of the plan module), as check ids; None for a tree without it."""
    return _as_checkout(root, project).smoke_checks()


def known_checks(root: Any, project: config.Config = config.DEFAULT) -> set[str] | None:
    """The check ids the tree's plan module defines; None for a tree without one."""
    return _as_checkout(root, project).known_checks()


def post_merge_selection(root: Any, plan: dict[str, Any],
                         project: config.Config = config.DEFAULT) -> tuple[str, Rows | None]:
    """What the post-merge check of a merge runs, given the merged diff's plan (plan_for since the previous tip):
    ("plan", its selection) when the diff selects only structure checks (documentation), otherwise ("smoke", the
    tree's smoke, #446). Each row is {id, suite, paths}: the merged paths that select the check, or [] for a smoke
    check the diff does not select, which runs because the smoke runs everything that builds. A tree without a smoke
    runs the plan."""
    selection = plan["selection"]
    if set(plan["selected"]) <= {"structure"}:
        return "plan", selection
    inspector = _as_checkout(root, project)
    why = {str(row["id"]): list(row["paths"]) for row in selection or ()}
    rows = inspector.smoke_rows(why)
    if rows is None:
        return "plan", selection
    return "smoke", rows


def _resolve_toolchain(root: Path, project: config.Config) -> dict[str, Any] | None:
    if not project.toolchain or not project.toolchain_pin or not (root / project.toolchain_pin).is_file():
        return None
    argv = [sys.executable if part == "python3" else part for part in project.toolchain]
    if len(argv) > 1 and not (root / argv[1]).is_file() and argv[0] == sys.executable:
        return None
    try:
        result = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as error:
        return {"refused": str(error)}
    if result.returncode:
        return {"refused": (result.stdout + result.stderr).strip()}
    try:
        return json.loads(result.stdout)
    except ValueError:
        return {"refused": f"unreadable resolution: {result.stdout.strip()[:200]}"}


def toolchain_state(root: Any, project: config.Config = config.DEFAULT) -> dict[str, Any] | None:
    """The toolchain the tree's resolver reports now (#571): its resolution, {"refused": why} when it refuses, None
    for a tree that pins none. A receipt certifies only the toolchain it ran with."""
    return _as_checkout(root, project).toolchain()


def toolchain_for_rev(project: config.Config, cwd: str, rev: str) -> dict[str, Any] | None:
    """The toolchain a result on REV's tree depends on, resolved in this checkout, which must pin what REV pins: a
    checkout of another pin would resolve another toolchain (review of fathers#91)."""
    top = gitops.git(cwd, "rev-parse", "--show-toplevel", check=False) or cwd
    pin = project.toolchain_pin
    if pin and project.toolchain:
        here = Path(top) / pin
        theirs = gitops.git(top, "show", f"{rev}:{pin}", check=False)
        if (here.read_text().strip() if here.is_file() else "") != theirs.strip():
            raise LaneError(f"results: {rev} pins another toolchain ({pin}) than this checkout; run in a checkout of "
                            f"{rev}")
    return toolchain_state(top, project)


def receipt_paths(receipt: dict[str, Any]) -> set[str]:
    """The paths a receipt's plan covered: its `paths`, or for a receipt from before them, the paths that chose its
    checks."""
    listed: Any = receipt.get("paths")
    if isinstance(listed, list):
        return {str(p) for p in cast(list[Any], listed)}
    rows: list[dict[str, Any]] = receipt.get("selection") or []
    reasons: dict[str, list[str]] = receipt.get("reasons") or {}
    return {p for row in rows for p in row.get("paths", [])} | {p for paths in reasons.values() for p in paths}


def fix_plan(lane_plan: dict[str, Any], parent: dict[str, Any], known: set[str] | None = None) -> dict[str, Any]:
    """A review-fix lane's plan (fathers#93), changed in place: its own diff's plan when every path it changes is in
    the parent lane's diff; otherwise the plan of both diffs, the union of the two selections. Returns what the
    receipt records about it."""
    parent_paths = receipt_paths(parent)
    outside = sorted(p for p in lane_plan["paths"] if p not in parent_paths)
    union = bool(outside) if lane_plan.get("selection") is not None else (None if outside else False)
    about: dict[str, Any] = {"lane": parent["lane"], "tree": receipts.tree_of(parent), "outside_parent": outside,
                             "union": union}
    if not union:
        return about
    rows = {row["id"]: row for row in lane_plan["selection"]}
    added = [row for row in parent["selection"] if row["id"] not in rows and (known is None or row["id"] in known)]
    dropped = [row["id"] for row in parent["selection"] if known is not None and row["id"] not in known]
    lane_plan["selection"] = [*lane_plan["selection"],
                              *({"id": r["id"], "suite": r["suite"], "paths": r.get("paths", []),
                                 **({"min_slots": need} if (need := declared_slots(r)) is not None else {})}
                                for r in added)]
    lane_plan["selected"] = suites_of(lane_plan["selection"])
    lane_plan["by_suite"] = by_suite(lane_plan["selection"])
    about["added"] = [r["id"] for r in added]
    if dropped:
        about["dropped"] = dropped
    return about


def reuse_from_tree(
    store: state.Store,
    wanted: dict[str, str],
    tree: str,
    toolchain: Any,
    *,
    platform: str | None = None,
) -> dict[str, dict[str, Any]]:
    """The wanted checks ({id: suite}) whose newest result on `tree`, under `toolchain`, passed (fathers#91), as
    reused check results naming the run that stands for them."""
    if not wanted:
        return {}
    stored = result_store.passed(store, tree, toolchain, platform=platform)

    def standing(result: dict[str, Any]) -> dict[str, Any]:
        origin: dict[str, Any] = result.get("origin") or {}
        return {**result, **origin}

    return {
        c: {
            "suite": suite,
            "status": "passed",
            "log": standing(stored[c]).get("receipt", ""),
            "reused_from": tree,
            "reused_by": "tree",
            **{
                f"reused_{k}": standing(stored[c])[k]
                for k in ("source", "receipt", "lane", "at")
                if standing(stored[c]).get(k)
            },
        }
        for c, suite in wanted.items()
        if c in stored
    }


def reuse_from_receipt(
    checkout: Checkout | Any,
    source_receipt: dict[str, Any] | None,
    wanted: set[str],
    *,
    toolchain: Any,
    platform: str | None = None,
    worktree: Any = None,
    reused_by: str | None = None,
    lane: str | None = None,
    receipt_path: str | None = None,
    project: config.Config = config.DEFAULT,
) -> dict[str, dict[str, Any]]:
    """Passed check results from `source_receipt` (a lane's previous receipt or a hand-over fix lane's parent
    receipt) that no change since `source_receipt['head']` selects."""
    if not wanted or not source_receipt:
        return {}
    inspector = _as_checkout(checkout, project)
    head = source_receipt.get("head")
    if not isinstance(head, str) or not head:
        return {}
    if not receipts.same_environment(
        source_receipt, toolchain, worktree=worktree, platform=platform, project=inspector.project
    ):
        return {}
    if not gitops.git_ok(inspector.root, "cat-file", "-e", head):
        return {}
    delta = inspector.plan_for(head)
    found = receipts.reusable_checks(source_receipt, delta)
    reused: dict[str, dict[str, Any]] = {}
    for check_id, entry in found.items():
        if check_id not in wanted:
            continue
        item = dict(entry)
        if reused_by is not None:
            item["reused_by"] = reused_by
        if lane is not None:
            item["reused_lane"] = item.get("reused_lane") or lane
        if receipt_path is not None:
            item["reused_receipt"] = item.get("reused_receipt") or receipt_path
        reused[check_id] = item
    return reused


def exact_tree_reuse(
    checkout: Checkout | Any,
    receipt: dict[str, Any] | None,
    landed: dict[str, Any],
    merge_tree: str,
    previous: str,
    toolchain: Any = UNRESOLVED,
    worktree: Any = None,
    *,
    platform: str | None = None,
    project: config.Config = config.DEFAULT,
) -> tuple[dict[str, dict[str, Any]] | None, dict[str, Any] | None, dict[str, Any], str | None]:
    """Check whether the lane's green receipt already covers the exact merge tree before running `plan_for`
    (fathers#198). A consumer whose plan adapter bootstraps dependencies (such as bun install under a heavy slot)
    would otherwise wait in the slot queue only to discover that nothing needs to run.

    Returns (lane_verified, narrowing, reasons, fallback_reason). When `lane_verified` is not None, every check the
    post-merge selection would run already passed on this exact merge tree under the same toolchain and platform;
    otherwise `fallback_reason` names why plan preparation is still required."""
    inspector = _as_checkout(checkout, project)
    resolved_tc = inspector.toolchain() if toolchain is UNRESOLVED else toolchain
    reason = receipts.smoke_basis(
        None, receipt, landed, resolved_tc, worktree=worktree, platform=platform, project=inspector.project
    )
    receipt_tree = receipts.tree_of(receipt)
    if reason or receipt is None or receipt_tree is None:
        return None, None, {}, reason or "the lane has no receipt"
    if merge_tree != receipt_tree:
        return None, None, {}, f"merge tree {merge_tree[:10]} differs from lane receipt {receipt_tree[:10]}"
    raw_results: Any = receipt.get("check_results") or {}
    results = cast(dict[str, dict[str, Any]], raw_results) if isinstance(raw_results, dict) else {}
    passed = {c for c, e in results.items() if receipts.status_of(e) == "passed"}
    receipt_base = receipt.get("base")
    same_base = bool(receipt_base) and (
        receipt_base == previous
        or (gitops.git_ok(inspector.root, "cat-file", "-e", f"{receipt_base}^{{tree}}")
            and gitops.git(inspector.root, "rev-parse", f"{previous}^{{tree}}")
            == gitops.git(inspector.root, "rev-parse", f"{receipt_base}^{{tree}}")))
    ids = inspector.smoke_checks()
    found = inspector.module()
    rows: Rows
    if ids and found is not None:
        if (same_base and isinstance(receipt.get("suites"), list)
                and set(receipt["suites"]) <= {"structure"} and receipt.get("selection") is not None):
            rows = list(receipt["selection"])
        else:
            smoke_list = inspector.smoke_rows()
            if smoke_list is None:
                return None, None, {}, "plan module could not list smoke checks"
            rows = smoke_list
            if not same_base:
                # Another base: the merged diff could select checks (a structure-only plan) the smoke does not
                # name. For a scoped receipt it selects nothing the receipt's plan did not, provided it touches no
                # path that plan left out; for both scoped and full receipts, every check in the receipt's own
                # selection must have passed as well (a green receipt may carry flaky/pre-existing failures).
                if not receipt.get("full"):
                    merged = {p for p in gitops.git(
                        inspector.root, "-c", "core.quotePath=false", "diff", "--no-renames", "--name-only",
                        previous, merge_tree, "--",
                    ).splitlines() if p}
                    outside = sorted(merged - receipt_paths(receipt))
                    if outside or receipt.get("selection") is None:
                        listed = ", ".join(outside[:3]) + ("..." if len(outside) > 3 else "")
                        return None, None, {}, ("merge base differs from the lane receipt's base"
                                                + (f" and the merge changes {listed}, which its plan did not cover"
                                                   if outside else ""))
                elif receipt.get("selection") is None:
                    return None, None, {}, "merge base differs from the lane receipt's base"
                named = {str(row["id"]) for row in rows}
                rows = rows + [row for row in receipt["selection"] if str(row["id"]) not in named]
    else:
        if receipt.get("selection") is None:
            return None, None, {}, "the lane's receipt has no per-check selection"
        if not receipt.get("full") and not same_base:
            return None, None, {}, "merge base differs from the lane receipt's base"
        rows = list(receipt["selection"])
    missing = [str(row["id"]) for row in rows if str(row["id"]) not in passed]
    if missing:
        listed = ", ".join(missing[:3]) + ("..." if len(missing) > 3 else "")
        return None, None, {}, f"lane receipt did not verify {listed}"
    lane_verified = {
        str(row["id"]): dict(results[str(row["id"])],
                             reused_from=results[str(row["id"])].get("reused_from") or receipt_tree)
        for row in rows
    }
    narrowing = {"receipt_tree": receipt_tree, "receipt_head": receipt.get("head"), "lane_tip": landed["tip"],
                 "narrowed": True, "integration_paths": [], "dropped": list(lane_verified)}
    return lane_verified, narrowing, dict(receipt.get("reasons") or {}), None


def narrow_smoke(
    checkout: Checkout | Any,
    receipt: dict[str, Any] | None,
    landed: dict[str, Any],
    merge_tree: str,
    kind: str,
    rows: Rows | None,
    worktree: Any = None,
    *,
    toolchain: Any = UNRESOLVED,
    platform: str | None = None,
    project: config.Config = config.DEFAULT,
) -> tuple[str, Rows | None, dict[str, Any], dict[str, dict[str, Any]]]:
    """The part of the post-merge selection `rows` that the merge still needs after the lane's green receipt
    (#640): (kind, rows, narrowing, lane_verified)."""
    if rows is None:
        return kind, rows, {"narrowed": False, "reason": "the tree's plan names whole suites"}, {}
    inspector = _as_checkout(checkout, project)
    resolved_tc = inspector.toolchain() if toolchain is UNRESOLVED else toolchain
    reason = receipts.smoke_basis(
        None, receipt, landed, resolved_tc, worktree=worktree, platform=platform, project=inspector.project
    )
    receipt_tree = receipts.tree_of(receipt)
    if reason or receipt is None or receipt_tree is None:
        return kind, rows, {"narrowed": False, "reason": reason}, {}
    basis = {"receipt_tree": receipt_tree, "receipt_head": receipt.get("head"), "lane_tip": landed["tip"]}
    paths: list[str]
    if merge_tree == receipt_tree:
        brought: dict[str, list[str]] = {}
        paths = []
    else:
        delta = inspector.plan_for(str(landed["tip"]))
        paths = list(delta["paths"])
        if delta.get("full") or delta["selection"] is None:
            return kind, rows, dict(basis, narrowed=False, integration_paths=paths,
                                    reason="what the integration brought in selects the full gate"), {}
        brought = {str(row["id"]): list(row["paths"]) for row in delta["selection"]}
    raw_results: Any = receipt.get("check_results") or {}
    results = cast(dict[str, dict[str, Any]], raw_results) if isinstance(raw_results, dict) else {}
    passed = {c for c, e in results.items() if receipts.status_of(e) == "passed"}
    kept: Rows = []
    for row in rows:
        cid = str(row["id"])
        if cid in brought:
            kept.append(dict(row, paths=brought[cid]))
        elif cid not in passed:
            unverified = receipts.status_of(results.get(cid), "not selected")
            kept.append(dict(row, unverified=unverified))
    running = {str(row["id"]) for row in kept}
    lane_verified = {
        str(row["id"]): dict(results[str(row["id"])],
                             reused_from=results[str(row["id"])].get("reused_from") or receipt_tree)
        for row in rows if str(row["id"]) not in running
    }
    narrowing = dict(basis, narrowed=True, integration_paths=paths, dropped=list(lane_verified))
    return ("narrowed" if kept else "skip"), kept, narrowing, lane_verified


def smoke_reuse(
    store: state.Store,
    kind: str,
    rows: Rows | None,
    merge_tree: str,
    toolchain: Any,
    merged_plan: dict[str, Any] | None = None,
    prior_tree: str | None = None,
    *,
    platform: str | None = None,
) -> tuple[str, Rows | None, dict[str, dict[str, Any]], dict[str, str]]:
    """The smoke rows left once gate_only, merge-tree passes, and unchanged previous-tip passes are taken out:
    (kind, rows, reused_results, skips). Nothing left is kind "skip"."""
    if not rows:
        return kind, rows, {}, {}
    skips: dict[str, str] = {}
    patterns = store.project.gate_only
    for row in rows:
        cid = str(row["id"])
        if any(fnmatch.fnmatchcase(cid, pattern) for pattern in patterns):
            skips[cid] = "left to the batch gate ([checks] gate_only)"
    wanted = {str(row["id"]): str(row["suite"]) for row in rows if str(row["id"]) not in skips}
    reused = reuse_from_tree(store, wanted, merge_tree, toolchain, platform=platform)
    skips.update({
        c: "passed on this merge tree before (" + str(e.get("reused_source") or "an earlier run")
           + (f", {e['reused_receipt']}" if e.get("reused_receipt") else "") + ")"
        for c, e in reused.items()
    })
    selection = (merged_plan or {}).get("selection")
    if prior_tree and merged_plan is not None and not merged_plan.get("full") and selection is not None:
        selected = {str(row["id"]) for row in selection}
        unchanged = {c: s for c, s in wanted.items() if c not in reused and c not in selected}
        earlier = {
            c: dict(e, reused_from=prior_tree, reused_by="previous-tip")
            for c, e in reuse_from_tree(store, unchanged, prior_tree, toolchain, platform=platform).items()
        }
        skips.update({
            c: f"the merge changes nothing it reads, and it passed on the previous tip's tree "
               f"{prior_tree[:10]} (" + str(e.get("reused_source") or "an earlier run") + ")"
            for c, e in earlier.items()
        })
        reused.update(earlier)
    kept = [row for row in rows if str(row["id"]) not in skips]
    return ("skip" if skips and not kept else kind), kept, reused, skips


def owned(owners: dict[str, set[str]] | None, suite: str, check_id: str) -> bool:
    # A tree without the plan module falls back to the suite's name as the id prefix.
    return check_id in owners[suite] if owners is not None else check_id.startswith(suite)


def suites_for_checks(root: Any, check_ids: Iterable[str], suites: list[str],
                      project: config.Config = config.DEFAULT) -> list[str]:
    """The selected suites that own any of the failed checks."""
    check_ids_list = list(check_ids)
    owners = suite_owners(root, suites, project)
    return [s for s in suites if any(owned(owners, s, c) for c in check_ids_list)]


def triage(root: Any, run: Path, project: config.Config = config.DEFAULT) -> str | None:
    """The project's gate triage of a red run's failures against its flake registry (`triage` in the config, given
    the run directory and --root)."""
    cfg = root.project if isinstance(root, Checkout) else project
    where = root.root if isinstance(root, Checkout) else Path(root)
    argv = list(cfg.triage)
    if not argv:
        return None
    argv = [sys.executable if part == "python3" else part for part in argv]
    script = next((part for part in argv[1:] if part.endswith(".py")), None)
    if script and not (where / script).is_file():
        return None
    result = subprocess.run([*argv, str(run), "--root", str(where)], cwd=where, capture_output=True, text=True)
    return (result.stdout + result.stderr).strip() or None


def perf_suite_of(cwd: Any, sha: str, project: config.Config = config.DEFAULT) -> str | None:
    """The perf suite the SHA's plan module names (`PERF = "<suite>"`), or None."""
    relative = project.plan_module
    if not relative:
        return None
    text = gitops.git(cwd, "show", f"{sha}:{relative}", check=False)
    found = re.search(r"^PERF = \"([\w-]+)\"", text or "", re.M)
    return found.group(1) if found else None
