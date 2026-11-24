"""The review gate (fathers#109, #74, #252): `gate_report`, `review_refusals`, `covering_batch_review`, `findings`."""
from __future__ import annotations

import json
import os
from typing import Any, cast

from . import journal, review_class, review_record, scope, state
from .errors import LaneError
from .gitops import common_dir, git, git_ok, main_checkout
from .state import Record


def finding_line(record: Record, finding: dict[str, Any], *, prog: str = "lanefather") -> str:
    """A finding and the two commands that mark it (fathers#109)."""
    lane_id, n = record["id"], finding.get("n")
    where = ""
    raw_file = finding.get("file")
    if isinstance(raw_file, str) and raw_file:
        raw_line = finding.get("line")
        line_part = f":{raw_line}" if isinstance(raw_line, int) and not isinstance(raw_line, bool) else ""
        where = f" {raw_file}{line_part}"
    sev = finding.get("severity") if isinstance(finding.get("severity"), str) else "P2"
    title = finding.get("title") if isinstance(finding.get("title"), str) else ""
    return (f"lane {lane_id} ({scope.display_ticket(record['ticket'])}): finding #{n} {sev}"
            f"{where} {title}: {prog} review {lane_id} --mark {n} fixed:<merge> | "
            f"{prog} review {lane_id} --mark {n} rejected:<reason>")


def _batch_finding_line(batch_label: str, tip: str, finding: dict[str, Any], *, prog: str = "lanefather") -> str:
    n = finding.get("n")
    where = ""
    raw_file = finding.get("file")
    if isinstance(raw_file, str) and raw_file:
        raw_line = finding.get("line")
        line_part = f":{raw_line}" if isinstance(raw_line, int) and not isinstance(raw_line, bool) else ""
        where = f" {raw_file}{line_part}"
    sev = finding.get("severity") if isinstance(finding.get("severity"), str) else "P2"
    title = finding.get("title") if isinstance(finding.get("title"), str) else ""
    return (
        f"batch review {batch_label}: finding #{n} {sev}{where} "
        f"{title}: {prog} review --overall {tip[:10]} --mark {n} fixed:<merge> | "
        f"{prog} review --overall {tip[:10]} --mark {n} rejected:<reason>"
    )


def _batch_verdict_rank(verdict: review_record.ReviewVerdict) -> int:
    if isinstance(verdict, review_record.Complete):
        return 5
    if isinstance(verdict, review_record.Unmarked):
        return 4
    if isinstance(verdict, review_record.PartialPanel):
        return 3
    if isinstance(verdict, review_record.Running):
        return 2
    if isinstance(verdict, review_record.Died):
        return 1
    return 0


def _batch_timestamp(rev: review_record.ReviewRecord) -> str:
    if rev.raw is None:
        return ""
    for key in ("finished", "started"):
        val = rev.raw.get(key)
        if isinstance(val, str):
            return val
    return ""


def _batch_covers_lane(cwd: str, merge: str, prev_sha: str, b_base: str, b_tip: str) -> bool:
    """Verify `b_base..b_tip` actually encloses the lane's merge (strictly after `b_base`, with `b_base` an ancestor
    of the lane's pre-merge commit `prev_sha`)."""
    if not b_base or not prev_sha:
        return False
    return (
        git_ok(cwd, "merge-base", "--is-ancestor", merge, b_tip)
        and not git_ok(cwd, "merge-base", "--is-ancestor", merge, b_base)
        and git_ok(cwd, "merge-base", "--is-ancestor", b_base, prev_sha)
    )


def _batch_coverages(
    store: state.Store, cwd: str, sha: str | None = None
) -> dict[str, list[tuple[review_record.ReviewRecord, str, str]]]:
    """Map lane id -> list of `(overall_review, base, tip)` ordered best-verdict-first (fathers#252)."""
    by_lane: dict[str, list[tuple[review_record.ReviewRecord, str, str]]] = {}
    for rev in review_record.ReviewRecord.all(store):
        raw = rev.raw
        if not raw or raw.get("kind") != "overall":
            continue
        raw_covers: Any = raw.get("covers")
        if not isinstance(raw_covers, list) or not raw_covers:
            continue
        raw_tip = raw.get("tip") if isinstance(raw.get("tip"), str) else raw.get("sha")
        if not isinstance(raw_tip, str) or not raw_tip:
            continue
        if sha is not None and not git_ok(cwd, "merge-base", "--is-ancestor", raw_tip, sha):
            continue
        raw_base = raw.get("base")
        batch_base = raw_base if isinstance(raw_base, str) else ""
        for item in cast(list[Any], raw_covers):
            if isinstance(item, str) and item:
                by_lane.setdefault(item, []).append((rev, batch_base, raw_tip))
    for entries in by_lane.values():
        entries.sort(key=lambda item: (_batch_verdict_rank(item[0].verdict), _batch_timestamp(item[0])), reverse=True)
    return by_lane


def covering_batch_review(store: state.Store, record: Record) -> review_record.ReviewRecord | None:
    """Return a batch review covering a landed `record` (if any), for `done --finished` (fathers#252)."""
    raw_landed: Any = record.get("landed")
    if not isinstance(raw_landed, dict):
        return None
    landed_info = cast(dict[str, Any], raw_landed)
    merge = landed_info.get("merge")
    prev_sha = landed_info.get("previous")
    common = record.get("common_dir")
    if not isinstance(merge, str) or not merge or not isinstance(prev_sha, str) or not prev_sha:
        return None
    if not isinstance(common, str) or not common or not os.path.isdir(common):
        return None
    cwd = main_checkout(common)
    lane_id = str(record["id"])
    batches = _batch_coverages(store, cwd, sha=None)
    for b_rev, b_base, b_tip in batches.get(lane_id, ()):
        if _batch_covers_lane(cwd, merge, prev_sha, b_base, b_tip):
            return b_rev
    return None


def _lane_effective_class(record: Record) -> str | None:
    raw_landed: Any = record.get("landed")
    if isinstance(raw_landed, dict):
        raw_cls = cast(dict[str, Any], raw_landed).get("review_class")
        if isinstance(raw_cls, str) and raw_cls in review_class.CLASS_RANK:
            return raw_cls
    return review_class.declared_class(record)


def _exempt_class_info(record: Record) -> tuple[str, str] | None:
    """Return `(review_class, reason)` for a valid `none` or `evidence` lane (excluding fix lanes), else None."""
    if state.fix_parents(record):
        return None
    raw_landed: Any = record.get("landed")
    landed_info = cast(dict[str, Any], raw_landed) if isinstance(raw_landed, dict) else {}
    raw_cls = landed_info.get("review_class")
    cls = raw_cls if isinstance(raw_cls, str) and raw_cls in review_class.CLASS_RANK else None
    declared = review_class.declared_class(record)
    if cls is None:
        cls = declared
    if cls is None and landed_info.get("docs_only"):
        cls = "none"
    if cls not in ("none", "evidence"):
        return None
    if cls == "evidence" and (declared != "evidence" or review_class.evidence_problem(record) is not None):
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


def _collect_audit_alerts(store: state.Store, common: str) -> list[dict[str, Any]]:
    """Collect unmarked P0/P1 audit findings across all landed lanes of `common` (fathers#252)."""
    alerts: list[dict[str, Any]] = []
    seen: set[tuple[str, Any]] = set()
    for record in store.lanes():
        if record.get("common_dir") != common:
            continue
        lane_id = str(record["id"])
        audit_rev = review_record.ReviewRecord.load(
            store, review_class.audit_label(lane_id), record=record,
        )
        if not audit_rev.has_finished_receipt:
            continue
        raw = audit_rev.raw or {}
        raw_cls = raw.get("review_class")
        cls = raw_cls if isinstance(raw_cls, str) and raw_cls in review_class.CLASS_RANK else (
            _lane_effective_class(record) or "none"
        )
        raw_why = raw.get("review_reason")
        reason = raw_why if isinstance(raw_why, str) and raw_why else "no review required"
        ticket_ref = scope.display_ticket(record["ticket"])
        for finding in audit_rev.findings:
            if isinstance(finding.get("mark"), dict):
                continue
            sev = finding.get("severity") if isinstance(finding.get("severity"), str) else ""
            if sev not in review_class.AUDIT_SEVERITIES:
                continue
            key = (lane_id, finding.get("n"))
            if key in seen:
                continue
            seen.add(key)
            title = finding.get("title") if isinstance(finding.get("title"), str) else ""
            alerts.append({
                "lane": lane_id,
                "ticket": record["ticket"],
                "review_class": cls,
                "reason": reason,
                "n": finding.get("n"),
                "severity": sev,
                "title": title,
                "text": (
                    f"lane {lane_id} ({ticket_ref}, allowed through by review class {cls!r}: {reason}) "
                    f"has {sev} audit finding #{finding.get('n')}: {title}"
                ),
            })
    return alerts


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
                              "exempt_lanes": [], "audit_alerts": _collect_audit_alerts(store, common)}
    batches = _batch_coverages(store, cwd, sha)
    reported_batches: set[str] = set()
    known = 0
    for record in store.lanes():
        raw_landed: Any = record.get("landed")
        landed_info = cast(dict[str, Any], raw_landed) if isinstance(raw_landed, dict) else {}
        raw_merge = landed_info.get("merge")
        merge = raw_merge if isinstance(raw_merge, str) else ""
        if not merge or record.get("common_dir") != common:
            continue
        if not git_ok(cwd, "merge-base", "--is-ancestor", merge, sha):
            continue
        if base and git_ok(cwd, "merge-base", "--is-ancestor", merge, base):
            continue
        known += 1
        lane_id = str(record["id"])
        ticket_ref = scope.display_ticket(record["ticket"])
        raw_prev = landed_info.get("previous")
        prev_sha = raw_prev if isinstance(raw_prev, str) else ""
        eff_cls = _lane_effective_class(record)
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
                    for b_rev, b_base, b_tip in batches[lane_id]
                    if _batch_covers_lane(cwd, merge, prev_sha, b_base, b_tip)
                ),
                None,
            )
            if covering is not None:
                b_rev, _, b_tip = covering
                if eff_cls == "double" and b_rev.has_finished_receipt:
                    double_err = review_class.double_review_problem(b_rev.raw, label=lane_id, prog=store.prog)
                    if double_err is not None:
                        report["unfinished"].append({
                            "lane": lane_id, "ticket": record["ticket"], "batch": b_rev.label,
                            "text": f"lane {lane_id} ({ticket_ref}): batch review {b_rev.label}: {double_err}",
                        })
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
                            raw_mark: Any = finding.get("mark")
                            mark = cast(dict[str, Any], raw_mark) if isinstance(raw_mark, dict) else {}
                            sev = finding.get("severity") if isinstance(finding.get("severity"), str) else "P2"
                            title = finding.get("title") if isinstance(finding.get("title"), str) else ""
                            entry = {
                                "lane": lane_id, "ticket": record["ticket"], "batch": b_rev.label,
                                "n": finding.get("n"), "severity": sev,
                                "title": title, "file": finding.get("file"),
                                "line": finding.get("line"),
                                "text": _batch_finding_line(b_rev.label, b_tip, finding, prog=store.prog),
                            }
                            held = review_class.blocks_gate(sev, blocks)
                            raw_fix_merge = mark.get("merge")
                            fix_merge = raw_fix_merge if isinstance(raw_fix_merge, str) else ""
                            if not mark:
                                report["blocking" if held else "reported"].append(entry)
                            elif mark.get("kind") == "fixed" and (
                                not fix_merge or not git_ok(cwd, "merge-base", "--is-ancestor", fix_merge, sha)
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
        if isinstance(verdict, (review_record.PartialPanel, review_record.Stale)):
            report["unfinished"].append({"lane": record["id"], "ticket": record["ticket"],
                                         "text": f"lane {record['id']} ({ticket_ref}): {words}"})
        if eff_cls == "double":
            double_err = review_class.double_review_problem(rev.raw, label=lane_id, prog=store.prog)
            if double_err is not None:
                report["unfinished"].append({
                    "lane": record["id"], "ticket": record["ticket"],
                    "text": f"lane {record['id']} ({ticket_ref}): {double_err}",
                })
        for finding in verdict.findings:
            raw_mark = finding.get("mark")
            mark = cast(dict[str, Any], raw_mark) if isinstance(raw_mark, dict) else {}
            sev = finding.get("severity") if isinstance(finding.get("severity"), str) else "P2"
            title = finding.get("title") if isinstance(finding.get("title"), str) else ""
            entry = {"lane": record["id"], "ticket": record["ticket"], "n": finding.get("n"),
                     "severity": sev, "title": title,
                     "file": finding.get("file"), "line": finding.get("line"),
                     "text": finding_line(record, finding, prog=store.prog)}
            held = review_class.blocks_gate(sev, blocks)
            raw_fix_merge = mark.get("merge")
            fix_merge = raw_fix_merge if isinstance(raw_fix_merge, str) else ""
            if not mark:
                report["blocking" if held else "reported"].append(entry)
            elif mark.get("kind") == "fixed" and (
                not fix_merge or not git_ok(cwd, "merge-base", "--is-ancestor", fix_merge, sha)
            ):
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
