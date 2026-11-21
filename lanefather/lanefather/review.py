"""The independent review (#570): one review per lane beside its land, gating the move of dev; one overall review on
promote --ship, whose findings become one ticket.

The reviewer is the `review` role of the shared models.toml (mastack): today Sol 6.1 at medium effort while Codex
has at least 15% of its weekly limit left, else Opus 5.5 at medium effort. The lane review gets the lane's diff
against the last integration commit it merged, its ticket and the coding standards, never the lane's report, in a
checkout detached at the lane tip. It asks `codex app-server` for the weekly limit and runs `codex exec` in a
read-only sandbox; under the limit, a reached limit, no Codex CLI, a failed Sol run or an empty answer runs `claude
-p` instead. A lane that changes only *.md and docs/**, none of them a steering file, and a fix lane are exempt.
LANEFATHER_REVIEW_START=manual makes land and promote --ship print the review command instead of starting it.
Mastack's `role` command, beside the models file, chooses the reviewer when present (fathers#150); `[review] panel`
runs several reviewers at once, their findings numbered in one list for marks and the gate (fathers#151).
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
import os
from pathlib import Path
import posixpath
import subprocess
import sys
import time
from typing import Any, cast

from . import (
    claims,
    config,
    github,
    journal,
    lanes,
    lifecycle,
    receipts,
    review_gate,
    reviewer,
    settings,
    state,
    worktrees,
)
from .errors import LaneError
from .gitops import common_dir, current_branch, git, git_ok, main_checkout
from .state import Record
from .verify import terminable

SEVERITIES = ("P1", "P2", "P3")
LaneFinding = tuple[Record, dict[str, Any]]
REVIEW_EVENTS = ("start", "finish", "fail", "exempt", "mark", "fix-lane", "ticket")


mark_help = receipts.mark_help
fix_parent_review = receipts.fix_parent_review
gh_command = github.gh_command
repo_args = github.repo_args
finding_line = review_gate.finding_line
gate_report = review_gate.gate_report
review_refusals = review_gate.review_refusals
findings = review_gate.findings


def receipt_transition(
    store: state.Store,
    label: str,
    event: str,
    mutate: Callable[[dict[str, Any] | None], dict[str, Any]],
) -> dict[str, Any]:
    """The single writer of review receipts: apply `mutate` under `review-<label>` and persist."""
    if event not in REVIEW_EVENTS:
        raise LaneError(f"unknown review receipt event {event!r}")
    with store.locked(f"review-{label}"):
        current = receipts.read(store, label, "review")
        updated = mutate(current)
        receipts.write(store, label, updated, "review")
        return updated


alive = receipts.alive
missing_members = receipts.missing_members
unmarked_findings = receipts.unmarked_findings
unmarked_reviews = receipts.unmarked_reviews


def review_state(label: str, store: state.Store | None = None) -> tuple[bool, str]:
    """Whether the review lets promote move dev, and its state in words; of the project `store` holds (fathers#157),
    by default the current one."""
    current = store or state.Store(config.current())
    prog = store.project.command if store is not None else config.prog()
    return receipts.review_state(current, label, prog=prog)


def asked_reviewers(args: Any) -> tuple[str, ...]:
    """`--reviewer ALIAS|both` (fathers#155): the reviewers this one review runs whatever the Codex limit; `both`
    is the configured panel, else Sol and Opus. () lets the panel or the models file choose."""
    asked: str | None = getattr(args, "reviewer", None)
    if not asked:
        return ()
    if asked == "both":
        return config.current().review_panel or ("sol", "opus")
    return (asked,)


# The family of the agent that ran the lane's last check (lanefather.events.actor), for the role's `not_family`.
FAMILIES = {"claude-code": "anthropic", "codex": "openai", "antigravity": "google", "t3-code": "google"}


def implementer_family(lane_id: str) -> str | None:
    """The implementer's model family: that of the agent whose `check` wrote the lane's receipt, None unknown."""
    store = state.Store(config.current())
    receipt = receipts.read(store, lane_id) or {}
    family = FAMILIES.get(str(receipt.get("agent")))
    if family:
        return family
    record = state.read_json(store.record_path(lane_id)) or {}
    agents: list[Any] = record.get("agents") or []
    for raw_entry in reversed(agents):
        if isinstance(raw_entry, dict):
            entry = cast(dict[str, Any], raw_entry)
            kind = str(entry.get("kind") or "")
            if kind in FAMILIES:
                return FAMILIES[kind]
            model = str(entry.get("model") or "").lower()
            if model.startswith("gemini-"):
                return "google"
            if model.startswith("claude-"):
                return "anthropic"
            if model.startswith("gpt-"):
                return "openai"
    return None


def steering() -> tuple[str, ...]:
    cfg = config.current()
    return cfg.steering + ((cfg.rules_dir.rstrip("/") + "/**",) if cfg.rules_dir else ())


def documentation_only(paths: list[str]) -> bool:
    """Whether a merge changes only documentation, *.md and docs/**, and none of the steering files."""
    globs = steering()
    return all((p.endswith(".md") or p.startswith("docs/")) and not any(claims.matches(s, p) for s in globs)
               for p in paths)


def valid_fix_parents(record: Record, store: state.Store | None = None) -> Iterator[str]:
    resolved_store = store if store is not None else state.Store(config.current())
    for parent in state.fix_parents(record):
        try:
            fix_parent_review(parent, record["common_dir"], "review exemption refused", store=resolved_store)
        except LaneError:
            continue
        yield parent


def review_exemption(record: Record) -> str | None:
    parents = state.fix_parents(record)
    if parents != list(valid_fix_parents(record)):
        parents = []
    if len(parents) == 1:
        return f"the fix round of {parents[0]}'s review, which nobody reviews again"
    if parents:
        return (f"the fix round of the reviews of {', '.join(parents[:-1])} and {parents[-1]}, which nobody reviews "
                "again")
    if (record.get("landed") or {}).get("docs_only"):
        return "documentation only: it changes only *.md and docs/** outside the steering files"
    return None


def review_range(record: Record) -> tuple[str, str]:
    """The lane's tip and the base to diff it against: the last integration commit the lane merged, never the
    integration branch, which already holds the landed lane."""
    main = main_checkout(record["common_dir"])
    landed = record.get("landed")
    if landed:
        tip, against = landed["tip"], landed["previous"]
    else:
        tip = git(main, "rev-parse", "--verify", record["branch"] + "^{commit}")
        against = git(main, "rev-parse", "--verify", "--quiet", record["base"] + "^{commit}",
                      check=False) or record["base_sha"]
    return tip, git(main, "merge-base", against, tip)


def coding_standards(main: str, tip: str) -> str:
    """The coding standards of the reviewed tree, else of the project the config belongs to."""
    name = config.current().coding_standards
    shown = subprocess.run(["git", "show", f"{tip}:{name}"], cwd=main, capture_output=True, text=True)
    if shown.returncode == 0:
        return shown.stdout.strip()
    path = config.current().path
    try:
        return (path.parent / name).read_text().strip() if path else f"(no {name})"
    except OSError:
        return f"(no {name})"


def ticket_text(record: Record) -> str:
    if record["ticket"] == "none":
        return "The lane has no ticket."
    data, error = github.issue_view(record["ticket"], json_fields="title,body", common=record["common_dir"])
    if data is None:
        return f"Ticket #{record['ticket']} (gh could not read it: {error[:300]})"
    return f"Ticket #{record['ticket']}: {data.get('title', '')}\n\n{data.get('body', '')}".strip()  # pyright: ignore[reportUnknownMemberType]


def review_about(label: str, fields: dict[str, Any]) -> dict[str, Any]:
    """The subject of a review's events: its lane's for a lane review, else the review itself (an overall one)."""
    if fields.get("kind") == "lane" and fields.get("lane"):
        return {"lane": str(fields["lane"])}
    return {"subject": {"type": "review", "id": label, **({"kind": fields["kind"]} if fields.get("kind") else {})}}


def perform_review(label: str, fields: dict[str, Any], main: str, base: str, tip: str, subject: str,
                   finalize: Callable[[dict[str, Any]], None] | None = None,
                   family: str | None = None, reviewers: tuple[str, ...] = ()) -> dict[str, Any]:
    """Review base...tip with Sol, or Opus when Sol may not or cannot run; write and return the receipt.
    `finalize` completes a finished receipt before anyone can read it as finished."""
    store = state.Store(config.current())
    runner = reviewer.Reviewer(store.project)
    receipt: dict[str, Any] = {"version": 1, "label": label, **fields, "base": base, "tip": tip,
                               "tree": git(main, "rev-parse", tip + "^{tree}"), "status": "running",
                               "pid": os.getpid(), "log": store.project.env("REVIEW_LOG"),
                               "started": journal.now(), "effort": runner.effort}
    receipt_transition(store, label, "start", lambda _current: receipt)
    reviews = store.directory / "reviews"
    reviews.mkdir(parents=True, exist_ok=True)
    output, log = reviews / f"{label}.md", reviews / f"{label}.log"
    log.write_text("")
    clock = time.monotonic()
    outcome_status = "finished"
    try:
        with terminable(), worktrees.review_checkout(main, label, tip) as checkout:
            standards = coding_standards(main, tip)
            answer = runner.run(label, checkout, reviewer.Request(standards, subject, base, tip, family,
                                                                     reviewers), output, log)
            receipt.update(answer.evidence)
            if answer.error is not None:
                raise LaneError(answer.error)
        receipt.update(status="finished", output=str(output))
        if finalize:
            finalize(receipt)
    except Exception as error:  # any failure leaves a failed receipt, not a running one
        outcome_status = "failed"
        receipt.update(status="failed", error=str(error), output=str(output))
    receipt.setdefault("fallback", None)
    receipt.update(seconds=round(time.monotonic() - clock, 1), finished=journal.now())
    receipt_transition(store, label, "finish" if outcome_status == "finished" else "fail",
                       lambda _current: receipt)
    findings = receipt.get("findings", [])
    counts = " ".join(f"{s.lower()}={sum(1 for f in findings if f['severity'] == s)}" for s in SEVERITIES)
    journal.ledger(f"review {label} {outcome_status} reviewer={receipt.get('reviewer', '-')} "
                   f"model={receipt.get('model', '-')} effort={receipt['effort']} "
                   f"left={'-' if receipt.get('codex_left') is None else receipt['codex_left']} "
                   f"seconds={receipt['seconds']:.0f} findings={len(findings)} {counts}"
                   + (f" fallback: {receipt['fallback']}" if receipt["fallback"] else ""),
                   kind="review.finished", level="info" if outcome_status == "finished" else "warn",
                   **review_about(label, fields),
                   details={"status": outcome_status, "reviewer": receipt.get("reviewer"),
                            "provider": receipt.get("provider"), "cli": receipt.get("cli"),
                            "model": receipt.get("model"), "effort": receipt["effort"],
                            "options": receipt.get("options"),
                            "seconds": receipt["seconds"], "findings": len(findings),
                            "severities": {s.lower(): sum(1 for f in findings if f["severity"] == s)
                                           for s in SEVERITIES},
                            "fallback": receipt["fallback"], "error": receipt.get("error"),
                            **({"panel": [{"member": e.get("member"), "reviewer": e.get("reviewer"),
                                           "provider": e.get("provider"), "cli": e.get("cli"),
                                           "status": e.get("status"), "model": e.get("model"),
                                           "options": e.get("options"),
                                           "seconds": e.get("seconds"), "findings": len(e.get("numbers") or [])}
                                          for e in receipt["panel"]]} if receipt.get("panel") else {})})
    return receipt


def review_lines(receipt: dict[str, Any]) -> list[str]:
    label = receipt["label"]
    verdict = receipts.review_verdict(None, receipt)
    if isinstance(verdict, receipts.Exempt):
        return [f"Review of {label}: exempt ({verdict.reason})"]
    if isinstance(verdict, (receipts.Running, receipts.Died)):
        return [f"Review of {label}: running since {verdict.started} (pid {verdict.pid}, log "
                f"{verdict.log})"]
    left = receipt.get("codex_left")
    forced: dict[str, Any] = receipt.get("forced") or {}
    head = (f"Review of {label} ({receipt.get('base', '')[:10]}...{receipt.get('tip', '')[:10]}) by "
            f"{receipt.get('model')} {receipt.get('effort')}"
            + (f", {left}% of the weekly Codex limit left" if left is not None else "")
            + (f"; fallback: {receipt['fallback']}" if receipt.get("fallback") else "")
            + (f"; forced by --reviewer {', '.join(forced['reviewers'])}" if forced.get("by") == "flag" else "")
            + f", {receipt.get('seconds', 0):.0f} s")
    if not isinstance(verdict, (receipts.Unmarked, receipts.Complete)):
        error = verdict.error if isinstance(verdict, receipts.Failed) else None
        return [head, f"review failed: {error}"]
    findings = receipt.get("findings", [])
    lines = [head + f": {len(findings)} findings  ({receipt.get('output')})"]
    panel: list[dict[str, Any]] = receipt.get("panel") or []
    for entry in panel:
        lines.append(f"  panel {entry.get('member')}: " + (
            f"{entry.get('reviewer')} {entry.get('model')} {entry.get('effort')}, {entry.get('seconds', 0):.0f} s, "
            f"{len(entry.get('numbers') or [])} findings" if entry.get("status") == "finished"
            else f"failed: {entry.get('error')}" if entry.get("status") == "failed"
            else f"skipped: {entry.get('reason')}")
            + (f" ({entry['reason']})" if entry.get("status") == "finished" and entry.get("reason") else "")
            + (f"; note: {entry['note']}" if entry.get("note") else ""))
    for f in findings:
        where = f"{f['file']}:{f['line']}" if f.get("file") and f.get("line") else f.get("file") or "-"
        mark = f.get("mark")
        marked = ("" if not mark else f"  [fixed: {mark['merge'][:10]}]" if mark["kind"] == "fixed"
                  else f"  [rejected: {mark['reason']}]")
        category = f" [{f['category']}]" if f.get("category") else ""
        by = f" ({f['reviewer']})" if f.get("reviewer") else ""
        lines.append(f"  {f['n']}. {f['severity']}{category} {where} {f['title']}{by}{marked}")
        lines += [f"     {line}" for line in f.get("detail", "").splitlines()[:6]]
    if receipt.get("issue"):
        lines.append(f"Ticket: {receipt['issue']}")
    elif receipt.get("issue_error"):
        lines.append(f"Ticket not filed: {receipt['issue_error']}; {config.prog()} review --overall "
                     f"{receipt.get('tip', '')[:10]} files it again")
    pre_land: dict[str, Any] = receipt.get("pre_land_fix") or {}
    if pre_land:
        lines.append(f"Pre-land fix round: reviewed commit {str(pre_land.get('reviewed_sha', ''))[:10]} on "
                     f"{pre_land.get('branch')} ({pre_land.get('worktree')})")
    if receipt.get("kind") == "lane" and any(not f.get("mark") for f in findings):
        if pre_land:
            lines.append("Mark each finding: " + mark_help(label).replace("<merge>", "<fix-commit>|" + label))
        else:
            lines.append("Mark each finding: " + mark_help(label) + "; the fix round runs in the lane's "
                         f"worktree on a follow-up lane: {config.prog()} review {label} --fix-lane")
    return lines


def review_start_manual() -> bool:
    """LANEFATHER_REVIEW_START when set, else this machine's `review.start` setting."""
    found = config.current().env("REVIEW_START")
    return found == "manual" if found else settings.value("review.start") == "manual"


def review_start_source() -> str:
    """What made land skip the review, for its message."""
    if config.current().env("REVIEW_START"):
        return f"{config.current().env_names('REVIEW_START')}=manual"
    return "lanefather settings review.start=manual"


def self_argv() -> list[str]:
    """This lanefather, run again with the same config."""
    path = config.current().path
    return [sys.executable, "-m", "lanefather", *(("--config", str(path)) if path else ())]


def self_env(**extra: str) -> dict[str, str]:
    package_parent = str(Path(__file__).resolve().parent.parent)
    python_path = os.environ.get("PYTHONPATH")
    return dict(os.environ, PYTHONPATH=package_parent + (os.pathsep + python_path if python_path else ""), **extra)


def spawn_review(label: str, fields: dict[str, Any], argv: list[str], cwd: str) -> dict[str, Any]:
    """Start `lanefather review ...` detached, with its own log, and record it as running; nobody waits for it."""
    store = state.Store(config.current())
    log = journal.log_path(f"review-{label}")
    # The child writes its own receipt under the same lock, so this running one never replaces a finished one.
    with store.locked(f"review-{label}"), open(log, "w") as out:
        child = subprocess.Popen([*self_argv(), "review", *argv], cwd=cwd,
                                 stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                 start_new_session=True, env=self_env(LANEFATHER_REVIEW_LOG=str(log)))
        receipt = {"version": 1, "label": label, **fields, "status": "running", "pid": child.pid, "log": str(log),
                   "started": journal.now(), "effort": reviewer.Reviewer(store.project).effort}
        receipt_transition(store, label, "start", lambda _current: receipt)
    journal.ledger(f"review-start {label} pid={child.pid} log={log}", kind="review.started",
                   **review_about(label, fields), details={"pid": child.pid, "log": str(log)})
    return receipt


def start_lane_review(record: Record) -> str:
    """At land: an exempt receipt, or the review in the background. The line land prints."""
    store = state.Store(config.current())
    lane_id = record["id"]
    current = receipts.read(store, lane_id, "review")
    verdict = receipts.review_verdict(record, current)
    if isinstance(verdict, (receipts.Running, receipts.Exempt, receipts.Unmarked, receipts.Complete)):
        assert current is not None
        kept_status = ("running" if isinstance(verdict, receipts.Running)
                       else "exempt" if isinstance(verdict, receipts.Exempt) else "finished")
        # Reviewed before landing (onto dev or main), or by hand: one review per lane, kept as it is.
        journal.ledger(f"review {lane_id} kept at land: {kept_status} since "
                       f"{current.get('started') or current.get('finished')}", kind="review.kept", lane=record,
                       details={"status": kept_status,
                                "since": current.get("started") or current.get("finished")})
        return f"review kept: {review_state(lane_id, store)[1]}"
    exempt = review_exemption(record)
    if exempt:
        receipt_transition(
            store, lane_id, "exempt",
            lambda _current: {"version": 1, "label": lane_id, "kind": "lane", "lane": lane_id,
                              "ticket": record["ticket"], "status": "exempt", "exempt": exempt,
                              "finished": journal.now()},
        )
        journal.ledger(f"review {lane_id} exempt: {exempt}", kind="review.exempted", lane=record,
                       details={"exempt": exempt})
        return f"review exempt: {exempt}"
    if review_start_manual():
        return (f"review not started ({review_start_source()}): run "
                f"{config.prog()} review {lane_id}")
    receipt = spawn_review(lane_id, {"kind": "lane", "lane": lane_id, "ticket": record["ticket"]}, [lane_id],
                           main_checkout(record["common_dir"]))
    return f"review started in the background (pid {receipt['pid']}, log {receipt['log']})"


def review_complete_finish(lane_id: str, store: state.Store | None = None) -> None:
    """After the lane's review is clean or fully marked: finish the lane as land would have (auto-done,
    fathers#157), unless land is still running its smoke (land finishes it then) or its smoke was red; a kept lane
    and `[done] auto = false` only say so."""
    store = store or state.Store(config.current())
    record = store.load(lane_id)
    if not lifecycle.awaiting_done(record):
        return
    if (record.get("post_merge") or {}).get("status") != "green" and not lifecycle.kept(record):
        return
    lifecycle.finish_landed(store, lane_id, False)


def review_lane(args: Any) -> int:
    store = state.Store(config.current())
    record = store.load(args.id)
    current = receipts.read(store, args.id, "review")
    verdict = receipts.review_verdict(record, current)
    # The background run land started finds its own pid in the receipt.
    elsewhere = isinstance(verdict, receipts.Running) and verdict.pid != os.getpid()
    already_finished = isinstance(verdict, (receipts.Unmarked, receipts.Complete))
    if (already_finished or isinstance(verdict, receipts.Exempt) or elsewhere) and not args.force_reason:
        assert current is not None
        print("\n".join(review_lines(current)))
        if already_finished:
            print(f"Lane {args.id} is already reviewed: one review per lane (--force-reason reviews again, logged)")
        return 0
    if already_finished:
        journal.ledger(f"override review {args.id}: {args.force_reason} (reviewed again)", kind="override",
                       level="warn", lane=record,
                       details={"command": "review", "reason": args.force_reason, "refusals": ["reviewed again"]})
    exempt = review_exemption(record)
    if exempt and not args.force_reason:
        start_lane_review(record)
        print(f"Review of {args.id}: exempt ({exempt})")
        return 0
    tip, base = review_range(record)
    receipt = perform_review(args.id, {"kind": "lane", "lane": args.id, "ticket": record["ticket"]},
                             main_checkout(record["common_dir"]), base, tip, ticket_text(record),
                             family=implementer_family(args.id), reviewers=asked_reviewers(args))
    if record.get("pre_land_fix"):
        store.update(args.id, lambda latest: latest.pop("pre_land_fix", None) is not None)
    print("\n".join(review_lines(receipt)))
    if not isinstance(receipts.review_verdict(record, receipt), (receipts.Unmarked, receipts.Complete)):
        return 1
    if review_state(args.id, store)[0]:
        review_complete_finish(args.id)
    return 0


def reviewer_views(receipt: dict[str, Any]) -> list[dict[str, Any]]:
    """Each reviewer of a finished review with its own findings (fathers#151): the panel's entries, or the one
    reviewer of a single review."""
    findings: list[dict[str, Any]] = receipt.get("findings", [])
    panel: list[dict[str, Any]] = receipt.get("panel") or []
    if not panel:
        return [{"member": receipt.get("reviewer"), "reviewer": receipt.get("reviewer"),
                 "provider": receipt.get("provider"), "cli": receipt.get("cli"),
                 "model": receipt.get("model"), "effort": receipt.get("effort"),
                 "options": receipt.get("options"), "seconds": receipt.get("seconds"),
                 "status": receipts.status_of(receipt), "reason": receipt.get("fallback"), "findings": findings}]
    by_number = {f["n"]: f for f in findings}
    views: list[dict[str, Any]] = []
    for entry in panel:
        numbers: list[int] = entry.get("numbers") or []
        views.append(dict(entry, findings=[by_number[n] for n in numbers if n in by_number]))
    return views


def mark_counts(findings: list[dict[str, Any]]) -> dict[str, int]:
    marks: list[dict[str, Any]] = [f.get("mark") or {} for f in findings]
    kinds: list[str | None] = [m.get("kind") for m in marks]
    return {"findings": len(findings), **{s.lower(): sum(1 for f in findings if f.get("severity") == s)
                                          for s in SEVERITIES},
            "fixed": kinds.count("fixed"), "rejected": kinds.count("rejected"), "unmarked": kinds.count(None)}


def compare(args: Any) -> int:
    """`review <id> --compare`: who found what and what held up, one block per reviewer with its marks."""
    store = state.Store(config.current())
    receipt = receipts.read(store, args.id, "review")
    verdict = receipts.review_verdict(None, receipt)
    if not isinstance(verdict, (receipts.Unmarked, receipts.Complete)) or receipt is None:
        raise LaneError(f"lane {args.id} has no finished review to compare ({review_state(args.id, store)[1]})")
    views = reviewer_views(receipt)
    lines = [f"Review of {args.id} ({receipt.get('base', '')[:10]}...{receipt.get('tip', '')[:10]}): "
             f"{len(views)} reviewer{'s' if len(views) != 1 else ''}"]
    width = max(len(str(v.get("reviewer"))) for v in views)
    for view in views:
        counts = mark_counts(view["findings"])
        lines.append(f"  {view.get('reviewer')!s:<{width}}  {view.get('model')} {view.get('effort')}: "
                     + (f"{counts['findings']} findings (p1 {counts['p1']}, p2 {counts['p2']}, p3 {counts['p3']}), "
                        f"fixed {counts['fixed']}, rejected {counts['rejected']}, unmarked {counts['unmarked']}"
                        if view.get("status") == "finished" else f"{view.get('status')}: "
                        f"{view.get('error') or view.get('reason')}"))
    for view in views:
        if view.get("status") != "finished":
            continue
        lines += ["", f"{view.get('reviewer')} ({view.get('model')} {view.get('effort')}, "
                      f"{view.get('seconds') or 0:.0f} s)" + (f", for {view['member']}: {view['reason']}"
                                                              if view.get("member") != view.get("reviewer") else "")
                  + (f"; note: {view['note']}" if view.get("note") else "")]
        if not view["findings"]:
            lines.append("  no findings")
        for f in view["findings"]:
            where = f"{f['file']}:{f['line']}" if f.get("file") and f.get("line") else f.get("file") or "-"
            mark: dict[str, Any] = f.get("mark") or {}
            held = (f"fixed: {mark['merge'][:10]}" if mark.get("kind") == "fixed"
                    else f"rejected: {mark['reason']}" if mark.get("kind") == "rejected" else "unmarked")
            lines.append(f"  #{f['n']} {f['severity']} {where} {f['title']}  [{held}]")
    print("\n".join(lines))
    return 0


def fix_merge(common: str, value: str) -> str:
    """The commit a `fixed:` mark names: a landed lane's merge (the fix lane's id), an active lane's tip during a
    pre-land fix round, or a commit."""
    record = state.read_json(state.record_path(value)) if state.ID.match(value) else None
    if record and (record.get("landed") or {}).get("merge"):
        return record["landed"]["merge"]
    if record and lifecycle.active(record) and record.get("pre_land_fix"):
        tip = git(main_checkout(common), "rev-parse", "--verify", "--quiet", record["branch"] + "^{commit}",
                  check=False)
        if tip:
            return tip
    sha = git(main_checkout(common), "rev-parse", "--verify", "--quiet", value + "^{commit}", check=False)
    if not value or not sha:
        raise LaneError(f"fixed:{value}: not a commit or a landed lane here; give the follow-up merge")
    return sha


def mark_review(args: Any) -> int:
    store = state.Store(config.current())
    record = store.load(args.id)
    with store.locked(f"review-{args.id}"):
        receipt = receipts.read(store, args.id, "review")
        verdict = receipts.review_verdict(record, receipt)
        if not isinstance(verdict, (receipts.Unmarked, receipts.Complete)) or receipt is None:
            raise LaneError(f"lane {args.id} has no finished review to mark ({review_state(args.id, store)[1]})")
        by_number = {f["n"]: f for f in receipt.get("findings", [])}
        marks: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for number, value in args.mark:
            finding = by_number.get(int(number)) if number.isdigit() else None
            if finding is None:
                raise LaneError(f"--mark {number}: the review of {args.id} has findings "
                                f"{', '.join(str(n) for n in by_number) or 'none'}")
            kind, _, detail = value.partition(":")
            detail = detail.strip()
            if kind == "fixed":
                fix_sha = fix_merge(record["common_dir"], detail)
                pre_land: dict[str, Any] = receipt.get("pre_land_fix") or {}
                reviewed_sha = pre_land.get("reviewed_sha")
                if reviewed_sha and (fix_sha == reviewed_sha or not git_ok(
                        main_checkout(record["common_dir"]), "merge-base", "--is-ancestor", reviewed_sha, fix_sha)):
                    raise LaneError(f"--mark {number} fixed:{detail}: commit {fix_sha[:10]} does not advance past "
                                    f"the reviewed commit {reviewed_sha[:10]}; commit the fix on top of it first")
                marks.append((finding, {"kind": "fixed", "merge": fix_sha, "at": journal.now()}))
            elif kind == "rejected":
                if not detail:
                    raise LaneError(f"--mark {number} rejected: give the reason, rejected:<reason>")
                marks.append((finding, {"kind": "rejected", "reason": detail, "at": journal.now()}))
            else:
                raise LaneError(f"--mark {number} {value}: use fixed:<merge> or rejected:<reason>")
        for finding, mark in marks:
            finding["mark"] = mark
            journal.ledger(f"review-mark {args.id} {finding['n']} "
                           + (f"fixed:{mark['merge'][:10]}" if mark["kind"] == "fixed"
                              else f"rejected: {mark['reason']}"),
                           kind="finding.marked", lane=args.id,
                           details={"finding": finding["n"], "mark": mark["kind"], "merge": mark.get("merge"),
                                    "reason": mark.get("reason")})
        receipt_transition(store, args.id, "mark", lambda _current: receipt)
    print("\n".join(review_lines(receipt)))
    complete, words = review_state(args.id, store)
    if complete:
        kinds = [f["mark"]["kind"] for f in receipt.get("findings", [])]
        journal.ledger(f"review-marked {args.id} fixed={kinds.count('fixed')} rejected={kinds.count('rejected')}",
                       kind="review.marked", lane=args.id,
                       details={"fixed": kinds.count("fixed"), "rejected": kinds.count("rejected")})
        review_complete_finish(args.id)
    else:
        print(f"Lane {args.id}: {words}")
    return 0


def fix_claims(receipt: dict[str, Any], fix_id: str, common: str | None = None) -> tuple[list[str], list[str]]:
    """The fix lane's claims (#586): the repository files the review's findings name, never the landed lane's
    claims, which hold every path its check widened to and so overlap lanes started since. A named file another
    planned or active lane claims stays out, with a line saying so; check widens the claims from the fix's own
    diff and warns if that touches another lane's claim (#574). Returns the claims and those lines."""
    found: list[str] = []
    held: list[str] = []
    lines: list[str] = []
    others = [r for r in state.occupying(state.of_project(state.lanes(), common)) if r["id"] != fix_id]
    for finding in receipt.get("findings", []):
        path = posixpath.normpath(finding.get("file") or ".")
        if path in (".", "..") or path.startswith(("/", "../")) or path in found or path in held:
            continue
        holders = [(other, claim) for other in others for claim in other["claims"]
                   if claims.claims_overlap(path, claim)]
        if holders:
            held.append(path)
            lines.append(f"{path} stays out of the fix lane's claims: "
                         + ", ".join(f"lane {o['id']} (#{o['ticket']}) claims {c}" for o, c in holders)
                         + f"; {config.prog()} check warns if the fix changes it")
        else:
            found.append(path)
    return found, lines


def fix_base(record: Record, cwd: str) -> str:
    """The branch a fix lane starts from (lanefather#1): the one checked out in the worktree the lane landed into,
    else its registration's base when that is a branch; either must hold the landed merge. A lane registered at a
    literal commit keeps that commit as its own base, which lacks what it landed."""
    landed: dict[str, Any] = record.get("landed") or {}
    merge: str = landed.get("merge") or ""
    integration: str = landed.get("integration") or ""
    into = current_branch(integration) if integration and os.path.isdir(integration) else ""
    for ref in dict.fromkeys(r for r in (into, record["base"]) if r):
        if (merge and git_ok(cwd, "show-ref", "--verify", "--quiet", f"refs/heads/{ref}")
                and git_ok(cwd, "merge-base", "--is-ancestor", merge, ref)):
            return ref
    prog, lane_id = config.prog(), record["id"]
    if not integration:
        where = "its landed record names no integration worktree"
        todo = "check out the branch it landed on in an integration worktree and give that branch to the fix lane"
    elif not os.path.isdir(integration):
        where = f"its integration worktree {integration} is gone"
        todo = f"recreate it on the branch the lane landed on (git worktree add {integration} <branch>)"
    else:
        where = (f"its integration worktree {integration} is on {into}, which does not hold it" if into
                 else f"its integration worktree {integration} has no branch checked out")
        todo = f"check out the integration branch the lane landed on in {integration}"
    raise LaneError(f"hand-over refused: no branch holds lane {lane_id}'s merge {merge[:10]}: {where}, and its base "
                    f"{record['base']} is not such a branch; {todo}, then run {prog} review {lane_id} --fix-lane "
                    "again")


def pre_land_fix_round(args: Any, record: Record, worktree: str) -> int:
    """One truthful pre-land fix round on a reviewed active candidate (fathers#197): keeps the reviewed SHA and
    finding linkage in the lane record and review receipt without landing the defective original or running a second
    blind review."""
    prog = config.prog()
    store = state.Store(config.current())
    receipt = receipts.read(store, args.id, "review")
    verdict = receipts.review_verdict(record, receipt)
    if not isinstance(verdict, receipts.Unmarked) or receipt is None:
        raise LaneError(f"hand-over refused: lane {args.id} has no finished review with unmarked findings")
    existing: dict[str, Any] | None = receipt.get("pre_land_fix") or record.get("pre_land_fix")
    if existing:
        sha = str(existing.get("reviewed_sha") or receipt.get("tip") or "")[:10]
        raise LaneError(f"hand-over refused: lane {args.id} already has a pre-land fix round "
                        f"(reviewed commit {sha}); only one pre-land fix round is allowed per review")
    main = main_checkout(record["common_dir"])
    reviewed_sha = str(receipt.get("tip") or "")
    if not reviewed_sha or not git_ok(main, "rev-parse", "--verify", "--quiet", reviewed_sha + "^{commit}"):
        raise LaneError(f"hand-over refused: lane {args.id}'s reviewed commit {reviewed_sha[:10] or '(none)'} "
                        "is missing from Git")
    unmarked = [f["n"] for f in verdict.unmarked]
    fix_round: dict[str, Any] = {
        "reviewed_sha": reviewed_sha,
        "branch": record["branch"],
        "worktree": worktree,
        "findings": unmarked,
        "started": journal.now(),
    }

    def _set_pre_land_fix(current: Record) -> None:
        current["pre_land_fix"] = dict(fix_round)

    record = store.update(args.id, _set_pre_land_fix)

    def _attach_pre_land(current: dict[str, Any] | None) -> dict[str, Any]:
        updated = dict(current or {})
        updated["pre_land_fix"] = dict(fix_round)
        return updated

    receipt_transition(store, args.id, "fix-lane", _attach_pre_land)
    journal.ledger(f"review-pre-land-fix {args.id} reviewed_sha={reviewed_sha} worktree={worktree}",
                   kind="review.pre_land_fix", lane=record,
                   details={"reviewed_sha": reviewed_sha, "branch": record["branch"],
                            "worktree": worktree, "findings": unmarked})
    numbered = ", ".join(f"#{n}" for n in unmarked)
    print(f"Pre-land fix round opened for active lane {args.id} in {worktree} "
          f"(reviewed commit {reviewed_sha[:10]} preserved; findings {numbered}).")
    print(f"Commit the fix on {record['branch']} on top of {reviewed_sha[:10]} (do not amend away the reviewed "
          f"commit), run {prog} check {args.id}, mark each finding ("
          + mark_help(args.id).replace("<merge>", "<fix-commit>|" + args.id)
          + f"), then {prog} land {args.id} (its review is kept without a second blind review).")
    return 0


def hand_over(args: Any) -> int:
    """The fix round: for a landed lane, a follow-up lane <id>-review-fixes takes over the landed lane's worktree,
    where the resumed agent keeps its context (a resumed agent is fenced to its own worktree); its land is the fix
    and gets no review of its own. For an active reviewed candidate (fathers#197), opens one truthful pre-land fix
    round on the same active lane, preserving the reviewed SHA and finding linkage."""
    store = state.Store(config.current())
    prog = store.project.command
    record = store.load(args.id)
    status = record["status"]
    if not (lifecycle.active(record) or lifecycle.awaiting_done(record)):
        raise LaneError(f"hand-over refused: lane {args.id} has unsupported status {status!r} "
                        "(must be active, landed or released)")
    worktree = record.get("worktree")
    if not worktree:
        raise LaneError(f"hand-over refused: lane {args.id} ({status}) has no worktree to hand over")
    if not os.path.isdir(worktree):
        raise LaneError(f"hand-over refused: lane {args.id}'s worktree {worktree} does not exist")
    if lifecycle.active(record):
        return pre_land_fix_round(args, record, worktree)
    receipt = fix_parent_review(args.id, record["common_dir"], "hand-over refused", store=store)
    fix_id = f"{args.id}-review-fixes"
    main = main_checkout(record["common_dir"])
    fix_paths, held = fix_claims(receipt, fix_id, record["common_dir"])
    fix, record = lanes.hand_over_fix_lane(store, record, fix_id, fix_paths, fix_base(record, main), args.force_reason)
    for line in held:
        print(f"lane: {line}")

    def _attach_fix_lane(current: dict[str, Any] | None) -> dict[str, Any]:
        updated = dict(current or {})
        updated["fix_lane"] = fix_id
        return updated

    receipt_transition(store, args.id, "fix-lane", _attach_fix_lane)
    journal.ledger(f"review-hand-over {args.id} to {fix_id} worktree={fix['worktree']}", kind="review.handed_over",
                   lane=record, details={"to": fix_id, "worktree": fix["worktree"]})
    print(f"The fix round runs in {fix['worktree']}: git checkout -B {fix['branch']} {fix['base']}, fix each finding "
          f"that holds, commit, then {prog} check {fix_id} and report. Its land needs no review. Then mark "
          f"{args.id}'s findings: " + mark_help(args.id).replace("<merge>", fix_id + "|<merge>"))
    return 0


def ship_review_label(sha: str) -> str:
    return f"ship-{sha[:12]}"


def start_overall_review(cwd: str, sha: str, base: str) -> str:
    """After promote --ship: the one overall review of base..sha in the background; it never holds the ship."""
    prog = config.prog()
    store = state.Store(config.current())
    label = ship_review_label(sha)
    current = receipts.read(store, label, "review")
    verdict = receipts.review_verdict(None, current)
    if isinstance(verdict, (receipts.Unmarked, receipts.Complete)):
        return (f"overall review of {sha[:10]} already ran: " + ticket_state(retry_ticket(label, common_dir(cwd)))
                + f"; {prog} review --overall {sha[:10]} prints it")
    if isinstance(verdict, receipts.Running):
        return f"overall review of {sha[:10]} is running (pid {verdict.pid}, log {verdict.log})"
    # A failed or killed review keeps its base: after the push, origin/main is the SHA itself.
    base = (current or {}).get("base") or base
    if git_ok(cwd, "merge-base", "--is-ancestor", sha, base):
        return f"no overall review: {base[:10]} already holds {sha[:10]}"
    argv = ["--overall", sha, "--since", base]
    if review_start_manual():
        return (f"overall review not started ({config.current().env_names('REVIEW_START')}=manual): run {prog} review "
                f"{' '.join(argv)}")
    receipt = spawn_review(label, {"kind": "overall", "sha": sha, "base": base}, argv, cwd)
    return (f"overall review of {base[:10]}..{sha[:10]} started in the background (pid {receipt['pid']}, log "
            f"{receipt['log']}); its findings become one ticket")


def file_ticket(receipt: dict[str, Any], common: str) -> None:
    """The overall review's findings as one issue, a checklist, for the queue (owner decision on #570)."""
    findings = receipt.get("findings", [])
    if not findings or receipt.get("issue"):
        return
    items: list[str] = []
    for f in findings:
        where = (f" `{f['file']}:{f['line']}`" if f.get("file") and f.get("line")
                 else f" `{f['file']}`" if f.get("file") else "")
        category = f" `{f['category']}`" if f.get("category") else ""
        items.append(f"- [ ] **{f['severity']}**{category}{where} {f['title']}")
        items += [f"  {line}" for line in f.get("detail", "").splitlines() if line.strip()]
    body = (f"Found by the overall review before main (`{config.prog()} promote --ship`) of "
            f"{receipt['base'][:10]}..{receipt['tip'][:10]}, by {receipt.get('model')} at {receipt.get('effort')} "
            f"effort. For {config.current().queue_name}: split what holds into tickets there, and tick each item "
            f"once it is fixed or rejected.\n\n" + "\n".join(items) + "\n")
    issue_url, issue_error = github.issue_create(
        f"Overall review of {receipt['tip'][:10]}: {len(findings)} findings",
        body,
        common=common,
    )
    if issue_url:
        receipt["issue"] = issue_url
        receipt.pop("issue_error", None)
    else:
        receipt["issue_error"] = issue_error
    journal.ledger(f"review-ticket {receipt['label']} " + (f"filed {receipt['issue']}" if receipt.get("issue")
                                                            else f"failed: {receipt['issue_error']}"),
                   kind="review.ticket_filed", level="info" if receipt.get("issue") else "warn",
                   subject={"type": "review", "id": receipt["label"]},
                   details={"issue": receipt.get("issue"), "error": receipt.get("issue_error")})


def retry_ticket(label: str, common: str) -> dict[str, Any]:
    """File the ticket of a finished overall review whose filing failed, without reviewing again."""
    store = state.Store(config.current())
    with store.locked(f"review-{label}"):
        receipt = receipts.read(store, label, "review") or {}
        if receipt.get("findings") and not receipt.get("issue"):
            file_ticket(receipt, common)
            return receipt_transition(store, label, "ticket", lambda _current: receipt)
    return receipt


def ticket_state(receipt: dict[str, Any]) -> str:
    if not receipt.get("findings"):
        return "no findings"
    return (f"ticket {receipt['issue']}" if receipt.get("issue")
            else f"the ticket is not filed ({receipt.get('issue_error')})")


def overall_review(args: Any) -> int:
    prog = config.prog()
    store = state.Store(config.current())
    cwd = os.getcwd()
    common = common_dir(cwd)
    sha = git(cwd, "rev-parse", "--verify", args.overall + "^{commit}")
    label = ship_review_label(sha)
    current = receipts.read(store, label, "review")
    verdict = receipts.review_verdict(None, current)
    if isinstance(verdict, (receipts.Unmarked, receipts.Complete)) and not args.force_reason:
        receipt = retry_ticket(label, common)
        print("\n".join(review_lines(receipt)))
        return 1 if receipt.get("issue_error") else 0
    if isinstance(verdict, receipts.Running) and verdict.pid != os.getpid() and not args.force_reason:
        assert current is not None
        print("\n".join(review_lines(current)))
        return 0
    # A rerun of a failed or killed review keeps its base.
    since = args.since or (current or {}).get("base")
    if not since:
        raise LaneError("give --since <base>: the overall review covers <base>..<sha> "
                        "(promote --ship gives origin/main)")
    base = git(cwd, "merge-base", git(cwd, "rev-parse", "--verify", since + "^{commit}"), sha)
    subject = (f"The overall review before main ({prog} promote --ship): every change from {base[:10]} to "
               f"{sha[:10]}, many lanes at once. Look for seams between them as well as bugs in each.")
    receipt = perform_review(label, {"kind": "overall", "sha": sha}, main_checkout(common), base, sha, subject,
                             finalize=lambda finished: file_ticket(finished, common), reviewers=asked_reviewers(args))
    print("\n".join(review_lines(receipt)))
    finished_ok = isinstance(receipts.review_verdict(None, receipt), (receipts.Unmarked, receipts.Complete))
    return 0 if finished_ok and not receipt.get("issue_error") else 1


def review(args: Any) -> int:
    if args.overall:
        return overall_review(args)
    if not args.id:
        raise LaneError("give a lane id, or --overall <sha> --since <base>")
    reviewer_flag = getattr(args, "reviewer", None)
    if reviewer_flag and (args.mark or getattr(args, "compare", False) or args.fix_lane):
        raise LaneError("--reviewer chooses who reviews; it does not combine with --mark, --compare or --fix-lane")
    if getattr(args, "compare", False):
        return compare(args)
    if args.mark:
        return mark_review(args)
    if args.fix_lane:
        return hand_over(args)
    return review_lane(args)


def landing_findings(record: Record, store: state.Store) -> tuple[list[LaneFinding], list[LaneFinding]]:
    """The open findings of other lanes here. A valid parent cannot hold the fix that will resolve its findings."""
    parents = set(valid_fix_parents(record, store))
    other = (r for r in store.lanes() if r.get("common_dir") == record["common_dir"] and r["id"] != record["id"])
    held: list[LaneFinding] = []
    reported: list[LaneFinding] = []
    for lane, findings in unmarked_reviews(other, store):
        for finding in findings:
            if finding.get("severity", "P2") in ("P1", "P2"):
                if lane["id"] not in parents:
                    held.append((lane, finding))
            else:
                reported.append((lane, finding))
    return held, reported


def fix_round_commands(records: Iterable[Record], alongside: Iterable[str] = ()) -> list[str]:
    prog = config.prog()
    lines: list[str] = []
    landed: list[Record] = []
    for record in records:
        landing: dict[str, Any] = record.get("landed") or {}
        if landing.get("merge"):
            landed.append(record)
        else:
            lines.append(f"Apply the fix on {record['branch']} and mark {record['id']}'s findings before another land.")
    if not landed:
        return lines
    parents = list(dict.fromkeys([*alongside, *(record["id"] for record in landed)]))
    ticket = "<ticket>"
    if len(parents) == 1:
        [parent] = landed
        ticket = parent["ticket"]
        if lifecycle.awaiting_done(parent) and parent.get("worktree") and os.path.isdir(parent["worktree"]):
            lines.append(f"{prog} review {parent['id']} --fix-lane")
    lines.append(f"{prog} start <fix-id> --fix-of {','.join(parents)} --ticket {ticket} --claims <paths> --create")
    return lines
