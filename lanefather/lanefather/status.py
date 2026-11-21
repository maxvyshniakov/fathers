"""`lanefather status --json`: every registered project's lanes on this machine, for dashboards (Godfather's Lanes
page), read-only.

  {"v": 1, "host", "time", "registry",
   "projects": [{"project", "state_dir", "config", "repository", "cap", "cap_source", "active", "planned",
                 "gate": {"sha", "status", "at"} | null,
                 "lanes": [{"id", "status", "ticket": {"number", "url"}, "branch", "base", "worktree", "claims",
                            "after", "created", "receipt": {"status", "tree_match", "path"},
                            "post_merge": str | null, "review": {"status", "open", "findings"},
                            "logs": {phase: path}, "outcome"}]}
                | {"project", "state_dir", "error"}]}

The lanes are those not done. A project that cannot be read gives its error entry, never a failed snapshot. It takes
no locks, writes nothing, runs no project hook and makes no network call; the lane cap is the project's admission
answer, or its derived cap when that is unavailable.
"""
from __future__ import annotations

import contextlib
import datetime
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any

from . import admission, config, events, host, journal, lifecycle, receipts, registry, state
from .errors import LaneError
from .gitops import main_checkout

VERSION = 1
POLL = 5
LOG = re.compile(r"(?P<label>.+)-\d{8}T\d{6}\.log")


def branch_trees(root: str) -> dict[str, str]:
    """Each local branch's tree in the checkout at `root`, in one git call."""
    try:
        result = subprocess.run(["git", "-C", root, "for-each-ref", "--format=%(refname:short) %(tree)",
                                 "refs/heads/"], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    trees: dict[str, str] = {}
    for line in result.stdout.splitlines() if result.returncode == 0 else []:
        name, _, tree = line.rpartition(" ")
        if name and tree:
            trees[name] = tree
    return trees


def latest_logs(directory: Path, ids: set[str]) -> dict[str, dict[str, str]]:
    """The newest run log of each phase of each lane: check, rerun, base, post-merge and review."""
    found: dict[str, dict[str, tuple[str, str]]] = {}
    with contextlib.suppress(OSError), os.scandir(directory / "logs") as entries:
        for entry in entries:
            match = LOG.fullmatch(entry.name)
            if not match:
                continue
            label = match["label"]
            owner = phase = None
            for prefix, name in (("post-merge-", "post-merge"), ("review-", "review"), ("lane-", "check")):
                rest = label.removeprefix(prefix)
                if rest == label:
                    continue
                if rest in ids:
                    owner, phase = rest, name
                elif name == "check":
                    for suffix, kind in (("-alone", "rerun"), ("-base", "base")):
                        if rest.endswith(suffix) and rest.removesuffix(suffix) in ids:
                            owner, phase = rest.removesuffix(suffix), kind
                break
            if owner and phase and entry.name > found.get(owner, {}).get(phase, ("", ""))[0]:
                found.setdefault(owner, {})[phase] = (entry.name, entry.path)
    return {lane: {phase: item[1] for phase, item in sorted(phases.items())} for lane, phases in found.items()}


def newest_gate(directory: Path) -> dict[str, Any] | None:
    """The newest batch gate's result: promote-<sha>.gate.json by its finish time."""
    return receipts.newest_gate(state.Store(config.Config(), directory=directory))


def lane_entry(record: state.Record, directory: Path, repo: str | None, trees: dict[str, str],
               logs: dict[str, str], *, store: state.Store | None = None) -> dict[str, Any]:
    current = store or state.Store(config.Config(), directory=directory)
    tree = trees.get(str(record.get("branch", "")))
    return receipts.lane_view(current, record, tip_tree=tree, repository=repo, logs=logs).for_status()


def project_entry(found: registry.Project) -> dict[str, Any]:
    project, directory, common, configured = found.config, found.directory, found.common, found.configured
    # A state directory several projects share holds each one's lanes: a project reports those of its repository,
    # and a record that names none, as the cap counts it (state.of_project).
    # A record a write cut short cannot say its repository: every project of the directory lists it (fathers#129).
    damaged: list[Path] = []
    store = state.Store(project, directory=directory)
    everywhere = store.lanes(damaged=damaged)
    records = state.of_project(everywhere, common)
    try:
        decision = admission.evaluate(project, everywhere, common)
        cap, source = decision.cap, decision.source
    except LaneError as error:
        cap, source = host.lane_cap(project)
        source += f" (admission unavailable: {error})"
    shown = [r for r in records if not lifecycle.finished(r)]
    roots = {main_checkout(str(r["common_dir"])) for r in shown if r.get("common_dir")}
    trees: dict[str, str] = {}
    for root in sorted(roots):
        trees.update(branch_trees(root))
    repo = found.repository
    logs = latest_logs(directory, {str(r["id"]) for r in shown})
    return {"project": found.name, "state_dir": str(directory), "config": configured,
            "repository": repo, "cap": cap, "cap_source": source,
            "active": sum(1 for r in records if lifecycle.active(r)),
            "planned": sum(1 for r in records if lifecycle.planned(r)),
            "gate": receipts.newest_gate(store),
            "lanes": [lane_entry(r, directory, repo, trees, logs.get(str(r["id"]), {}), store=store) for r in shown],
            "damaged": [{"path": str(path), "error": state.cut_short(path)} for path in damaged]}


def snapshot() -> dict[str, Any]:
    now = datetime.datetime.now(datetime.UTC)
    projects: list[dict[str, Any]] = []
    for entry, found, unreadable in registry.projects():
        try:
            if found is None:
                raise LaneError(unreadable or "unreadable")
            projects.append(project_entry(found))
        except Exception as error:  # one unreadable project never fails the snapshot
            projects.append({"project": entry.get("project") or Path(entry["state_dir"]).name,
                             "state_dir": entry["state_dir"], "error": str(error).replace("\n", " ")[:500]})
    return {"v": VERSION, "host": events.host_name(), "time": now.strftime(journal.LEDGER_TIME),
            "registry": str(registry.path()), "projects": projects}


def status(args: Any) -> int:
    found = snapshot()
    if args.json:
        print(json.dumps(found))
        return 0
    if not found["projects"]:
        print(f"No projects in {found['registry']}: a lanefather command in a project registers it")
    for project in found["projects"]:
        if "error" in project:
            print(f"{project['project']}  {project['state_dir']}  error: {project['error']}")
            continue
        gate_info = project["gate"]
        print(f"{project['project']}  {project['active']} active, {project['planned']} planned of {project['cap']}"
              + (f"  gate {gate_info['status']} {str(gate_info['sha'])[:10]}" if gate_info else "")
              + f"  {project['state_dir']}")
        for lane in project["lanes"]:
            print(f"    {lane['id']}  {lane['status']}  receipt {lane['receipt']['status']}"
                  f"  review {lane['review']['status']}")
        for row in project.get("damaged", []):
            print(f"    damaged {row['path']}: restore the record or remove the file; start, land, promote and gc "
                  "refuse until then")
    return 0
