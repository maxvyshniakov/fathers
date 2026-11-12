"""The independent review (#570): one review per lane beside its land, gating the move of dev; one overall review on
promote --ship, whose findings become one ticket.

The reviewer is the `review` role of the shared models.toml (mastack): today Sol 6.1 at medium effort while Codex
has at least 15% of its weekly limit left, else Opus 5.5 at medium effort. The lane review gets the lane's diff
against the last integration commit it merged, its ticket and the coding standards, never the lane's report, in a
checkout detached at the lane tip. It asks `codex app-server` for the weekly limit and runs `codex exec` in a
read-only sandbox; under the limit, a reached limit, no Codex CLI, a failed Sol run or an empty answer runs `claude
-p` instead. A lane that changes only *.md and docs/**, none of them a steering file, and a fix lane are exempt.
LANEFATHER_REVIEW_START=manual makes land and promote --ship print the review command instead of starting it.
"""
from __future__ import annotations

from collections.abc import Callable
import json
import os
from pathlib import Path
import posixpath
import re
import subprocess
import sys
import time
from typing import Any

from . import claims, config, done, journal, lanes, reviewer, settings, state, worktrees
from .checks import terminable
from .errors import LaneError
from .gitops import common_dir, current_branch, git, git_ok, main_checkout
from .state import Record

SEVERITIES = ("P1", "P2", "P3")


def mark_help(lane_id: str = "{id}") -> str:
    return f"{config.prog()} review {lane_id} --mark <n> fixed:<merge>|rejected:<reason>"



def gh_command() -> str:
    return config.current().env("GH") or "gh"


def repo_args(common: str) -> list[str]:
    """`-R owner/name` for gh, from the origin remote; [] when origin is not on GitHub."""
    origin = git(main_checkout(common), "remote", "get-url", "origin", check=False)
    found = re.search(r"github\.com[:/]([^/]+/[^/.]+)", origin)
    return ["-R", found.group(1)] if found else []


def alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def review_state(label: str) -> tuple[bool, str]:
    """Whether the review lets promote move dev, and its state in words."""
    prog = config.prog()
    receipt = state.read_json(state.receipt_path(label, "review"))
    if receipt is None:
        return False, f"no review: run {prog} review {label}"
    status = receipt.get("status")
    if status == "exempt":
        return True, f"review exempt: {receipt.get('exempt')}"
    if status == "running":
        if alive(receipt.get("pid")):
            return False, (f"review running since {receipt.get('started')} (pid {receipt.get('pid')}, "
                           f"{receipt.get('log')})")
        return False, f"review died (pid {receipt.get('pid')}, {receipt.get('log')}): rerun {prog} review {label}"
    if status != "finished":
        return False, f"review failed: {receipt.get('error')}; rerun {prog} review {label}"
    findings = receipt.get("findings", [])
    unmarked = [f for f in findings if not f.get("mark")]
    if unmarked:
        return False, (f"{len(unmarked)} of {len(findings)} review findings unmarked "
                       f"({', '.join('#' + str(f['n']) for f in unmarked)})")
    return True, f"reviewed by {receipt.get('reviewer')}: {len(findings)} findings, all marked"


def steering() -> tuple[str, ...]:
    cfg = config.current()
    return cfg.steering + ((cfg.rules_dir.rstrip("/") + "/**",) if cfg.rules_dir else ())


def documentation_only(paths: list[str]) -> bool:
    """Whether a merge changes only documentation, *.md and docs/**, and none of the steering files."""
    globs = steering()
    return all((p.endswith(".md") or p.startswith("docs/")) and not any(claims.matches(s, p) for s in globs)
               for p in paths)


def review_exemption(record: Record) -> str | None:
    if record.get("review_fix_of"):
        return f"the fix round of {record['review_fix_of']}'s review, which nobody reviews again"
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
    result = subprocess.run([gh_command(), "issue", "view", record["ticket"], "--json", "title,body",
                             *repo_args(record["common_dir"])], capture_output=True, text=True)
    try:
        data = json.loads(result.stdout) if result.returncode == 0 else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return f"Ticket #{record['ticket']} (gh could not read it: {result.stderr.strip()[:300]})"
    return f"Ticket #{record['ticket']}: {data.get('title', '')}\n\n{data.get('body', '')}".strip()  # pyright: ignore[reportUnknownMemberType]


def review_about(label: str, fields: dict[str, Any]) -> dict[str, Any]:
    """The subject of a review's events: its lane's for a lane review, else the review itself (an overall one)."""
    if fields.get("kind") == "lane" and fields.get("lane"):
        return {"lane": str(fields["lane"])}
    return {"subject": {"type": "review", "id": label, **({"kind": fields["kind"]} if fields.get("kind") else {})}}


def perform_review(label: str, fields: dict[str, Any], main: str, base: str, tip: str, subject: str,
                   finalize: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Review base...tip with Sol, or Opus when Sol may not or cannot run; write and return the receipt.
    `finalize` completes a finished receipt before anyone can read it as finished."""
    runner = reviewer.Reviewer(config.current())
    path = state.receipt_path(label, "review")
    receipt: dict[str, Any] = {"version": 1, "label": label, **fields, "base": base, "tip": tip,
                               "tree": git(main, "rev-parse", tip + "^{tree}"), "status": "running",
                               "pid": os.getpid(), "log": config.current().env("REVIEW_LOG"),
                               "started": journal.now(), "effort": runner.models["sol_effort"]}
    with state.locked(f"review-{label}"):
        state.write_json(path, receipt)
    reviews = state.state_dir() / "reviews"
    reviews.mkdir(exist_ok=True)
    output, log = reviews / f"{label}.md", reviews / f"{label}.log"
    log.write_text("")
    clock = time.monotonic()
    try:
        with terminable(), worktrees.review_checkout(main, label, tip) as checkout:
            standards = coding_standards(main, tip)
            answer = runner.run(label, checkout, reviewer.Request(standards, subject, base, tip), output, log)
            receipt.update(answer.evidence)
            if answer.error is not None:
                raise LaneError(answer.error)
        receipt.update(status="finished", output=str(output))
        if finalize:
            finalize(receipt)
    except Exception as error:  # any failure leaves a failed receipt, not a running one
        receipt.update(status="failed", error=str(error), output=str(output))
    receipt.setdefault("fallback", None)
    receipt.update(seconds=round(time.monotonic() - clock, 1), finished=journal.now())
    with state.locked(f"review-{label}"):
        state.write_json(path, receipt)
    findings = receipt.get("findings", [])
    counts = " ".join(f"{s.lower()}={sum(1 for f in findings if f['severity'] == s)}" for s in SEVERITIES)
    journal.ledger(f"review {label} {receipt['status']} reviewer={receipt.get('reviewer', '-')} "
                   f"model={receipt.get('model', '-')} effort={receipt['effort']} "
                   f"left={'-' if receipt.get('codex_left') is None else receipt['codex_left']} "
                   f"seconds={receipt['seconds']:.0f} findings={len(findings)} {counts}"
                   + (f" fallback: {receipt['fallback']}" if receipt["fallback"] else ""),
                   kind="review.finished", level="info" if receipt["status"] == "finished" else "warn",
                   **review_about(label, fields),
                   details={"status": receipt["status"], "reviewer": receipt.get("reviewer"),
                            "model": receipt.get("model"), "effort": receipt["effort"],
                            "seconds": receipt["seconds"], "findings": len(findings),
                            "severities": {s.lower(): sum(1 for f in findings if f["severity"] == s)
                                           for s in SEVERITIES},
                            "fallback": receipt["fallback"], "error": receipt.get("error")})
    return receipt


def review_lines(receipt: dict[str, Any]) -> list[str]:
    label = receipt["label"]
    if receipt["status"] == "exempt":
        return [f"Review of {label}: exempt ({receipt['exempt']})"]
    if receipt["status"] == "running":
        return [f"Review of {label}: running since {receipt.get('started')} (pid {receipt.get('pid')}, log "
                f"{receipt.get('log')})"]
    left = receipt.get("codex_left")
    head = (f"Review of {label} ({receipt.get('base', '')[:10]}...{receipt.get('tip', '')[:10]}) by "
            f"{receipt.get('model')} {receipt.get('effort')}"
            + (f", {left}% of the weekly Codex limit left" if left is not None else "")
            + (f"; fallback: {receipt['fallback']}" if receipt.get("fallback") else "")
            + f", {receipt.get('seconds', 0):.0f} s")
    if receipt["status"] != "finished":
        return [head, f"review failed: {receipt.get('error')}"]
    findings = receipt.get("findings", [])
    lines = [head + f": {len(findings)} findings  ({receipt.get('output')})"]
    for f in findings:
        where = f"{f['file']}:{f['line']}" if f.get("file") and f.get("line") else f.get("file") or "-"
        mark = f.get("mark")
        marked = ("" if not mark else f"  [fixed: {mark['merge'][:10]}]" if mark["kind"] == "fixed"
                  else f"  [rejected: {mark['reason']}]")
        lines.append(f"  {f['n']}. {f['severity']} {where} {f['title']}{marked}")
        lines += [f"     {line}" for line in f.get("detail", "").splitlines()[:6]]
    if receipt.get("issue"):
        lines.append(f"Ticket: {receipt['issue']}")
    elif receipt.get("issue_error"):
        lines.append(f"Ticket not filed: {receipt['issue_error']}; {config.prog()} review --overall "
                     f"{receipt.get('tip', '')[:10]} files it again")
    if receipt.get("kind") == "lane" and any(not f.get("mark") for f in findings):
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
    log = journal.log_path(f"review-{label}")
    # The child writes its own receipt under the same lock, so this running one never replaces a finished one.
    with state.locked(f"review-{label}"), open(log, "w") as out:
        child = subprocess.Popen([*self_argv(), "review", *argv], cwd=cwd,
                                 stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                 start_new_session=True, env=self_env(LANEFATHER_REVIEW_LOG=str(log)))
        receipt = {"version": 1, "label": label, **fields, "status": "running", "pid": child.pid, "log": str(log),
                   "started": journal.now(), "effort": reviewer.Reviewer(config.current()).models["sol_effort"]}
        state.write_json(state.receipt_path(label, "review"), receipt)
    journal.ledger(f"review-start {label} pid={child.pid} log={log}", kind="review.started",
                   **review_about(label, fields), details={"pid": child.pid, "log": str(log)})
    return receipt


def start_lane_review(record: Record) -> str:
    """At land: an exempt receipt, or the review in the background. The line land prints."""
    lane_id = record["id"]
    current = state.read_json(state.receipt_path(lane_id, "review")) or {}
    if current.get("status") in ("finished", "exempt") or (current.get("status") == "running"
                                                          and alive(current.get("pid"))):
        # Reviewed before landing (onto dev or main), or by hand: one review per lane, kept as it is.
        journal.ledger(f"review {lane_id} kept at land: {current['status']} since "
                       f"{current.get('started') or current.get('finished')}", kind="review.kept", lane=record,
                       details={"status": current["status"],
                                "since": current.get("started") or current.get("finished")})
        return f"review kept: {review_state(lane_id)[1]}"
    exempt = review_exemption(record)
    if exempt:
        state.write_json(state.receipt_path(lane_id, "review"),
                         {"version": 1, "label": lane_id, "kind": "lane", "lane": lane_id, "ticket": record["ticket"],
                          "status": "exempt", "exempt": exempt, "finished": journal.now()})
        journal.ledger(f"review {lane_id} exempt: {exempt}", kind="review.exempted", lane=record,
                       details={"exempt": exempt})
        return f"review exempt: {exempt}"
    if review_start_manual():
        return (f"review not started ({review_start_source()}): run "
                f"{config.prog()} review {lane_id}")
    receipt = spawn_review(lane_id, {"kind": "lane", "lane": lane_id, "ticket": record["ticket"]}, [lane_id],
                           main_checkout(record["common_dir"]))
    return f"review started in the background (pid {receipt['pid']}, log {receipt['log']})"


def review_complete_finish(lane_id: str) -> None:
    """After the lane's review is clean or fully marked: finish the lane as land would have, unless land is still
    running its smoke (land finishes it then), kept it (--keep), or its smoke was red."""
    record = state.load(lane_id)
    if record["status"] not in ("landed", "released"):
        return
    if (record.get("landed") or {}).get("keep"):
        print(f"Lane {lane_id} kept (--keep): finish it with {config.prog()} done {lane_id}")
        return
    if (record.get("post_merge") or {}).get("status") != "green":
        return
    done.finish_landed(lane_id, False)


def review_lane(args: Any) -> int:
    record = state.load(args.id)
    current = state.read_json(state.receipt_path(args.id, "review")) or {}
    status, pid = current.get("status"), current.get("pid")
    # The background run land started finds its own pid in the receipt.
    elsewhere = status == "running" and alive(pid) and pid != os.getpid()
    if (status in ("finished", "exempt") or elsewhere) and not args.force_reason:
        print("\n".join(review_lines(current)))
        if status == "finished":
            print(f"Lane {args.id} is already reviewed: one review per lane (--force-reason reviews again, logged)")
        return 0
    if status == "finished":
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
                             main_checkout(record["common_dir"]), base, tip, ticket_text(record))
    print("\n".join(review_lines(receipt)))
    if receipt["status"] != "finished":
        return 1
    if review_state(args.id)[0]:
        review_complete_finish(args.id)
    return 0


def fix_merge(common: str, value: str) -> str:
    """The commit a `fixed:` mark names: a landed lane's merge (the fix lane's id) or a commit."""
    record = state.read_json(state.record_path(value)) if state.ID.match(value) else None
    if record and (record.get("landed") or {}).get("merge"):
        return record["landed"]["merge"]
    sha = git(main_checkout(common), "rev-parse", "--verify", "--quiet", value + "^{commit}", check=False)
    if not value or not sha:
        raise LaneError(f"fixed:{value}: not a commit or a landed lane here; give the follow-up merge")
    return sha


def mark_review(args: Any) -> int:
    record = state.load(args.id)
    path = state.receipt_path(args.id, "review")
    with state.locked(f"review-{args.id}"):
        receipt = state.read_json(path)
        if not receipt or receipt.get("status") != "finished":
            raise LaneError(f"lane {args.id} has no finished review to mark ({review_state(args.id)[1]})")
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
                marks.append((finding, {"kind": "fixed", "merge": fix_merge(record["common_dir"], detail),
                                        "at": journal.now()}))
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
        state.write_json(path, receipt)
    print("\n".join(review_lines(receipt)))
    complete, words = review_state(args.id)
    if complete:
        kinds = [f["mark"]["kind"] for f in receipt.get("findings", [])]
        journal.ledger(f"review-marked {args.id} fixed={kinds.count('fixed')} rejected={kinds.count('rejected')}",
                       kind="review.marked", lane=args.id,
                       details={"fixed": kinds.count("fixed"), "rejected": kinds.count("rejected")})
        review_complete_finish(args.id)
    else:
        print(f"Lane {args.id}: {words}")
    return 0


def fix_claims(receipt: dict[str, Any], fix_id: str) -> tuple[list[str], list[str]]:
    """The fix lane's claims (#586): the repository files the review's findings name, never the landed lane's
    claims, which hold every path its check widened to and so overlap lanes started since. A named file another
    planned or active lane claims stays out, with a line saying so; check widens the claims from the fix's own
    diff and warns if that touches another lane's claim (#574). Returns the claims and those lines."""
    found: list[str] = []
    held: list[str] = []
    lines: list[str] = []
    others = [r for r in state.occupying() if r["id"] != fix_id]
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


def hand_over(args: Any) -> int:
    """The fix round: a follow-up lane <id>-review-fixes takes over the landed lane's worktree, where the resumed
    agent keeps its context (a resumed agent is fenced to its own worktree); its land is the fix and gets no
    review of its own."""
    prog = config.prog()
    record = state.load(args.id)
    receipt = state.read_json(state.receipt_path(args.id, "review"))
    if not receipt or receipt.get("status") != "finished" or not receipt.get("findings"):
        raise LaneError(f"hand-over refused: lane {args.id} has no finished review with findings")
    if record["status"] not in ("landed", "released") or not record.get("worktree") or not os.path.isdir(
            record["worktree"]):
        raise LaneError(f"hand-over refused: lane {args.id} is {record['status']} and has no worktree to hand over")
    fix_id = f"{args.id}-review-fixes"
    main = main_checkout(record["common_dir"])
    fix_paths, held = fix_claims(receipt, fix_id)
    fix, rules = lanes.prepare(main, fix_id, record["ticket"], fix_paths, fix_base(record, main), [])
    fix.update(review_fix_of=args.id, worktree=record["worktree"])
    lanes.register(fix, rules, args.force_reason, False)
    for line in held:
        print(f"lane: {line}")
    with state.locked("state"):
        record = state.load(args.id)
        record.update(worktree=None, handed_over={"to": fix_id, "worktree": fix["worktree"], "at": journal.now()})
        state.save(record)
    with state.locked(f"review-{args.id}"):
        receipt = state.read_json(state.receipt_path(args.id, "review")) or {}
        receipt["fix_lane"] = fix_id
        state.write_json(state.receipt_path(args.id, "review"), receipt)
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
    label = ship_review_label(sha)
    current = state.read_json(state.receipt_path(label, "review")) or {}
    if current.get("status") == "finished":
        return (f"overall review of {sha[:10]} already ran: " + ticket_state(retry_ticket(label, common_dir(cwd)))
                + f"; {prog} review --overall {sha[:10]} prints it")
    if current.get("status") == "running" and alive(current.get("pid")):
        return f"overall review of {sha[:10]} is running (pid {current['pid']}, log {current.get('log')})"
    # A failed or killed review keeps its base: after the push, origin/main is the SHA itself.
    base = current.get("base") or base
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
        items.append(f"- [ ] **{f['severity']}**{where} {f['title']}")
        items += [f"  {line}" for line in f.get("detail", "").splitlines() if line.strip()]
    body = (f"Found by the overall review before main (`{config.prog()} promote --ship`) of "
            f"{receipt['base'][:10]}..{receipt['tip'][:10]}, by {receipt.get('model')} at {receipt.get('effort')} "
            f"effort. For {config.current().queue_name}: split what holds into tickets there, and tick each item "
            f"once it is fixed or rejected.\n\n" + "\n".join(items) + "\n")
    created = subprocess.run([gh_command(), "issue", "create", *repo_args(common), "--title",
                              f"Overall review of {receipt['tip'][:10]}: {len(findings)} findings", "--body", body],
                             capture_output=True, text=True)
    if created.returncode == 0 and created.stdout.strip():
        receipt["issue"] = created.stdout.strip().splitlines()[-1]
        receipt.pop("issue_error", None)
    else:
        receipt["issue_error"] = ((created.stderr or created.stdout).strip() or f"gh exit {created.returncode}")[:300]
    journal.ledger(f"review-ticket {receipt['label']} " + (f"filed {receipt['issue']}" if receipt.get("issue")
                                                            else f"failed: {receipt['issue_error']}"),
                   kind="review.ticket_filed", level="info" if receipt.get("issue") else "warn",
                   subject={"type": "review", "id": receipt["label"]},
                   details={"issue": receipt.get("issue"), "error": receipt.get("issue_error")})


def retry_ticket(label: str, common: str) -> dict[str, Any]:
    """File the ticket of a finished overall review whose filing failed, without reviewing again."""
    with state.locked(f"review-{label}"):
        receipt = state.read_json(state.receipt_path(label, "review")) or {}
        if receipt.get("findings") and not receipt.get("issue"):
            file_ticket(receipt, common)
            state.write_json(state.receipt_path(label, "review"), receipt)
    return receipt


def ticket_state(receipt: dict[str, Any]) -> str:
    if not receipt.get("findings"):
        return "no findings"
    return (f"ticket {receipt['issue']}" if receipt.get("issue")
            else f"the ticket is not filed ({receipt.get('issue_error')})")


def overall_review(args: Any) -> int:
    prog = config.prog()
    cwd = os.getcwd()
    common = common_dir(cwd)
    sha = git(cwd, "rev-parse", "--verify", args.overall + "^{commit}")
    label = ship_review_label(sha)
    current = state.read_json(state.receipt_path(label, "review")) or {}
    status, pid = current.get("status"), current.get("pid")
    if status == "finished" and not args.force_reason:
        receipt = retry_ticket(label, common)
        print("\n".join(review_lines(receipt)))
        return 1 if receipt.get("issue_error") else 0
    if status == "running" and alive(pid) and pid != os.getpid() and not args.force_reason:
        print("\n".join(review_lines(current)))
        return 0
    # A rerun of a failed or killed review keeps its base.
    since = args.since or current.get("base")
    if not since:
        raise LaneError("give --since <base>: the overall review covers <base>..<sha> "
                        "(promote --ship gives origin/main)")
    base = git(cwd, "merge-base", git(cwd, "rev-parse", "--verify", since + "^{commit}"), sha)
    subject = (f"The overall review before main ({prog} promote --ship): every change from {base[:10]} to "
               f"{sha[:10]}, many lanes at once. Look for seams between them as well as bugs in each.")
    receipt = perform_review(label, {"kind": "overall", "sha": sha}, main_checkout(common), base, sha, subject,
                             finalize=lambda finished: file_ticket(finished, common))
    print("\n".join(review_lines(receipt)))
    return 0 if receipt["status"] == "finished" and not receipt.get("issue_error") else 1


def review(args: Any) -> int:
    if args.overall:
        return overall_review(args)
    if not args.id:
        raise LaneError("give a lane id, or --overall <sha> --since <base>")
    if args.mark:
        return mark_review(args)
    if args.fix_lane:
        return hand_over(args)
    return review_lane(args)


def review_refusals(cwd: str, common: str, sha: str) -> list[str]:
    """The lanes landed since dev and in the SHA whose review is missing, unfinished or has an unmarked finding."""
    dev = git(cwd, "rev-parse", "--verify", "--quiet", "refs/heads/dev^{commit}", check=False)
    refusals: list[str] = []
    for record in state.lanes():
        merge = (record.get("landed") or {}).get("merge")
        if not merge or record.get("common_dir") != common:
            continue
        if not git_ok(cwd, "merge-base", "--is-ancestor", merge, sha):
            continue
        if dev and git_ok(cwd, "merge-base", "--is-ancestor", merge, dev):
            continue
        complete, words = review_state(record["id"])
        if not complete:
            refusals.append(f"lane {record['id']} (#{record['ticket']}): {words}")
            continue
        # A finding marked fixed is fixed in what moves only when its follow-up merge is in the SHA.
        for finding in (state.read_json(state.receipt_path(record["id"], "review")) or {}).get("findings", []):
            mark = finding.get("mark") or {}
            if mark.get("kind") == "fixed" and not git_ok(cwd, "merge-base", "--is-ancestor", mark["merge"], sha):
                refusals.append(f"lane {record['id']} (#{record['ticket']}): finding #{finding['n']} is marked fixed "
                                f"by {mark['merge'][:10]}, which is not in {sha[:10]}")
    return refusals
