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
from typing import Any, cast

from . import admission, config, events, host, journal, registry, review, state
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
    newest: dict[str, Any] | None = None
    for path in directory.glob("promote-*.gate.json"):
        gate = state.read_json(path)
        if gate and isinstance(gate.get("finished"), str) and (newest is None or gate["finished"] > newest["at"]):
            newest = {"sha": gate.get("sha"), "status": gate.get("status"), "at": gate["finished"]}
    return newest


def lane_entry(record: state.Record, directory: Path, repo: str | None, trees: dict[str, str],
               logs: dict[str, str]) -> dict[str, Any]:
    lane_id = str(record["id"])
    number = str(record.get("ticket", "")).lstrip("#")
    receipt = state.read_json(directory / f"{lane_id}.receipt.json")
    tree = trees.get(str(record.get("branch", "")))
    review_receipt = state.read_json(directory / f"{lane_id}.review.json") or {}
    raw: Any = review_receipt.get("findings")
    findings: list[dict[str, Any]] = [cast(dict[str, Any], f) for f in cast(list[Any], raw)
                                      if isinstance(f, dict)] if isinstance(raw, list) else []
    status = review_receipt.get("status", "not started")
    if status == "running" and not review.alive(review_receipt.get("pid")):
        status = "stopped"
    return {
        "id": lane_id, "status": record.get("status"),
        "ticket": {"number": number if number and number != "none" else None,
                   "url": f"https://github.com/{repo}/issues/{number}" if repo and number.isdigit() else None},
        "branch": record.get("branch"), "base": record.get("base"), "worktree": record.get("worktree"),
        "claims": record.get("claims", []), "after": record.get("after", []), "created": record.get("created"),
        "receipt": {"status": receipt.get("status", "unknown") if receipt else "not run",
                    "tree_match": receipt.get("tree") == tree if receipt and tree else None,
                    "path": str(directory / f"{lane_id}.receipt.json") if receipt else None},
        "post_merge": cast(dict[str, Any], post).get("status") if isinstance(post := record.get("post_merge"), dict)
        else None,
        "review": {"status": status, "open": sum(1 for f in findings if not f.get("mark")), "findings": len(findings)},
        "logs": logs, "outcome": record.get("outcome")}


def project_entry(entry: dict[str, Any]) -> dict[str, Any]:
    directory = Path(entry["state_dir"])
    configured = entry.get("config")
    if isinstance(configured, str):
        if not Path(configured).is_file():
            raise LaneError(f"config not found at {configured}")
        project = config.load(Path(configured))
    else:
        project = config.Config()
    if not directory.is_dir():
        raise LaneError(f"state directory {directory} not found")
    common = entry.get("common_dir") if isinstance(entry.get("common_dir"), str) else None
    # A state directory several projects share holds each one's lanes: a project reports those of its repository.
    # A record a write cut short cannot say its repository: every project of the directory lists it (fathers#129).
    damaged: list[Path] = []
    records = [r for r in state.Store(project, directory=directory).lanes(damaged=damaged)
               if common is None or r.get("common_dir") == common]
    try:
        decision = admission.evaluate(project, records)
        cap, source = decision.cap, decision.source
    except LaneError as error:
        cap, source = host.lane_cap(project)
        source += f" (admission unavailable: {error})"
    shown = [r for r in records if r.get("status") != "done"]
    roots = {main_checkout(str(r["common_dir"])) for r in shown if r.get("common_dir")}
    trees: dict[str, str] = {}
    for root in sorted(roots):
        trees.update(branch_trees(root))
    repo = entry.get("repository") if isinstance(entry.get("repository"), str) else None
    logs = latest_logs(directory, {str(r["id"]) for r in shown})
    name = journal.project_name(project) if isinstance(configured, str) else entry.get("project") or project.project
    return {"project": name, "state_dir": str(directory), "config": configured,
            "repository": repo, "cap": cap, "cap_source": source,
            "active": sum(1 for r in records if r.get("status") == "active"),
            "planned": sum(1 for r in records if r.get("status") == "planned"),
            "gate": newest_gate(directory),
            "lanes": [lane_entry(r, directory, repo, trees, logs.get(str(r["id"]), {})) for r in shown],
            "damaged": [{"path": str(path), "error": state.cut_short(path)} for path in damaged]}


def snapshot() -> dict[str, Any]:
    now = datetime.datetime.now(datetime.UTC)
    projects: list[dict[str, Any]] = []
    for entry in registry.read():
        if registry.forgotten(entry, now):
            continue
        try:
            projects.append(project_entry(entry))
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
        gate = project["gate"]
        print(f"{project['project']}  {project['active']} active, {project['planned']} planned of {project['cap']}"
              + (f"  gate {gate['status']} {str(gate['sha'])[:10]}" if gate else "") + f"  {project['state_dir']}")
        for lane in project["lanes"]:
            print(f"    {lane['id']}  {lane['status']}  receipt {lane['receipt']['status']}"
                  f"  review {lane['review']['status']}")
        for row in project.get("damaged", []):
            print(f"    damaged {row['path']}: restore the record or remove the file; start, land, promote and gc "
                  "refuse until then")
    return 0
