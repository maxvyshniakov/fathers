"""The check contract: what a project's verify command and plan module answer (#649, #422, #640).

  <verify> --changed --base B --plan --json   the plan: {full, paths, unowned, checks: [{id, suite, paths,
                                             producer}], suites, reasons}; a tree from before the verification
                                             graph has no `checks` and names whole suites
  <verify> --checks A,B | --suites S,T [--keep-going] --jobs N
                                             a run; it prints `Verification report: <path>` to a report.json of
                                             {checks: [{id, status, log, blocked_by}], stopped, environment}
  <verify> (no selection)                    the full gate, which stamps the tree (`Verified tree <tree> stamped`)

The plan module (`plan_module`, loaded from the tree it plans) answers `SUITES`, `checks(root, scratch, suites,
keep_going, native_only, jobs, bundle=...)` with objects carrying .id and .suite, and `smoke(root)`, the check ids
of the post-merge smoke. `PERF = "<suite>"` in it names the perf suite. Each answer of a tree without the module is
None, and lanefather falls back to whole suites.
"""
from __future__ import annotations

from collections.abc import Iterable
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType
from typing import Any

from . import config, gitops
from .errors import LaneError

Rows = list[dict[str, Any]]


def verify_path(root: Any) -> str:
    return os.path.join(root, config.current().verify)


def plan_for(root: Any, base: str) -> dict[str, Any]:
    """The tree's own verification plan since `base`.

    `selection` lists the checks it selects, [{id, suite, paths}] in plan order, each with the changed paths that
    chose it: every check of the gate for a full plan, and None for a tree whose plan names whole suites. `selected`
    names the suites those checks belong to in the tree's order, `by_suite` the checks of each."""
    result = subprocess.run([verify_path(root), "--changed", "--base", base, "--plan", "--json"],
                            cwd=root, capture_output=True, text=True)
    if result.returncode:
        raise LaneError(f"{config.current().verify} --changed --plan in {root} failed: {result.stderr.strip()}")
    plan: dict[str, Any] = json.loads(result.stdout)
    rows = plan.get("checks")
    if rows is None:
        plan.update(selection=None, selected=list(plan["suites"]), by_suite=None)
        return plan
    # A full plan (a path no check reads, or a change that selects every check) runs every check of the gate; its
    # rows that no changed path chose run for the paths no check reads, or for every changed path.
    full_why = plan.get("unowned") or plan["paths"]
    plan["selection"] = [{"id": row["id"], "suite": row["suite"],
                          "paths": row["paths"] or (full_why if plan.get("full") else [])}
                         for row in rows if plan.get("full") or not row["producer"]]
    plan["selected"] = suites_of(plan["selection"])
    plan["by_suite"] = by_suite(plan["selection"])
    return plan


def suites_of(rows: Rows) -> list[str]:
    """The suites of selected checks, in the order their first checks come."""
    return list(dict.fromkeys(row["suite"] for row in rows))


def by_suite(rows: Rows | None) -> dict[str, list[str]] | None:
    """The selected checks of each suite, in plan order; None for a selection of whole suites."""
    if rows is None:
        return None
    return {suite: [row["id"] for row in rows if row["suite"] == suite] for suite in suites_of(rows)}


def module(root: Any) -> Any:
    """The tree's own plan module; None for a tree without it."""
    relative = config.current().plan_module
    if not relative:
        return None
    try:
        path = Path(root) / relative
        loader = importlib.machinery.SourceFileLoader(f"lane_plan_{abs(hash(str(path)))}", str(path))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        assert spec is not None
        found: ModuleType = importlib.util.module_from_spec(spec)
        sys.path.insert(0, str(path.parent))
        try:
            loader.exec_module(found)
        finally:
            sys.path.remove(str(path.parent))
        return found
    except Exception:
        return None


def suite_owners(root: Any, suites: Iterable[str]) -> dict[str, set[str]] | None:
    """Each suite's check ids, from the tree's own plan module; None for a tree without it."""
    found = module(root)
    try:
        return {suite: {c.id for c in found.checks(Path(root), Path("/nonexistent"), {suite}, True, False, 1)}
                for suite in suites} if found else None
    except Exception:
        return None


def smoke_checks(root: Any) -> list[str] | None:
    """The tree's post-merge smoke (smoke() of the plan module), as check ids; None for a tree without it."""
    found = module(root)
    try:
        return list(found.smoke(Path(root))) if found and hasattr(found, "smoke") else None
    except Exception:
        return None


def known_checks(root: Any) -> set[str] | None:
    """The check ids the tree's plan module defines; None for a tree without one."""
    found = module(root)
    try:
        return {c.id for c in found.checks(Path(root), Path("/nonexistent"), set(found.SUITES), True, False, 1,
                                           bundle=False)} if found else None
    except Exception:
        return None


def post_merge_selection(root: Any, plan: dict[str, Any]) -> tuple[str, Rows | None]:
    """What the post-merge check of a merge runs, given the merged diff's plan (plan_for since the previous tip):
    ("plan", its selection) when the diff selects only structure checks (documentation), otherwise ("smoke", the
    tree's smoke, #446). Each row is {id, suite, paths}: the merged paths that select the check, or [] for a smoke
    check the diff does not select, which runs because the smoke runs everything that builds. A tree without a smoke
    runs the plan."""
    selection = plan["selection"]
    if set(plan["selected"]) <= {"structure"}:
        return "plan", selection
    ids = smoke_checks(root)
    found = module(root)
    if not ids or found is None:
        return "plan", selection
    try:
        every = found.checks(Path(root), Path("/nonexistent"), set(found.SUITES), True, False, 4, bundle=False)
    except Exception:
        return "plan", selection
    suite = {check.id: check.suite for check in every}
    why = {row["id"]: row["paths"] for row in selection or ()}
    rows = [{"id": i, "suite": suite[i], "paths": why.get(i, [])} for i in ids if i in suite]
    # The smoke's checks in the tree's suite order.
    order = {s: n for n, s in enumerate(found.SUITES)}
    return "smoke", sorted(rows, key=lambda row: order.get(row["suite"], len(order)))


def toolchain_state(root: Any) -> dict[str, Any] | None:
    """The toolchain the tree's resolver reports now (#571): its resolution, {"refused": why} when it refuses, None
    for a tree that pins none. A receipt certifies only the toolchain it ran with."""
    cfg = config.current()
    if not cfg.toolchain or not cfg.toolchain_pin or not (Path(root) / cfg.toolchain_pin).is_file():
        return None
    argv = [sys.executable if part == "python3" else part for part in cfg.toolchain]
    if len(argv) > 1 and not (Path(root) / argv[1]).is_file() and argv[0] == sys.executable:
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


def lane_basis(receipt: dict[str, Any] | None, landed: dict[str, Any], toolchain: Any,
               worktree: Any = None) -> str | None:
    """Why the lane's receipt cannot stand for part of its merge's smoke (#640), or None when it can: green, for
    the tree the lane landed, with per-check results, and run with the toolchain the smoke resolves (#571)."""
    if not receipt:
        return "the lane has no receipt"
    if receipt.get("status") != "green":
        return "the lane's receipt is red"
    if receipt.get("tree") != landed["tree"]:
        return (f"the lane's receipt covers tree {str(receipt.get('tree'))[:10]}, not the landed tree "
                f"{landed['tree'][:10]}")
    if receipt.get("checks") is None or receipt.get("check_results") is None:
        return "the lane's receipt has no per-check results"
    if receipt.get("toolchain") != toolchain:
        return "the Rust toolchain changed since the lane check"
    from . import results
    if results.receipt_platform(receipt, worktree) != results.host_platform():
        return f"the lane check did not run on {results.host_platform()}"
    return None


def narrow_smoke(checkout: Any, receipt: dict[str, Any] | None, landed: dict[str, Any], merge_tree: str, kind: str,
                 rows: Rows | None, worktree: Any = None) -> tuple[str, Rows | None, dict[str, Any]]:
    """The part of the post-merge selection `rows` that the merge still needs after the lane's green receipt
    (#640): (kind, rows, narrowing).

    The receipt verified the lane's tree; the merge differs from it only by what the integration brought in, the
    diff from the lane tip to the merge. A check that diff does not select reads the same files on both trees, so
    the lane's passed result stands for it. What runs:
    - each check that diff selects, with the integration's paths that select it;
    - every other check the receipt does not show passed. Each carries `unverified`, the receipt's status.
    Nothing left is kind "skip", the receipt standing as the smoke's result, otherwise "narrowed"; `lane_verified`
    holds the receipt's result for each check left out. A diff that selects the full gate or a receipt that does
    not cover the landed tree (lane_basis) leaves the rows as they were, with the reason."""
    if rows is None:
        return kind, rows, {"narrowed": False, "reason": "the tree's plan names whole suites"}
    reason = lane_basis(receipt, landed, toolchain_state(checkout), worktree)
    if reason or receipt is None:
        return kind, rows, {"narrowed": False, "reason": reason}
    basis = {"receipt_tree": receipt["tree"], "receipt_head": receipt.get("head"), "lane_tip": landed["tip"]}
    paths: list[str]
    if merge_tree == receipt["tree"]:
        brought: dict[str, list[str]] = {}
        paths = []
    else:
        delta = plan_for(checkout, landed["tip"])
        paths = delta["paths"]
        if delta.get("full") or delta["selection"] is None:
            return kind, rows, dict(basis, narrowed=False, integration_paths=paths,
                                    reason="what the integration brought in selects the full gate")
        brought = {row["id"]: row["paths"] for row in delta["selection"]}
    results: dict[str, Any] = receipt["check_results"]
    passed = {c for c, e in results.items() if e.get("status") == "passed"}
    kept: Rows = []
    for row in rows:
        if row["id"] in brought:
            kept.append(dict(row, paths=brought[row["id"]]))
        elif row["id"] not in passed:
            kept.append(dict(row, unverified=(results.get(row["id"]) or {}).get("status", "not selected")))
    running = {row["id"] for row in kept}
    # Every check left out passed in the receipt; land records its result in check_results.
    lane_verified = {row["id"]: dict(results[row["id"]], reused_from=results[row["id"]].get("reused_from")
                                     or receipt["tree"]) for row in rows if row["id"] not in running}
    narrowing = dict(basis, narrowed=True, integration_paths=paths, lane_verified=lane_verified,
                     dropped=list(lane_verified))
    return ("narrowed" if kept else "skip"), kept, narrowing


def owned(owners: dict[str, set[str]] | None, suite: str, check_id: str) -> bool:
    # A tree without the plan module falls back to the suite's name as the id prefix.
    return check_id in owners[suite] if owners is not None else check_id.startswith(suite)


def suites_for_checks(root: Any, check_ids: Iterable[str], suites: list[str]) -> list[str]:
    """The selected suites that own any of the failed checks."""
    check_ids = list(check_ids)
    owners = suite_owners(root, suites)
    return [s for s in suites if any(owned(owners, s, c) for c in check_ids)]


def triage(root: Any, run: Path) -> str | None:
    """The project's gate triage of a red run's failures against its flake registry (`triage` in the config, given
    the run directory and --root)."""
    argv = list(config.current().triage)
    if not argv:
        return None
    argv = [sys.executable if part == "python3" else part for part in argv]
    script = next((part for part in argv[1:] if part.endswith(".py")), None)
    if script and not (Path(root) / script).is_file():
        return None
    result = subprocess.run([*argv, str(run), "--root", str(root)], cwd=root, capture_output=True, text=True)
    return (result.stdout + result.stderr).strip() or None


def perf_suite_of(cwd: Any, sha: str) -> str | None:
    """The perf suite the SHA's plan module names (`PERF = "<suite>"`), or None."""
    import re
    relative = config.current().plan_module
    if not relative:
        return None
    text = gitops.git(cwd, "show", f"{sha}:{relative}", check=False)
    found = re.search(r"^PERF = \"([\w-]+)\"", text or "", re.M)
    return found.group(1) if found else None
