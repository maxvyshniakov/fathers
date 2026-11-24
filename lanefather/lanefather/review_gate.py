"""The review gate (fathers#109, #74): `gate_report`, `review_refusals`, and `findings`."""
from __future__ import annotations

import json
import os
from typing import Any, cast

from . import journal, review_class, review_record, scope, state
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


def _batch_finding_line(batch_label: str, tip: str, finding: dict[str, Any], *, prog: str = "lanefather") -> str:
    n = finding.get("n")
    where = ""
    if finding.get("file"):
        where = f" {finding['file']}" + (f":{finding['line']}" if finding.get("line") else "")
    return (
        f"batch review {batch_label}: finding #{n} {finding.get('severity', 'P2')}{where} "
        f"{finding.get('title', '')}: {prog} review --overall {tip[:10]} --mark {n} fixed:<merge> | "
        f"{prog} review --overall {tip[:10]} --mark {n} rejected:<reason>"
    )


def _batch_coverages(
    store: state.Store, cwd: str, sha: str
) -> dict[str, list[tuple[review_record.ReviewRecord, str, str]]]:
    """Map lane id -> list of `(overall_review, base, tip)` whose `tip` is in `sha` (fathers#252)."""
    by_lane: dict[str, list[tuple[review_record.ReviewRecord, str, str]]] = {}
    for rev in review_record.ReviewRecord.all(store):
        raw = rev.raw
        if not raw or raw.get("kind") != "overall":
            continue
        raw_covers: Any = raw.get("covers")
        if not isinstance(raw_covers, list) or not raw_covers:
            continue
        tip = raw.get("tip") or raw.get("sha")
        if not isinstance(tip, str) or not tip:
            continue
        if not git_ok(cwd, "merge-base", "--is-ancestor", tip, sha):
            continue
        raw_base = raw.get("base")
        batch_base = raw_base if isinstance(raw_base, str) else ""
        for item in cast(list[Any], raw_covers):
            if isinstance(item, str) and item:
                by_lane.setdefault(item, []).append((rev, batch_base, tip))
    return by_lane


def _exempt_class_info(record: Record) -> tuple[str, str] | None:
    """Return `(review_class, reason)` for a `none` or `evidence` lane (excluding fix lanes), else None."""
    if state.fix_parents(record):
        return None
    raw_landed: Any = record.get("landed")
    landed_info = cast(dict[str, Any], raw_landed) if isinstance(raw_landed, dict) else {}
    raw_cls = landed_info.get("review_class")
    cls = raw_cls if isinstance(raw_cls, str) and raw_cls in review_class.CLASS_RANK else None
    if cls is None:
        cls = review_class.declared_class(record)
    if cls is None and landed_info.get("docs_only"):
        cls = "none"
    if cls not in ("none", "evidence"):
        return None
    raw_why = landed_info.get("review_class_reason")
    reason = raw_why.strip() if isinstance(raw_why, str) and raw_why.strip() else ""
    if not reason:
        if cls == "evidence":
            reason = review_class.evidence_summary(record)
        elif landed_info.get("docs_only"):
            reason = "documentation only (*.md and docs/** outside steering files)"
        else:
            raw_r = record.get("review_reason")
            reason = raw_r.strip() if isinstance(raw_r, str) and raw_r.strip() else "no review required"
    return cls, reason


def gate_report(store: state.Store, cwd: str, common: str, sha: str,
                since: str | None = None) -> dict[str, Any]:
    """The reviews of the lanes landed in the SHA and not in `since` (local dev by default), as the batch gate,
    promote and a publish read them (#570, fathers#109, fathers#252):
    {v, sha, since, blocks, unfinished, blocking, reported, unknown, exempt_lanes, audit_alerts}."""
    promoted = store.project.promoted
    base = (since or git(cwd, "rev-parse", "--verify", "--quiet", f"refs/heads/{promoted[0]}^{{commit}}", check=False)
            or None)
    blocks = store.project.review_blocks
    report: dict[str, Any] = {"v": 1, "sha": sha, "since": base, "blocks": list(blocks), "unfinished": [],
                              "blocking": [], "reported": [], "unknown": [],
                              "exempt_lanes": [], "audit_alerts": []}
    batches = _batch_coverages(store, cwd, sha)
    reported_batches: set[str] = set()
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
        lane_id = str(record["id"])
        ticket_ref = scope.display_ticket(record["ticket"])
        exempt_info = _exempt_class_info(record)
        if exempt_info is not None:
            cls, reason = exempt_info
            report["exempt_lanes"].append({
                "lane": lane_id,
                "ticket": record["ticket"],
                "review_class": cls,
                "reason": reason,
                "merge": merge,
            })
            audit_rev = review_record.ReviewRecord.load(
                store, review_class.audit_label(lane_id), record=record,
            )
            if audit_rev.has_finished_receipt:
                for f in audit_rev.findings:
                    sev = str(f.get("severity") or "")
                    if sev in review_class.AUDIT_SEVERITIES:
                        report["audit_alerts"].append({
                            "lane": lane_id,
                            "ticket": record["ticket"],
                            "review_class": cls,
                            "reason": reason,
                            "n": f.get("n"),
                            "severity": sev,
                            "title": f.get("title", ""),
                            "text": (
                                f"lane {lane_id} ({ticket_ref}, allowed through by review class {cls!r}: {reason}) "
                                f"has {sev} audit finding #{f.get('n')}: {f.get('title', '')}"
                            ),
                        })
        rev = review_record.ReviewRecord.load(store, lane_id, record=record)
        verdict = rev.verdict
        _, words = rev.summary(prog=store.prog)
        if isinstance(verdict, review_record.Exempt):
            continue
        if isinstance(
            verdict,
            (review_record.Absent, review_record.Failed, review_record.Died),
        ) and lane_id in batches:
            covering = next(
                (
                    (b_rev, b_base, b_tip)
                    for b_rev, b_base, b_tip in reversed(batches[lane_id])
                    if git_ok(cwd, "merge-base", "--is-ancestor", merge, b_tip)
                    and (not b_base or not git_ok(cwd, "merge-base", "--is-ancestor", merge, b_base))
                ),
                None,
            )
            if covering is not None:
                b_rev, _, b_tip = covering
                if b_rev.label not in reported_batches:
                    reported_batches.add(b_rev.label)
                    b_verdict = b_rev.verdict
                    _, b_words = b_rev.summary(prog=store.prog)
                    if isinstance(
                        b_verdict,
                        (review_record.Absent, review_record.Running, review_record.Died, review_record.Failed),
                    ):
                        report["unfinished"].append({
                            "lane": lane_id, "ticket": record["ticket"], "batch": b_rev.label,
                            "text": f"batch review {b_rev.label} (covering {lane_id}): {b_words}",
                        })
                    else:
                        if isinstance(b_verdict, review_record.PartialPanel):
                            report["unfinished"].append({
                                "lane": lane_id, "ticket": record["ticket"], "batch": b_rev.label,
                                "text": f"batch review {b_rev.label} (covering {lane_id}): {b_words}",
                            })
                        for finding in b_rev.findings:
                            mark: dict[str, Any] = finding.get("mark") or {}
                            entry = {
                                "lane": lane_id, "ticket": record["ticket"], "batch": b_rev.label,
                                "n": finding.get("n"), "severity": finding.get("severity", "P2"),
                                "title": finding.get("title", ""), "file": finding.get("file"),
                                "line": finding.get("line"),
                                "text": _batch_finding_line(b_rev.label, b_tip, finding, prog=store.prog),
                            }
                            held = entry["severity"] in blocks or entry["severity"] == "P0"
                            fix_merge = str(mark.get("merge") or "")
                            if not mark:
                                report["blocking" if held else "reported"].append(entry)
                            elif mark.get("kind") == "fixed" and not git_ok(
                                cwd, "merge-base", "--is-ancestor", fix_merge, sha
                            ):
                                report["blocking" if held else "reported"].append(dict(entry, text=(
                                    f"batch review {b_rev.label}: finding #{finding.get('n')} "
                                    f"is marked fixed by {fix_merge[:10]}, which is not in {sha[:10]}"
                                )))
                continue
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
            held = entry["severity"] in blocks or entry["severity"] == "P0"
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
        for alert in cast(list[dict[str, Any]], report.get("audit_alerts") or []):
            print(f"audit alert: {alert['text']}")
        if not held:
            print(f"lane: no review holds {sha[:10]}")
        elif args.force_reason:
            print(f"lane: {len(held)} held, overridden: {args.force_reason} (logged)")
    return 1 if report["held"] else 0
