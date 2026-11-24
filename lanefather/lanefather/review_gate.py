"""The review gate (fathers#109, #74): `gate_report`, `review_refusals`, and `findings`."""
from __future__ import annotations

import json
import os
from typing import Any

from . import journal, review_record, scope, state
from .errors import LaneError
from .gitops import common_dir, git, git_ok
from .state import Record


def finding_line(record: Record, finding: dict[str, Any], *, prog: str = "lanefather") -> str:
    """A finding and the two commands that mark it (fathers#109)."""
    lane_id, n = record["id"], finding.get("n")
    where = ""
    if finding.get("file"):
        where = f" {finding['file']}" + (f":{finding['line']}" if finding.get("line") else "")
    return (f"lane {lane_id} ({scope.display_ticket(record['ticket'])}): finding #{n} {finding.get('severity', 'P2')}"
            f"{where} {finding.get('title', '')}: {prog} review {lane_id} --mark {n} fixed:<merge> | "
            f"{prog} review {lane_id} --mark {n} rejected:<reason>")


def gate_report(store: state.Store, cwd: str, common: str, sha: str,
                since: str | None = None) -> dict[str, Any]:
    """The reviews of the lanes landed in the SHA and not in `since` (local dev by default), as the batch gate,
    promote and a publish read them (#570, fathers#109): {v, sha, since, blocks, unfinished, blocking, reported}."""
    promoted = store.project.promoted
    base = (since or git(cwd, "rev-parse", "--verify", "--quiet", f"refs/heads/{promoted[0]}^{{commit}}", check=False)
            or None)
    blocks = store.project.review_blocks
    report: dict[str, Any] = {"v": 1, "sha": sha, "since": base, "blocks": list(blocks), "unfinished": [],
                              "blocking": [], "reported": [], "unknown": []}
    known = 0
    for record in store.lanes():
        landed_info: dict[str, Any] = record.get("landed") or {}
        merge = str(landed_info.get("merge") or "")
        if not merge or record.get("common_dir") != common:
            continue
        if not git_ok(cwd, "merge-base", "--is-ancestor", merge, sha):
            continue
        if base and git_ok(cwd, "merge-base", "--is-ancestor", merge, base):
            continue
        known += 1
        rev = review_record.ReviewRecord.load(store, record["id"])
        verdict = rev.verdict
        _, words = rev.summary(prog=store.prog)
        if isinstance(verdict, review_record.Exempt):
            continue
        ticket_ref = scope.display_ticket(record["ticket"])
        if isinstance(
            verdict,
            (review_record.Absent, review_record.Running, review_record.Died, review_record.Failed),
        ):
            report["unfinished"].append({"lane": record["id"], "ticket": record["ticket"],
                                         "text": f"lane {record['id']} ({ticket_ref}): {words}"})
            continue
        if isinstance(verdict, review_record.PartialPanel):
            report["unfinished"].append({"lane": record["id"], "ticket": record["ticket"],
                                         "text": f"lane {record['id']} ({ticket_ref}): {words}"})
        for finding in verdict.findings:
            mark: dict[str, Any] = finding.get("mark") or {}
            entry = {"lane": record["id"], "ticket": record["ticket"], "n": finding.get("n"),
                     "severity": finding.get("severity", "P2"), "title": finding.get("title", ""),
                     "file": finding.get("file"), "line": finding.get("line"),
                     "text": finding_line(record, finding, prog=store.prog)}
            held = entry["severity"] in blocks
            fix_merge = str(mark.get("merge") or "")
            if not mark:
                report["blocking" if held else "reported"].append(entry)
            elif mark.get("kind") == "fixed" and not git_ok(cwd, "merge-base", "--is-ancestor", fix_merge, sha):
                report["blocking" if held else "reported"].append(dict(entry, text=(
                    f"lane {record['id']} ({ticket_ref}): finding #{finding.get('n')} "
                    f"is marked fixed by {fix_merge[:10]}, which is not in {sha[:10]}")))
    if not known:
        merges = git(cwd, "rev-list", "--merges", f"{base}..{sha}" if base else sha, check=False).split()
        report["unknown"] = [{"merge": m, "text": f"merge {m[:10]}: no lane state here knows it; run where the lanes' "
                                                  "state lives"} for m in merges[:20]]
    return report


def review_refusals(store: state.Store, cwd: str, common: str, sha: str) -> list[str]:
    """What holds the move of dev: each lane whose review is unfinished, and each blocking finding with its mark
    commands (fathers#109)."""
    report = gate_report(store, cwd, common, sha)
    return [e["text"] for e in report["unfinished"]] + [e["text"] for e in report["blocking"]]


def findings(args: Any, store: state.Store) -> int:
    """`lanefather findings <sha>`: the review gate as the batch gate and promote apply it, for a publish that
    Lanefather does not run (Repo-Kit's release:publish). Exit 0 when nothing holds the SHA, 1 when a review is
    unfinished or a blocking finding is unmarked; --force-reason answers 0 and logs the override."""
    cwd = os.getcwd()
    sha = git(cwd, "rev-parse", "--verify", "--quiet", args.sha + "^{commit}", check=False)
    if not sha:
        raise LaneError(f"findings: {args.sha} is not a commit here")
    since = None
    if args.since:
        since = git(cwd, "rev-parse", "--verify", "--quiet", args.since + "^{commit}", check=False)
        if not since:
            raise LaneError(f"findings: --since {args.since} is not a commit here")
    report = gate_report(store, cwd, common_dir(cwd), sha, since)
    held = [e["text"] for e in report["unfinished"] + report["blocking"] + report["unknown"]]
    if held and args.force_reason:
        journal.override("findings", None, args.force_reason, held, subject={"type": "commit", "id": sha}, store=store)
    report["held"] = bool(held) and not args.force_reason
    if args.json:
        print(json.dumps(report, sort_keys=True))
    else:
        for entry in report["unfinished"] + report["blocking"] + report["unknown"]:
            print(f"held: {entry['text']}")
        for entry in report["reported"]:
            print(f"reported ({entry['severity']}, does not block): {entry['text']}")
        if not held:
            print(f"lane: no review holds {sha[:10]}")
        elif args.force_reason:
            print(f"lane: {len(held)} held, overridden: {args.force_reason} (logged)")
    return 1 if report["held"] else 0
