"""The independent review (#570): one review per lane beside its land, gating the move of dev; one overall review on
promote --ship, whose findings become one ticket.

The reviewer is the `review` role of the shared models.toml (mastack): today Sol 6.1 at medium effort while Codex
has at least 15% of its weekly limit left, else Opus 5.5 at medium effort. The lane review gets the lane's diff
against the last integration commit it merged, its accepted scope (fathers#192: its tickets without their comments,
its decisions, its specs at the base) and the coding standards, never the lane's report, in a checkout detached at
the lane tip. It asks `codex app-server` for the weekly limit and runs `codex exec` in a
read-only sandbox; under the limit, a reached limit, no Codex CLI, a failed Sol run or an empty answer runs `claude
-p` instead. A lane that changes only *.md and docs/**, none of them a steering file, and a fix lane are exempt.
LANEFATHER_REVIEW_START=manual makes land and promote --ship print the review command instead of starting it.
Mastack's `role` command, beside the models file, chooses the reviewer when present (fathers#150); `[review] panel`
runs several reviewers at once, their findings numbered in one list for marks and the gate (fathers#151).
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Sequence
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
    done,
    github,
    journal,
    lanes,
    lifecycle,
    postmerge,
    receipts,
    review_class,
    review_record,
    reviewer,
    scope,
    settings,
    state,
    worktrees,
)
from .errors import LaneError
from .gitops import common_dir, current_branch, dirty, git, git_ok, main_checkout
from .state import Record
from .verify import terminable

SEVERITIES = review_record.SEVERITIES
LaneFinding = tuple[Record, dict[str, Any]]


def asked_reviewers(args: Any, project: config.Config = config.DEFAULT) -> tuple[str, ...]:
    """`--reviewer ALIAS|both` (fathers#155): the reviewers this one review runs whatever the Codex limit; `both`
    is the configured panel, else Sol and Opus. () lets the panel or the models file choose."""
    asked: str | None = getattr(args, "reviewer", None)
    if not asked:
        return ()
    if asked == "both":
        return project.review_panel or ("sol", "opus")
    return (asked,)


# The family of a harness that runs one family only (lanefather.events.actor), for the role's `not_family`. T3 Code
# runs Claude, Codex or Antigravity, so only its recorded model tells (fathers#201).
FAMILIES = {"claude-code": "anthropic", "codex": "openai", "antigravity": "google"}
MODEL_FAMILIES = (("claude-", "anthropic"), ("gpt-", "openai"), ("gemini-", "google"))


def _agent_family(kind: str, model: str) -> str | None:
    """The family of one recorded agent: its model's when the model names one, else its harness's when that runs
    one family only."""
    lowered = model.strip().lower()
    for prefix, family in MODEL_FAMILIES:
        if lowered.startswith(prefix):
            return family
    return FAMILIES.get(kind)


def implementer_family(store: state.Store, lane_id: str) -> str | None:
    """The implementer's model family: that of the agent whose `check` wrote the lane's receipt (its recorded model
    first, then a single-family harness), else of the lane's latest agent that tells; None unknown."""
    record = state.read_json(store.record_path(lane_id)) or {}
    check_data = receipts.read(store, lane_id, record=record) or {}
    checker = str(check_data.get("agent") or "")
    raw_agents: list[Any] = record.get("agents") or []
    agents = [cast(dict[str, Any], e) for e in reversed(raw_agents) if isinstance(e, dict)]
    if checker:
        for entry in agents:
            if str(entry.get("kind") or "") == checker:
                family = _agent_family(checker, str(entry.get("model") or ""))
                if family:
                    return family
        if checker in FAMILIES:
            return FAMILIES[checker]
    for entry in agents:
        family = _agent_family(str(entry.get("kind") or ""), str(entry.get("model") or ""))
        if family:
            return family
    return None


def steering(project: config.Config = config.DEFAULT) -> tuple[str, ...]:
    return project.steering + ((project.rules_dir.rstrip("/") + "/**",) if project.rules_dir else ())


def documentation_only(paths: list[str], project: config.Config = config.DEFAULT) -> bool:
    """Whether a merge changes only documentation, *.md and docs/**, and none of the steering files."""
    globs = steering(project)
    return all(
        (p.endswith(".md") or p.startswith("docs/")) and not any(claims.matches(s, p, project=project) for s in globs)
        for p in paths
    )


def valid_fix_parents(record: Record, store: state.Store) -> Iterator[str]:
    for parent in state.fix_parents(record):
        try:
            review_record.ReviewRecord.fix_parent(
                store, parent, record["common_dir"], "review exemption refused"
            )
        except LaneError:
            continue
        yield parent


def review_exemption(record: Record, store: state.Store | None = None) -> str | None:
    parents = state.fix_parents(record)
    if store is not None and parents != list(valid_fix_parents(record, store)):
        parents = []
    if len(parents) == 1:
        return f"the fix round of {parents[0]}'s review, which nobody reviews again"
    if parents:
        return (
            f"the fix round of the reviews of {', '.join(parents[:-1])} and {parents[-1]}, which nobody reviews "
            "again"
        )
    raw_landed: Any = record.get("landed")
    landed_info = cast(dict[str, Any], raw_landed) if isinstance(raw_landed, dict) else {}
    raw_cls = landed_info.get("review_class")
    eff_cls = raw_cls if isinstance(raw_cls, str) and raw_cls in review_class.CLASS_RANK else None
    raw_why = landed_info.get("review_class_reason")
    eff_why = raw_why.strip() if isinstance(raw_why, str) and raw_why.strip() else ""
    if eff_cls == "none":
        declared = review_class.declared_class(record)
        has_rules = store is not None and store.project.review_classes.has_rules()
        if landed_info.get("docs_only") and declared is None and not has_rules:
            return "documentation only: it changes only *.md and docs/** outside the steering files"
        return f"review class none: {eff_why}" if eff_why else "review class none"
    if eff_cls == "evidence":
        detail = eff_why or review_class.evidence_summary(record)
        return f"review class evidence: {detail}"
    if landed_info.get("docs_only"):
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
        against = (
            git(main, "rev-parse", "--verify", "--quiet", record["base"] + "^{commit}", check=False)
            or record["base_sha"]
        )
    return tip, git(main, "merge-base", against, tip)


def coding_standards(main: str, tip: str, project: config.Config = config.DEFAULT) -> str:
    """The coding standards of the reviewed tree, else of the project the config belongs to."""
    name = project.coding_standards
    shown = subprocess.run(["git", "show", f"{tip}:{name}"], cwd=main, capture_output=True, text=True)
    if shown.returncode == 0:
        return shown.stdout.strip()
    path = project.path
    try:
        return (path.parent / name).read_text().strip() if path else f"(no {name})"
    except OSError:
        return f"(no {name})"


def ticket_text(record: Record, project: config.Config = config.DEFAULT, base: str | None = None) -> str:
    """What the change is for: the lane's whole accepted scope as the reviewer reads it (lanefather.scope)."""
    return scope.prompt_text(scope.resolve(record, base, project))


def review_about(label: str, fields: dict[str, Any]) -> dict[str, Any]:
    """The subject of a review's events: its lane's for a lane review, else the review itself (an overall one)."""
    if fields.get("kind") == "lane" and fields.get("lane"):
        return {"lane": str(fields["lane"])}
    return {"subject": {"type": "review", "id": label, **({"kind": fields["kind"]} if fields.get("kind") else {})}}


def perform_review(
    store: state.Store,
    label: str,
    fields: dict[str, Any],
    main: str,
    base: str,
    tip: str,
    subject: str,
    finalize: Callable[[dict[str, Any]], None] | None = None,
    family: str | None = None,
    reviewers: tuple[str, ...] = (),
    *,
    force: bool = False,
) -> review_record.ReviewRecord:
    """Review base...tip with Sol, or Opus when Sol may not or cannot run; write and return the ReviewRecord.
    `finalize` completes a finished receipt before anyone can read it as finished."""
    runner = reviewer.Reviewer(store.project)
    pid = os.getpid()
    started_rev = review_record.ReviewRecord.begin(
        store,
        label,
        {**fields, "base": base, "tip": tip, "tree": git(main, "rev-parse", tip + "^{tree}")},
        pid=pid,
        log=store.project.env("REVIEW_LOG"),
        effort=runner.effort,
        force=force,
    )
    reviews = store.reviews_dir
    reviews.mkdir(parents=True, exist_ok=True)
    output, log = reviews / f"{label}.md", reviews / f"{label}.log"
    log.write_text("")
    clock = time.monotonic()
    evidence: dict[str, Any] = {}
    error_text: str | None = None
    finished_payload: dict[str, Any] = {}
    try:
        with terminable(), worktrees.review_checkout(store, main, label, tip) as checkout:
            standards = coding_standards(main, tip, store.project)
            answer = runner.run(
                label,
                checkout,
                reviewer.Request(standards, subject, base, tip, family, reviewers),
                output,
                log,
            )
            evidence.update(answer.evidence)
            if answer.error is not None:
                raise LaneError(answer.error)
        finished_payload = {
            **(started_rev.raw or {}),
            **evidence,
            "status": "finished",
            "output": str(output),
        }
        if finalize:
            latest_raw = review_record.ReviewRecord.load(store, label).raw
            if latest_raw is None or latest_raw.get("pid") != pid:
                raise review_record.StaleReviewRunError(
                    f"review {label!r} is owned by pid {latest_raw.get('pid') if latest_raw else None}, not {pid}"
                )
            finalize(finished_payload)
    except review_record.StaleReviewRunError:
        raise
    except Exception as error:  # any failure leaves a failed receipt, not a running one
        error_text = str(error)

    seconds = round(time.monotonic() - clock, 1)
    finished_at = journal.now()
    if error_text is None:
        outcome_status = "finished"
        completed = review_record.ReviewRecord.finish(
            store,
            label,
            {**finished_payload, "seconds": seconds, "finished": finished_at},
            owner_pid=pid,
            finished=finished_at,
        )
    else:
        outcome_status = "failed"
        completed = review_record.ReviewRecord.fail(
            store,
            label,
            error_text,
            extra={**evidence, "output": str(output), "seconds": seconds, "finished": finished_at},
            owner_pid=pid,
            finished=finished_at,
        )
    raw = completed.raw or {}
    findings = completed.findings
    counts = " ".join(f"{s.lower()}={sum(1 for f in findings if f['severity'] == s)}" for s in SEVERITIES)
    raw_panel: Any = raw.get("panel")
    panel_entries = (
        [cast(dict[str, Any], e) for e in cast(list[Any], raw_panel) if isinstance(e, dict)]
        if isinstance(raw_panel, list)
        else []
    )
    journal.ledger(
        f"review {label} {outcome_status} reviewer={raw.get('reviewer', '-')} "
        f"model={raw.get('model', '-')} effort={raw['effort']} "
        f"left={'-' if raw.get('codex_left') is None else raw['codex_left']} "
        f"seconds={raw['seconds']:.0f} findings={len(findings)} {counts}"
        + (f" fallback: {raw['fallback']}" if raw["fallback"] else ""),
        kind="review.finished",
        level="info" if outcome_status == "finished" else "warn",
        **review_about(label, fields),
        details={
            "status": outcome_status,
            "reviewer": raw.get("reviewer"),
            "provider": raw.get("provider"),
            "cli": raw.get("cli"),
            "model": raw.get("model"),
            "effort": raw["effort"],
            "options": raw.get("options"),
            "seconds": raw["seconds"],
            "findings": len(findings),
            "severities": {s.lower(): sum(1 for f in findings if f["severity"] == s) for s in SEVERITIES},
            "fallback": raw["fallback"],
            "error": raw.get("error"),
            **(
                {
                    "panel": [
                        {
                            "member": e.get("member"),
                            "reviewer": e.get("reviewer"),
                            "provider": e.get("provider"),
                            "cli": e.get("cli"),
                            "status": e.get("status"),
                            "model": e.get("model"),
                            "options": e.get("options"),
                            "seconds": e.get("seconds"),
                            "findings": len(e.get("numbers") or []),
                        }
                        for e in panel_entries
                    ]
                }
                if panel_entries
                else {}
            ),
        },
        store=store,
    )
    return completed


def scope_lines(raw: dict[str, Any], record: Record | None, label: str, prog: str) -> list[str]:
    """`Scope reviewed:` from a lane receipt's `scope`, and a warning when the lane's scope changed since."""
    raw_scope: Any = raw.get("scope")
    if raw.get("kind") != "lane" or not isinstance(raw_scope, list):
        return []
    raw_entries = cast(list[Any], raw_scope)
    if not all(isinstance(e, dict) for e in raw_entries):
        return ["Scope reviewed: the receipt's scope is unreadable (entry is not an object)"]
    entries = [cast(dict[str, Any], e) for e in raw_entries]
    try:
        items = scope.from_json([e.get("item") for e in entries])
    except (ValueError, LaneError) as error:
        return [f"Scope reviewed: the receipt's scope is unreadable ({error})"]
    unseen = [f"{scope.label(item)} {e.get('status')}"
              for item, e in zip(items, entries, strict=True) if e.get("status") not in scope.READ]
    lines = [f"Scope reviewed: {scope.describe(items)}" + (f" (not seen: {'; '.join(unseen)})" if unseen else "")]
    if record is not None:
        now = scope.of(record)
        if now != items:
            lines.append(f"Warning: scope changed since the review: now {scope.describe(now)}; "
                         f"{prog} review {label} --force-reason <why> reviews it again")
    return lines


def review_lines(
    rev: review_record.ReviewRecord,
    *,
    record: Record | None = None,
    prog: str | None = None,
) -> list[str]:
    cmd_prog = prog or rev.prog
    raw = rev.raw or {}
    label = str(raw.get("label") or rev.label)
    verdict = rev.verdict
    if isinstance(verdict, review_record.Exempt):
        return [f"Review of {label}: exempt ({verdict.reason})"]
    if isinstance(verdict, (review_record.Running, review_record.Died)):
        return [f"Review of {label}: running since {verdict.started} (pid {verdict.pid}, log {verdict.log})"]
    left = raw.get("codex_left")
    raw_forced: Any = raw.get("forced")
    forced = cast(dict[str, Any], raw_forced) if isinstance(raw_forced, dict) else {}
    head = (
        f"Review of {label} ({str(raw.get('base', ''))[:10]}...{str(raw.get('tip', ''))[:10]}) by "
        f"{raw.get('model')} {raw.get('effort')}"
        + (f", {left}% of the weekly Codex limit left" if left is not None else "")
        + (f"; fallback: {raw['fallback']}" if raw.get("fallback") else "")
        + (f"; forced by --reviewer {', '.join(forced['reviewers'])}" if forced.get("by") == "flag" else "")
        + f", {raw.get('seconds', 0):.0f} s"
    )
    scoped = scope_lines(raw, record, label, cmd_prog)
    if not rev.has_finished_receipt:
        error = verdict.error if isinstance(verdict, review_record.Failed) else None
        return [head, *scoped, f"review failed: {error}"]
    findings = rev.findings
    lines = [head + f": {len(findings)} findings  ({raw.get('output')})", *scoped]
    raw_panel: Any = raw.get("panel")
    panel = (
        [cast(dict[str, Any], e) for e in cast(list[Any], raw_panel) if isinstance(e, dict)]
        if isinstance(raw_panel, list)
        else []
    )
    for entry in panel:
        lines.append(
            f"  panel {entry.get('member')}: "
            + (
                f"{entry.get('reviewer')} {entry.get('model')} {entry.get('effort')}, "
                f"{entry.get('seconds', 0):.0f} s, {len(entry.get('numbers') or [])} findings"
                if entry.get("status") == "finished"
                else f"failed: {entry.get('error')}"
                if entry.get("status") == "failed"
                else f"skipped: {entry.get('reason')}"
            )
            + (f" ({entry.get('reason') or entry.get('fallback')})"
               if entry.get("status") == "finished" and (entry.get("reason") or entry.get("fallback")) else "")
            + (f"; note: {entry['note']}" if entry.get("note") else "")
        )
    for f in findings:
        where = f"{f['file']}:{f['line']}" if f.get("file") and f.get("line") else f.get("file") or "-"
        raw_mark: Any = f.get("mark")
        mark = cast(dict[str, Any], raw_mark) if isinstance(raw_mark, dict) else None
        marked = (
            ""
            if not mark
            else f"  [fixed: {str(mark['merge'])[:10]}]"
            if mark.get("kind") == "fixed"
            else f"  [rejected: {mark.get('reason')}]"
        )
        category = f" [{f['category']}]" if f.get("category") else ""
        by = f" ({f['reviewer']})" if f.get("reviewer") else ""
        lines.append(f"  {f['n']}. {f['severity']}{category} {where} {f['title']}{by}{marked}")
        lines += [f"     {line}" for line in f.get("detail", "").splitlines()[:6]]
    if raw.get("issue"):
        lines.append(f"Ticket: {raw['issue']}")
    elif raw.get("issue_error"):
        lines.append(
            f"Ticket not filed: {raw['issue_error']}; {cmd_prog} review --overall "
            f"{str(raw.get('tip', ''))[:10]} files it again"
        )
    pre_land = rev.pre_land_fix(record)
    if pre_land is not None:
        lines.append(
            f"Pre-land fix round: reviewed commit {pre_land.reviewed_sha[:10]} on "
            f"{pre_land.branch} ({pre_land.worktree})"
        )
    if raw.get("kind") == "lane" and rev.open_findings:
        if pre_land is not None or (rev.is_pre_land and (record is None or lifecycle.active(record))):
            lines.append(
                "Mark each finding: "
                + review_record.mark_help(label, prog=cmd_prog).replace("<merge>", "<fix-commit>|" + label)
            )
        else:
            lines.append(
                "Mark each finding: "
                + review_record.mark_help(label, prog=cmd_prog)
                + "; the fix round runs in the lane's worktree on a follow-up lane: "
                + f"{cmd_prog} review {label} --fix-lane"
            )
    return lines


def review_start_manual(project: config.Config = config.DEFAULT) -> bool:
    """LANEFATHER_REVIEW_START when set, else this machine's `review.start` setting."""
    found = project.env("REVIEW_START")
    return found == "manual" if found else settings.value("review.start") == "manual"


def review_start_source(project: config.Config = config.DEFAULT) -> str:
    """What made land skip the review, for its message."""
    if project.env("REVIEW_START"):
        return f"{project.env_names('REVIEW_START')}=manual"
    return "lanefather settings review.start=manual"


def self_argv(project: config.Config = config.DEFAULT) -> list[str]:
    """This lanefather, run again with the same config."""
    path = project.path
    return [sys.executable, "-m", "lanefather", *(("--config", str(path)) if path else ())]


def self_env(**extra: str) -> dict[str, str]:
    package_parent = str(Path(__file__).resolve().parent.parent)
    python_path = os.environ.get("PYTHONPATH")
    return dict(os.environ, PYTHONPATH=package_parent + (os.pathsep + python_path if python_path else ""), **extra)


def spawn_review(
    store: state.Store, label: str, fields: dict[str, Any], argv: list[str], cwd: str
) -> review_record.ReviewRecord:
    """Start `lanefather review ...` detached, with its own log, and record it as running; nobody waits for it."""
    log = store.log_path(f"review-{label}")
    rev = review_record.ReviewRecord.spawn_and_begin(
        store,
        label,
        fields,
        cmd=[*self_argv(store.project), "review", *argv],
        cwd=cwd,
        env=self_env(LANEFATHER_REVIEW_LOG=str(log)),
        log=log,
        effort=reviewer.Reviewer(store.project).effort,
    )
    raw = rev.raw or {}
    journal.ledger(
        f"review-start {label} pid={raw.get('pid')} log={log}",
        kind="review.started",
        **review_about(label, fields),
        details={"pid": raw.get("pid"), "log": str(log)},
        store=store,
    )
    return rev


def _lane_review_fields(record: Record, **extra: Any) -> dict[str, Any]:
    created = state.lane_created_of(record)
    return {
        "kind": "lane",
        "lane": record["id"],
        "ticket": record["ticket"],
        **({"lane_created": created} if created else {}),
        **extra,
    }


def start_lane_review(store: state.Store, record: Record, *, force_fresh: bool = False) -> str:
    """At land: an exempt receipt, or the review in the background. The line land prints."""
    lane_id = record["id"]
    rev = review_record.ReviewRecord.load(store, lane_id, record=record)
    verdict = rev.verdict
    if force_fresh and rev.exists and not isinstance(verdict, review_record.Exempt):
        review_record.ReviewRecord.clear_stale(store, lane_id, lane_created=state.lane_created_of(record))
        rev = review_record.ReviewRecord.load(store, lane_id, record=record)
        verdict = rev.verdict
    if isinstance(
        verdict,
        (
            review_record.Running,
            review_record.Exempt,
            review_record.PartialPanel,
            review_record.Unmarked,
            review_record.Complete,
        ),
    ):
        raw = rev.raw or {}
        kept_status = (
            "running"
            if isinstance(verdict, review_record.Running)
            else "exempt"
            if isinstance(verdict, review_record.Exempt)
            else "finished"
        )
        # Reviewed before landing (onto dev or main), or by hand: one review per lane, kept as it is.
        journal.ledger(
            f"review {lane_id} kept at land: {kept_status} since {raw.get('started') or raw.get('finished')}",
            kind="review.kept",
            lane=record,
            details={"status": kept_status, "since": raw.get("started") or raw.get("finished")},
            store=store,
        )
        return f"review kept: {rev.summary()[1]}"
    exempt = review_exemption(record, store)
    if exempt:
        raw_landed: Any = record.get("landed")
        landed_info = cast(dict[str, Any], raw_landed) if isinstance(raw_landed, dict) else {}
        eff_cls = landed_info.get("review_class")
        eff_why = landed_info.get("review_class_reason")
        extra_fields: dict[str, Any] = {}
        if isinstance(eff_cls, str) and eff_cls in review_class.CLASS_RANK:
            extra_fields["review_class"] = eff_cls
        if isinstance(eff_why, str) and eff_why:
            extra_fields["review_reason"] = eff_why
        review_record.ReviewRecord.exempt(
            store,
            lane_id,
            exempt,
            fields=_lane_review_fields(record, **extra_fields),
        )
        journal.ledger(
            f"review {lane_id} exempt: {exempt}",
            kind="review.exempted",
            lane=record,
            details={"exempt": exempt},
            store=store,
        )
        return f"review exempt: {exempt}"
    if review_start_manual(store.project):
        return f"review not started ({review_start_source(store.project)}): run {store.prog} review {lane_id}"
    started = spawn_review(
        store,
        lane_id,
        _lane_review_fields(record),
        [lane_id],
        main_checkout(record["common_dir"]),
    )
    started_raw = started.raw or {}
    return f"review started in the background (pid {started_raw.get('pid')}, log {started_raw.get('log')})"


def _audit_review_override(store: state.Store, record: Record | None, force_reason: str | None) -> None:
    journal.override("review", record, force_reason, ["reviewed again"], audited=False, store=store)


def _effective_lane_review_class(record: Record) -> str | None:
    raw_landed: Any = record.get("landed")
    if isinstance(raw_landed, dict):
        cls = cast(dict[str, Any], raw_landed).get("review_class")
        if isinstance(cls, str) and cls in review_class.CLASS_RANK:
            return cls
    return review_class.declared_class(record)


def _read_external_report(
    report_path: str, alias: str, project: config.Config
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    path = Path(report_path)
    if not path.is_file():
        raise LaneError(f"--record {report_path}: file does not exist")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise LaneError(f"--record {report_path}: could not read file ({error})") from None
    findings = reviewer.parse_findings(text)
    resolved = reviewer.resolve_reviewer(alias, project)
    if resolved is not None:
        meta: dict[str, Any] = {
            "reviewer": resolved.alias,
            "model": resolved.id,
            "effort": resolved.effort,
            "provider": resolved.provider,
            "cli": resolved.cli,
            "options": resolved.options,
        }
    else:
        cleaned = alias.strip()
        meta = {
            "reviewer": cleaned,
            "model": cleaned,
            "effort": "external",
            "provider": None,
            "cli": "external",
            "options": None,
        }
    return text, findings, meta


def _parse_lanes_option(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return list(dict.fromkeys(
        name
        for value in cast(list[Any], raw)
        if isinstance(value, str)
        for name in value.replace(",", " ").split()
        if name
    ))


def _record_external_review(
    store: state.Store,
    label: str,
    fields: dict[str, Any],
    *,
    base: str,
    tip: str,
    tree: str,
    report_path: str,
    alias: str,
    force: bool = False,
) -> review_record.ReviewRecord:
    text, findings, meta = _read_external_report(report_path, alias, store.project)
    reviews = store.reviews_dir
    reviews.mkdir(parents=True, exist_ok=True)
    output = reviews / f"{label}.md"
    output.write_text(text, encoding="utf-8")
    finished_at = journal.now()
    payload: dict[str, Any] = {
        **fields,
        **meta,
        "base": base,
        "tip": tip,
        "tree": tree,
        "output": str(output),
        "source_file": str(Path(report_path).resolve()),
        "findings": findings,
        "seconds": 0.0,
        "fallback": None,
        "finished": finished_at,
    }
    completed = review_record.ReviewRecord.record_external(
        store, label, payload, force=force, finished=finished_at,
    )
    raw = completed.raw or {}
    counts = " ".join(f"{s.lower()}={sum(1 for f in findings if f['severity'] == s)}" for s in SEVERITIES)
    journal.ledger(
        f"review {label} finished reviewer={raw.get('reviewer', '-')} "
        f"model={raw.get('model', '-')} effort={raw.get('effort', '-')} "
        f"left=- seconds=0 findings={len(findings)} {counts} (recorded)",
        kind="review.finished",
        level="info",
        **review_about(label, fields),
        details={
            "status": "finished",
            "recorded": True,
            "source_file": payload["source_file"],
            "reviewer": raw.get("reviewer"),
            "provider": raw.get("provider"),
            "cli": raw.get("cli"),
            "model": raw.get("model"),
            "effort": raw.get("effort"),
            "options": raw.get("options"),
            "seconds": 0.0,
            "findings": len(findings),
            "severities": {s.lower(): sum(1 for f in findings if f["severity"] == s) for s in SEVERITIES},
            "fallback": None,
            "error": None,
        },
        store=store,
    )
    return completed


def _report_audit_findings(
    store: state.Store,
    record: Record,
    completed: review_record.ReviewRecord,
    cls: str,
    reason: str,
) -> list[dict[str, Any]]:
    severe = [f for f in completed.findings if f.get("severity") in review_class.AUDIT_SEVERITIES]
    if not severe:
        return []
    lane_id = str(record["id"])
    for finding in severe:
        print(
            f"Audit alert: lane {lane_id} (allowed through by review class {cls!r}: {reason}) "
            f"has {finding['severity']} finding #{finding['n']}: {finding.get('title', '')}",
            file=sys.stderr,
        )
    journal.ledger(
        f"audit-alert {lane_id} class={cls} findings={len(severe)}",
        kind="review.audit_alert",
        level="warn",
        lane=record,
        details={
            "lane": lane_id,
            "review_class": cls,
            "review_reason": reason,
            "findings": [
                {"n": f.get("n"), "severity": f.get("severity"), "title": f.get("title")}
                for f in severe
            ],
        },
        store=store,
    )
    return severe


def audit_lane(args: Any, store: state.Store) -> int:
    """Non-blocking post-hoc audit review of a `none` or `evidence` lane (fathers#252)."""
    record = store.load(args.id)
    if lifecycle.planned(record):
        raise LaneError(f"audit refused: lane {args.id} is planned; it has not landed")
    label = review_class.audit_label(args.id)
    existing = review_record.ReviewRecord.load(store, label, record=record)
    verdict = existing.verdict
    elsewhere = isinstance(verdict, review_record.Running) and verdict.pid != os.getpid()
    already_finished = isinstance(verdict, (review_record.Unmarked, review_record.Complete))
    if (already_finished or elsewhere) and not args.force_reason:
        print("\n".join(review_lines(existing, record=record, prog=store.prog)))
        return 0
    raw_landed: Any = record.get("landed")
    landed_info = cast(dict[str, Any], raw_landed) if isinstance(raw_landed, dict) else {}
    raw_cls = landed_info.get("review_class")
    cls = raw_cls if isinstance(raw_cls, str) and raw_cls in review_class.CLASS_RANK else (
        review_class.declared_class(record) or "none"
    )
    raw_why = landed_info.get("review_class_reason")
    reason = raw_why if isinstance(raw_why, str) and raw_why else (
        review_class.evidence_summary(record) if cls == "evidence" else "no review required"
    )
    tip, base = review_range(record)
    main = main_checkout(record["common_dir"])
    fields: dict[str, Any] = {
        "kind": "audit",
        "lane": args.id,
        "ticket": record["ticket"],
        "review_class": cls,
        "review_reason": reason,
        **({"lane_created": state.lane_created_of(record)} if state.lane_created_of(record) else {}),
    }
    record_file = getattr(args, "record", None)
    if record_file:
        alias = getattr(args, "reviewer", None)
        if not alias:
            raise LaneError("--record requires --reviewer <alias>")
        tree = git(main, "rev-parse", tip + "^{tree}")
        completed = _record_external_review(
            store, label, fields, base=base, tip=tip, tree=tree,
            report_path=record_file, alias=alias, force=bool(args.force_reason),
        )
    else:
        resolved = scope.resolve(record, base, store.project)
        completed = perform_review(
            store, label, fields, main, base, tip, scope.prompt_text(resolved),
            family=implementer_family(store, args.id),
            reviewers=asked_reviewers(args, store.project),
            force=bool(args.force_reason),
        )
    print("\n".join(review_lines(completed, record=record, prog=store.prog)))
    if completed.has_finished_receipt:
        _report_audit_findings(store, record, completed, cls, reason)
        return 0
    return 1


def start_audit_reviews(store: state.Store, cwd: str, exempt_lanes: Sequence[dict[str, Any]]) -> list[str]:
    """Deterministically pick 1-in-N `none`/`evidence` lanes for non-blocking post-hoc audit at promote."""
    every = store.project.review_classes.audit_every
    if every <= 0 or not exempt_lanes:
        return []
    messages: list[str] = []
    for entry in exempt_lanes:
        lane_id = str(entry.get("lane") or "")
        merge_sha = str(entry.get("merge") or "")
        cls = str(entry.get("review_class") or "none")
        reason = str(entry.get("reason") or "")
        if not lane_id or not merge_sha or not review_class.should_audit(lane_id, merge_sha, every):
            continue
        record = state.read_json(store.record_path(lane_id))
        if record is None:
            continue
        label = review_class.audit_label(lane_id)
        existing = review_record.ReviewRecord.load(store, label, record=record)
        if isinstance(
            existing.verdict,
            (review_record.Running, review_record.Unmarked, review_record.Complete, review_record.PartialPanel),
        ):
            continue
        if review_start_manual(store.project):
            messages.append(
                f"audit sample (1 in {every}): lane {lane_id} (class={cls}) not started "
                f"({review_start_source(store.project)}): run {store.prog} review {lane_id} --audit"
            )
            continue
        fields: dict[str, Any] = {
            "kind": "audit",
            "lane": lane_id,
            "ticket": record["ticket"],
            "review_class": cls,
            "review_reason": reason,
            **({"lane_created": state.lane_created_of(record)} if state.lane_created_of(record) else {}),
        }
        started = spawn_review(store, label, fields, [lane_id, "--audit"], cwd)
        started_raw = started.raw or {}
        messages.append(
            f"audit sample (1 in {every}): lane {lane_id} (class={cls}) review started in background "
            f"(pid {started_raw.get('pid')}, log {started_raw.get('log')})"
        )
    return messages


def record_lane_review(args: Any, store: state.Store) -> int:
    """`lanefather review <id> --record <report file> --reviewer <alias>` (fathers#252)."""
    alias = getattr(args, "reviewer", None)
    if not alias:
        raise LaneError("--record requires --reviewer <alias>")
    record = store.load(args.id)
    if lifecycle.planned(record):
        raise LaneError(f"review refused: lane {args.id} is planned; create its worktree and commit changes first")
    existing = review_record.ReviewRecord.load(store, args.id, record=record)
    verdict = existing.verdict
    elsewhere = isinstance(verdict, review_record.Running) and verdict.pid != os.getpid()
    already_finished = isinstance(verdict, (review_record.Unmarked, review_record.Complete))
    incomplete_panel = isinstance(verdict, review_record.PartialPanel)
    has_existing = already_finished or incomplete_panel or isinstance(verdict, review_record.Exempt) or elsewhere
    if has_existing and not args.force_reason:
        print("\n".join(review_lines(existing, record=record, prog=store.prog)))
        if already_finished:
            print(f"Lane {args.id} is already reviewed: one review per lane (--force-reason reviews again, logged)")
        return 1 if incomplete_panel else 0
    is_pre_land = lifecycle.active(record)
    main = main_checkout(record["common_dir"])
    tip, base = review_range(record)
    if is_pre_land and tip == base:
        raise LaneError(f"review refused: lane {args.id} has no commits beyond {record['base']}")
    if already_finished or incomplete_panel:
        _audit_review_override(store, record, args.force_reason)
    tree = git(main, "rev-parse", tip + "^{tree}")
    fields = _lane_review_fields(record, **({"pre_land": True} if is_pre_land else {}))
    completed = _record_external_review(
        store,
        args.id,
        fields,
        base=base,
        tip=tip,
        tree=tree,
        report_path=args.record,
        alias=alias,
        force=bool(args.force_reason),
    )
    if record.get("pre_land_fix"):
        record = store.update(args.id, lambda latest: latest.pop("pre_land_fix", None) is not None)
    print("\n".join(review_lines(completed, record=record, prog=store.prog)))
    if completed.allows_promote:
        done.settle_lane(store, args.id, mode=postmerge.CompletionMode.AUTO_AFTER_REVIEW)
    return 0


def review_lane(args: Any, store: state.Store) -> int:
    record = store.load(args.id)
    existing = review_record.ReviewRecord.load(store, args.id, record=record)
    verdict = existing.verdict
    # The background run land started finds its own pid in the receipt.
    elsewhere = isinstance(verdict, review_record.Running) and verdict.pid != os.getpid()
    already_finished = isinstance(verdict, (review_record.Unmarked, review_record.Complete))
    incomplete_panel = isinstance(verdict, review_record.PartialPanel)
    has_existing = already_finished or incomplete_panel or isinstance(verdict, review_record.Exempt) or elsewhere
    if has_existing and not args.force_reason:
        print("\n".join(review_lines(existing, record=record, prog=store.prog)))
        if incomplete_panel:
            print(f"Lane {args.id}: {existing.summary()[1]}")
            return 1
        if already_finished:
            print(f"Lane {args.id} is already reviewed: one review per lane (--force-reason reviews again, logged)")
        return 0
    if lifecycle.planned(record):
        raise LaneError(f"review refused: lane {args.id} is planned; create its worktree and commit changes first")
    is_pre_land = lifecycle.active(record)
    if is_pre_land:
        worktree = record.get("worktree")
        if not isinstance(worktree, str) or not worktree or not os.path.isdir(worktree):
            raise LaneError(f"review refused: lane {args.id} has no existing worktree ({worktree or 'none'})")
        checked_out = current_branch(worktree)
        if checked_out != record["branch"]:
            raise LaneError(
                f"review refused: {worktree} is on {checked_out or 'detached HEAD'}, not {record['branch']}"
            )
        if dirty(worktree):
            raise LaneError(f"review refused: {worktree} has uncommitted changes; commit them before reviewing")
    exempt = review_exemption(record, store)
    if exempt and not args.force_reason:
        start_lane_review(store, record)
        print(f"Review of {args.id}: exempt ({exempt})")
        return 0
    tip, base = review_range(record)
    if is_pre_land and tip == base:
        raise LaneError(f"review refused: lane {args.id} has no commits beyond {record['base']}")
    if already_finished or incomplete_panel:
        _audit_review_override(store, record, args.force_reason)
    chosen_reviewers = asked_reviewers(args, store.project)
    if not chosen_reviewers and _effective_lane_review_class(record) == "double":
        chosen_reviewers = store.project.review_panel or ("sol", "opus")
    # The accepted scope as the reviewer reads it, once for every panel member; the receipt keeps what was read.
    resolved = scope.resolve(record, base, store.project)
    completed = perform_review(
        store,
        args.id,
        _lane_review_fields(
            record,
            scope=[scope.receipt_entry(entry) for entry in resolved],
            scope_complete=scope.complete(resolved),
            **({"pre_land": True} if is_pre_land else {}),
        ),
        main_checkout(record["common_dir"]),
        base,
        tip,
        scope.prompt_text(resolved),
        family=implementer_family(store, args.id),
        reviewers=chosen_reviewers,
        force=bool(args.force_reason),
    )
    if record.get("pre_land_fix"):
        record = store.update(args.id, lambda latest: latest.pop("pre_land_fix", None) is not None)
    print("\n".join(review_lines(completed, record=record, prog=store.prog)))
    if isinstance(completed.verdict, review_record.PartialPanel):
        print(f"Lane {args.id}: {completed.summary()[1]}")
        return 1
    if not isinstance(completed.verdict, (review_record.Unmarked, review_record.Complete)):
        return 1
    if completed.allows_promote:
        done.settle_lane(store, args.id, mode=postmerge.CompletionMode.AUTO_AFTER_REVIEW)
    return 0


def compare(args: Any, store: state.Store) -> int:
    """`review <id> --compare`: who found what and what held up, one block per reviewer with its marks."""
    record = state.read_json(store.record_path(args.id)) if state.ID.match(args.id) else None
    rev = review_record.ReviewRecord.load(store, args.id, record=record)
    if not rev.has_finished_receipt or rev.raw is None:
        raise LaneError(f"lane {args.id} has no finished review to compare ({rev.summary()[1]})")
    raw = rev.raw
    views = rev.reviewer_views()
    lines = [
        f"Review of {args.id} ({str(raw.get('base', ''))[:10]}...{str(raw.get('tip', ''))[:10]}): "
        f"{len(views)} reviewer{'s' if len(views) != 1 else ''}"
    ]
    width = max(len(str(v.get("reviewer"))) for v in views)
    for view in views:
        counts = review_record.mark_counts(view["findings"])
        lines.append(
            f"  {view.get('reviewer')!s:<{width}}  {view.get('model')} {view.get('effort')}: "
            + (
                f"{counts['findings']} findings (p1 {counts['p1']}, p2 {counts['p2']}, p3 {counts['p3']}), "
                f"fixed {counts['fixed']}, rejected {counts['rejected']}, unmarked {counts['unmarked']}"
                if view.get("status") == "finished"
                else f"{view.get('status')}: {view.get('error') or view.get('reason')}"
            )
        )
    for view in views:
        if view.get("status") != "finished":
            continue
        lines += [
            "",
            f"{view.get('reviewer')} ({view.get('model')} {view.get('effort')}, "
            f"{view.get('seconds') or 0:.0f} s)"
            + (f", for {view['member']}: {view.get('reason') or view.get('fallback')}"
               if view.get("member") != view.get("reviewer") else "")
            + (f"; note: {view['note']}" if view.get("note") else ""),
        ]
        if not view["findings"]:
            lines.append("  no findings")
        for f in view["findings"]:
            where = f"{f['file']}:{f['line']}" if f.get("file") and f.get("line") else f.get("file") or "-"
            raw_mark: Any = f.get("mark")
            mark = cast(dict[str, Any], raw_mark) if isinstance(raw_mark, dict) else {}
            held = (
                f"fixed: {mark['merge'][:10]}"
                if mark.get("kind") == "fixed"
                else f"rejected: {mark['reason']}"
                if mark.get("kind") == "rejected"
                else "unmarked"
            )
            lines.append(f"  #{f['n']} {f['severity']} {where} {f['title']}  [{held}]")
    print("\n".join(lines))
    return 0


def mark_review(args: Any, store: state.Store) -> int:
    record = store.load(args.id)
    result = review_record.ReviewRecord.mark(store, record, args.mark)
    for app in result.applied:
        journal.ledger(
            f"review-mark {args.id} {app.finding_n} "
            + (f"fixed:{(app.merge or '')[:10]}" if app.kind == "fixed" else f"rejected: {app.reason}"),
            kind="finding.marked",
            lane=args.id,
            details={"finding": app.finding_n, "mark": app.kind, "merge": app.merge, "reason": app.reason},
            store=store,
        )
    print("\n".join(review_lines(result.review, record=record, prog=store.prog)))
    complete, words = result.review.summary()
    if complete:
        journal.ledger(
            f"review-marked {args.id} fixed={result.fixed_count} rejected={result.rejected_count}",
            kind="review.marked",
            lane=args.id,
            details={"fixed": result.fixed_count, "rejected": result.rejected_count},
            store=store,
        )
        done.settle_lane(store, args.id, mode=postmerge.CompletionMode.AUTO_AFTER_REVIEW)
    else:
        print(f"Lane {args.id}: {words}")
    return 0


def fix_claims(
    store: state.Store,
    source: review_record.ReviewRecord | dict[str, Any],
    fix_id: str,
    common: str | None = None,
) -> tuple[list[str], list[str]]:
    """The fix lane's claims (#586): the repository files the review's findings name, never the landed lane's
    claims, which hold every path its check widened to and so overlap lanes started since. A named file another
    planned or active lane claims stays out, with a line saying so; check widens the claims from the fix's own
    diff and warns if that touches another lane's claim (#574). Returns the claims and those lines."""
    found: list[str] = []
    held: list[str] = []
    lines: list[str] = []
    others = [r for r in state.occupying(state.of_project(store.lanes(), common)) if r["id"] != fix_id]
    findings = source.findings if isinstance(source, review_record.ReviewRecord) else source.get("findings", [])
    for finding in findings:
        path = posixpath.normpath(finding.get("file") or ".")
        if path in (".", "..") or path.startswith(("/", "../")) or path in found or path in held:
            continue
        holders = [
            (other, claim)
            for other in others
            for claim in other["claims"]
            if claims.claims_overlap(path, claim, project=store.project)
        ]
        if holders:
            held.append(path)
            lines.append(
                f"{path} stays out of the fix lane's claims: "
                + ", ".join(f"lane {o['id']} ({scope.display_ticket(o['ticket'])}) claims {c}" for o, c in holders)
                + f"; {store.prog} check warns if the fix changes it"
            )
        else:
            found.append(path)
    return found, lines


def fix_base(record: Record, cwd: str, *, prog: str = "lanefather") -> str:
    """The branch a fix lane starts from (lanefather#1): the one checked out in the worktree the lane landed into,
    else its registration's base when that is a branch; either must hold the landed merge. A lane registered at a
    literal commit keeps that commit as its own base, which lacks what it landed."""
    raw_landed: Any = record.get("landed")
    landed = cast(dict[str, Any], raw_landed) if isinstance(raw_landed, dict) else {}
    merge: str = landed.get("merge") or ""
    integration: str = landed.get("integration") or ""
    into = current_branch(integration) if integration and os.path.isdir(integration) else ""
    for ref in dict.fromkeys(r for r in (into, record["base"]) if r):
        if (
            merge
            and git_ok(cwd, "show-ref", "--verify", "--quiet", f"refs/heads/{ref}")
            and git_ok(cwd, "merge-base", "--is-ancestor", merge, ref)
        ):
            return ref
    lane_id = record["id"]
    if not integration:
        where = "its landed record names no integration worktree"
        todo = "check out the branch it landed on in an integration worktree and give that branch to the fix lane"
    elif not os.path.isdir(integration):
        where = f"its integration worktree {integration} is gone"
        todo = f"recreate it on the branch the lane landed on (git worktree add {integration} <branch>)"
    else:
        where = (
            f"its integration worktree {integration} is on {into}, which does not hold it"
            if into
            else f"its integration worktree {integration} has no branch checked out"
        )
        todo = f"check out the integration branch the lane landed on in {integration}"
    raise LaneError(
        f"hand-over refused: no branch holds lane {lane_id}'s merge {merge[:10]}: {where}, and its base "
        f"{record['base']} is not such a branch; {todo}, then run {prog} review {lane_id} --fix-lane again"
    )


def pre_land_fix_round(store: state.Store, args: Any, record: Record, worktree: str) -> int:
    """One truthful pre-land fix round on a reviewed active candidate (fathers#197, fathers#216): stores the reviewed
    SHA and finding linkage in `<id>.review.json` without landing the defective original or running a second blind
    review."""
    prog = store.prog
    _updated, fix_round = review_record.ReviewRecord.open_pre_land_fix(store, record, worktree)
    unmarked = list(fix_round.findings)
    reviewed_sha = fix_round.reviewed_sha
    journal.ledger(
        f"review-pre-land-fix {args.id} reviewed_sha={reviewed_sha} worktree={worktree}",
        kind="review.pre_land_fix",
        lane=record,
        details={
            "reviewed_sha": reviewed_sha,
            "branch": record["branch"],
            "worktree": worktree,
            "findings": unmarked,
        },
        store=store,
    )
    numbered = ", ".join(f"#{n}" for n in unmarked)
    print(
        f"Pre-land fix round opened for active lane {args.id} in {worktree} "
        f"(reviewed commit {reviewed_sha[:10]} preserved; findings {numbered})."
    )
    print(
        f"Commit the fix on {record['branch']} on top of {reviewed_sha[:10]} (do not amend away the reviewed "
        f"commit), run {prog} check {args.id}, mark each finding ("
        + review_record.mark_help(args.id, prog=prog).replace("<merge>", "<fix-commit>|" + args.id)
        + f"), then {prog} land {args.id} (its review is kept without a second blind review)."
    )
    return 0


def hand_over(args: Any, store: state.Store) -> int:
    """The fix round: for a landed lane, a follow-up lane <id>-review-fixes takes over the landed lane's worktree,
    where the resumed agent keeps its context (a resumed agent is fenced to its own worktree); its land is the fix
    and gets no review of its own. For an active reviewed candidate (fathers#197), opens one truthful pre-land fix
    round on the same active lane, preserving the reviewed SHA and finding linkage."""
    prog = store.prog
    record = store.load(args.id)
    lane_state = record["status"]
    if not (lifecycle.active(record) or lifecycle.awaiting_done(record)):
        raise LaneError(
            f"hand-over refused: lane {args.id} has unsupported status {lane_state!r} "
            "(must be active, landed or released)"
        )
    worktree = record.get("worktree")
    if not worktree:
        raise LaneError(f"hand-over refused: lane {args.id} ({lane_state}) has no worktree to hand over")
    if not os.path.isdir(worktree):
        raise LaneError(f"hand-over refused: lane {args.id}'s worktree {worktree} does not exist")
    if lifecycle.active(record):
        return pre_land_fix_round(store, args, record, worktree)
    parent_rev = review_record.ReviewRecord.fix_parent(store, args.id, record["common_dir"], "hand-over refused")
    fix_id = f"{args.id}-review-fixes"
    main = main_checkout(record["common_dir"])
    fix_paths, held = fix_claims(store, parent_rev, fix_id, record["common_dir"])
    fix, record = lanes.hand_over_fix_lane(
        store, record, fix_id, fix_paths, fix_base(record, main, prog=prog), args.force_reason
    )
    for line in held:
        print(f"lane: {line}")
    review_record.ReviewRecord.attach_fix_lane(store, args.id, fix_id, record=record)
    journal.ledger(
        f"review-hand-over {args.id} to {fix_id} worktree={fix['worktree']}",
        kind="review.handed_over",
        lane=record,
        details={"to": fix_id, "worktree": fix["worktree"]},
        store=store,
    )
    print(
        f"The fix round runs in {fix['worktree']}: git checkout -B {fix['branch']} {fix['base']}, fix each finding "
        f"that holds, commit, then {prog} check {fix_id} and report. Its land needs no review. Then mark "
        f"{args.id}'s findings: " + review_record.mark_help(args.id, prog=prog).replace("<merge>", fix_id + "|<merge>")
    )
    return 0


def ship_review_label(sha: str) -> str:
    return f"ship-{sha[:12]}"


def start_overall_review(store: state.Store, cwd: str, sha: str, base: str) -> str:
    """After promote --ship: the one overall review of base..sha in the background; it never holds the ship."""
    prog = store.prog
    label = ship_review_label(sha)
    rev = review_record.ReviewRecord.load(store, label)
    verdict = rev.verdict
    if isinstance(verdict, (review_record.Unmarked, review_record.Complete)):
        return (
            f"overall review of {sha[:10]} already ran: "
            + ticket_state(retry_ticket(store, label, common_dir(cwd)))
            + f"; {prog} review --overall {sha[:10]} prints it"
        )
    if isinstance(verdict, review_record.PartialPanel):
        return f"overall review of {sha[:10]} is incomplete: {rev.summary()[1]}"
    if isinstance(verdict, review_record.Running):
        return f"overall review of {sha[:10]} is running (pid {verdict.pid}, log {verdict.log})"
    # A failed or killed review keeps its base: after the push, origin/main is the SHA itself.
    saved_base = str(rev.raw["base"]) if rev.raw and rev.raw.get("base") else ""
    base = saved_base or base
    if git_ok(cwd, "merge-base", "--is-ancestor", sha, base):
        return f"no overall review: {base[:10]} already holds {sha[:10]}"
    argv = ["--overall", sha, "--since", base]
    if review_start_manual(store.project):
        return f"overall review not started ({review_start_source(store.project)}): run {prog} review {' '.join(argv)}"
    started = spawn_review(store, label, {"kind": "overall", "sha": sha, "base": base}, argv, cwd)
    started_raw = started.raw or {}
    return (
        f"overall review of {base[:10]}..{sha[:10]} started in the background (pid {started_raw.get('pid')}, log "
        f"{started_raw.get('log')}); its findings become one ticket"
    )


def file_ticket(store: state.Store, payload: dict[str, Any], common: str) -> None:
    """The overall review's findings as one issue, a checklist, for the queue (owner decision on #570)."""
    findings = payload.get("findings", [])
    if not findings or payload.get("issue"):
        return
    items: list[str] = []
    for f in findings:
        where = (
            f" `{f['file']}:{f['line']}`"
            if f.get("file") and f.get("line")
            else f" `{f['file']}`"
            if f.get("file")
            else ""
        )
        category = f" `{f['category']}`" if f.get("category") else ""
        items.append(f"- [ ] **{f['severity']}**{category}{where} {f['title']}")
        items += [f"  {line}" for line in f.get("detail", "").splitlines() if line.strip()]
    body = (
        f"Found by the overall review before main (`{store.prog} promote --ship`) of "
        f"{payload['base'][:10]}..{payload['tip'][:10]}, by {payload.get('model')} at {payload.get('effort')} "
        f"effort. For {store.project.queue_name}: split what holds into tickets there, and tick each item "
        f"once it is fixed or rejected.\n\n" + "\n".join(items) + "\n"
    )
    issue_url, issue_error = github.issue_create(
        f"Overall review of {payload['tip'][:10]}: {len(findings)} findings",
        body,
        common=common,
        project=store.project,
    )
    if issue_url:
        payload["issue"] = issue_url
        payload.pop("issue_error", None)
    else:
        payload["issue_error"] = issue_error
    journal.ledger(
        f"review-ticket {payload['label']} "
        + (f"filed {payload['issue']}" if payload.get("issue") else f"failed: {payload['issue_error']}"),
        kind="review.ticket_filed",
        level="info" if payload.get("issue") else "warn",
        subject={"type": "review", "id": payload["label"]},
        details={"issue": payload.get("issue"), "error": payload.get("issue_error")},
        store=store,
    )


def retry_ticket(store: state.Store, label: str, common: str) -> review_record.ReviewRecord:
    """File the ticket of a finished overall review whose filing failed, without reviewing again."""
    with store.locked(f"review-{label}"):
        rev = review_record.ReviewRecord.load(store, label)
        if rev.has_finished_receipt and rev.findings and rev.raw is not None and not rev.raw.get("issue"):
            draft = dict(rev.raw)
            file_ticket(store, draft, common)
            return review_record.ReviewRecord.record_ticket(
                store,
                label,
                issue=draft.get("issue"),
                issue_error=draft.get("issue_error"),
            )
        return rev


def ticket_state(rev: review_record.ReviewRecord) -> str:
    raw = rev.raw or {}
    if not rev.findings:
        return "no findings"
    return f"ticket {raw['issue']}" if raw.get("issue") else f"the ticket is not filed ({raw.get('issue_error')})"


def overall_review(args: Any, store: state.Store) -> int:
    prog = store.prog
    panel_required = store.project.review_panel_required
    cwd = os.getcwd()
    common = common_dir(cwd)
    sha = git(cwd, "rev-parse", "--verify", args.overall + "^{commit}")
    label = ship_review_label(sha)
    if args.mark:
        result = review_record.ReviewRecord.mark_label(store, label, common, args.mark)
        for app in result.applied:
            journal.ledger(
                f"review-mark {label} {app.finding_n} "
                + (f"fixed:{(app.merge or '')[:10]}" if app.kind == "fixed" else f"rejected: {app.reason}"),
                kind="finding.marked",
                subject={"type": "review", "id": label},
                details={"finding": app.finding_n, "mark": app.kind, "merge": app.merge, "reason": app.reason},
                store=store,
            )
        print("\n".join(review_lines(result.review, prog=prog)))
        complete, words = result.review.summary()
        if complete:
            journal.ledger(
                f"review-marked {label} fixed={result.fixed_count} rejected={result.rejected_count}",
                kind="review.marked",
                subject={"type": "review", "id": label},
                details={"fixed": result.fixed_count, "rejected": result.rejected_count},
                store=store,
            )
        else:
            print(f"Overall review {label}: {words}")
        return 0
    covered_ids = _parse_lanes_option(getattr(args, "lanes", None))
    covered_records: list[Record] = []
    for lane_id in covered_ids:
        rec = store.load(lane_id)
        if rec.get("common_dir") != common:
            raise LaneError(f"--lanes {lane_id}: lane belongs to another repository")
        raw_landed: Any = rec.get("landed")
        landed_info = cast(dict[str, Any], raw_landed) if isinstance(raw_landed, dict) else {}
        merge_sha = landed_info.get("merge")
        if not isinstance(merge_sha, str) or not merge_sha:
            raise LaneError(f"--lanes {lane_id}: lane is not landed")
        if not git_ok(cwd, "merge-base", "--is-ancestor", merge_sha, sha):
            raise LaneError(f"--lanes {lane_id}: landed merge {merge_sha[:10]} is not an ancestor of {sha[:10]}")
        covered_records.append(rec)
    existing = review_record.ReviewRecord.load(store, label)
    verdict = existing.verdict
    if isinstance(verdict, (review_record.Unmarked, review_record.Complete)) and not args.force_reason:
        if not covered_ids:
            retried = retry_ticket(store, label, common)
            print("\n".join(review_lines(retried, prog=prog)))
            return 1 if (retried.raw or {}).get("issue_error") else 0
        print("\n".join(review_lines(existing, prog=prog)))
        return 0
    if isinstance(verdict, review_record.PartialPanel) and not args.force_reason:
        print("\n".join(review_lines(existing, prog=prog)))
        print(f"Overall review {label}: {existing.summary()[1]}")
        return 1
    if isinstance(verdict, review_record.Running) and verdict.pid != os.getpid() and not args.force_reason:
        print("\n".join(review_lines(existing, prog=prog)))
        return 0
    # A rerun of a failed or killed review keeps its base.
    saved_base = str(existing.raw["base"]) if existing.raw and existing.raw.get("base") else None
    since = args.since or saved_base
    if not since and covered_records:
        prevs = [
            str(cast(dict[str, Any], r["landed"]).get("previous") or "")
            for r in covered_records
            if isinstance(r.get("landed"), dict) and cast(dict[str, Any], r["landed"]).get("previous")
        ]
        if prevs:
            since = prevs[0]
            for prev in prevs[1:]:
                mb = git(cwd, "merge-base", since, prev, check=False)
                if mb:
                    since = mb
    if not since:
        raise LaneError(
            "give --since <base>: the overall review covers <base>..<sha> (promote --ship gives origin/main)"
        )
    base = git(cwd, "merge-base", git(cwd, "rev-parse", "--verify", since + "^{commit}"), sha)
    for rec in covered_records:
        merge_sha = str(cast(dict[str, Any], rec["landed"])["merge"])
        if git_ok(cwd, "merge-base", "--is-ancestor", merge_sha, base):
            raise LaneError(
                f"--lanes {rec['id']}: landed merge {merge_sha[:10]} is already in base {base[:10]}; "
                "give an earlier --since base"
            )
    fields: dict[str, Any] = {
        "kind": "overall",
        "sha": sha,
        **({"covers": covered_ids} if covered_ids else {}),
    }
    record_file = getattr(args, "record", None)
    if record_file:
        alias = getattr(args, "reviewer", None)
        if not alias:
            raise LaneError("--record requires --reviewer <alias>")
        main = main_checkout(common)
        tree = git(main, "rev-parse", sha + "^{tree}")
        completed = _record_external_review(
            store, label, fields, base=base, tip=sha, tree=tree,
            report_path=record_file, alias=alias, force=bool(args.force_reason),
        )
        print("\n".join(review_lines(completed, prog=prog)))
        return 0
    if covered_ids:
        subject = (
            f"Batch review covering lanes {', '.join(covered_ids)}: every change from {base[:10]} to "
            f"{sha[:10]}. Review each covered lane's changes and the seams between them."
        )
    else:
        subject = (
            f"The overall review before main ({prog} promote --ship): every change from {base[:10]} to "
            f"{sha[:10]}, many lanes at once. Look for seams between them as well as bugs in each."
        )

    def _finalize_overall(finished: dict[str, Any]) -> None:
        if not covered_ids and isinstance(
            review_record.derive_verdict(finished, panel_required=panel_required),
            (review_record.Unmarked, review_record.Complete),
        ):
            file_ticket(store, finished, common)

    completed = perform_review(
        store,
        label,
        fields,
        main_checkout(common),
        base,
        sha,
        subject,
        finalize=_finalize_overall,
        reviewers=asked_reviewers(args, store.project),
        force=bool(args.force_reason),
    )
    print("\n".join(review_lines(completed, prog=prog)))
    if isinstance(completed.verdict, review_record.PartialPanel):
        print(f"Overall review {label}: {completed.summary()[1]}")
        return 1
    finished_ok = isinstance(completed.verdict, (review_record.Unmarked, review_record.Complete))
    return 0 if finished_ok and not (completed.raw or {}).get("issue_error") else 1


def review(args: Any, store: state.Store) -> int:
    if args.overall:
        return overall_review(args, store)
    if not args.id:
        raise LaneError("give a lane id, or --overall <sha> --since <base>")
    record_file = getattr(args, "record", None)
    is_audit = bool(getattr(args, "audit", False))
    reviewer_flag = getattr(args, "reviewer", None)
    if record_file and (args.mark or getattr(args, "compare", False) or args.fix_lane):
        raise LaneError("--record does not combine with --mark, --compare or --fix-lane")
    if reviewer_flag and not record_file and (args.mark or getattr(args, "compare", False) or args.fix_lane):
        raise LaneError("--reviewer chooses who reviews; it does not combine with --mark, --compare or --fix-lane")
    if is_audit:
        return audit_lane(args, store)
    if record_file:
        return record_lane_review(args, store)
    if getattr(args, "compare", False):
        return compare(args, store)
    if args.mark:
        return mark_review(args, store)
    if args.fix_lane:
        return hand_over(args, store)
    return review_lane(args, store)


def _is_clean_integration_merge(
    main: str, commit: str, int_head: str, reviewed_tip: str
) -> tuple[bool, str]:
    """Whether `commit` is a two-parent merge of an integration commit (`int_head`) into a descendant of
    `reviewed_tip` whose tree equals the clean merge of its parents (leaving the lane's own diff unchanged).
    Returns `(is_clean, first_parent)`."""
    if not int_head:
        return False, ""
    parents_proc = subprocess.run(
        ["git", "rev-list", "--parents", "-n", "1", commit],
        cwd=main,
        capture_output=True,
        text=True,
    )
    if parents_proc.returncode != 0:
        return False, ""
    parts = parents_proc.stdout.strip().split()
    if len(parts) != 3:
        return False, ""
    _, p1, p2 = parts
    if not git_ok(main, "merge-base", "--is-ancestor", p2, int_head):
        return False, ""
    if not git_ok(main, "merge-base", "--is-ancestor", reviewed_tip, p1):
        return False, ""
    commit_tree = git(main, "rev-parse", "--verify", "--quiet", f"{commit}^{{tree}}", check=False)
    if not commit_tree:
        return False, ""
    merged = subprocess.run(
        ["git", "merge-tree", "--write-tree", p1, p2],
        cwd=main,
        capture_output=True,
        text=True,
    )
    if merged.returncode == 0:
        first_line = merged.stdout.splitlines()[0].strip() if merged.stdout.splitlines() else ""
        return (first_line == commit_tree, p1)
    if merged.returncode == 1:
        # Merge conflict was manually resolved: treat as unreviewed.
        return False, ""
    if merged.returncode != 129:
        return False, ""
    # Fallback only when `git merge-tree --write-tree` is unsupported (git < 2.38 usage error 129).
    mb = git(main, "merge-base", p1, p2, check=False)
    if not mb:
        return False, ""
    lane_before = git(main, "diff", "--no-ext-diff", mb, p1, check=False)
    lane_after = git(main, "diff", "--no-ext-diff", p2, commit, check=False)
    return (lane_before == lane_after, p1)


def pre_land_review_problem(
    store: state.Store,
    record: Record,
    main: str,
    tip: str,
    tree: str,
    integration: str,
    *,
    prog: str = "lanefather",
) -> str | None:
    """Validate a lane's pre-land review before landing (fathers#242); None when no pre-land review blocks land."""
    lane_id = str(record["id"])
    rev = review_record.ReviewRecord.load(store, lane_id, record=record)
    verdict = rev.verdict
    if isinstance(verdict, (review_record.Absent, review_record.Exempt, review_record.Failed, review_record.Died)):
        return None
    if isinstance(verdict, review_record.Running):
        return f"lane {lane_id}'s pre-land review is still running ({rev.summary(prog=prog)[1]})"
    if isinstance(verdict, review_record.PartialPanel):
        return f"lane {lane_id}'s pre-land review is incomplete ({rev.summary(prog=prog)[1]})"
    if isinstance(verdict, review_record.Unmarked):
        blocks = store.project.review_blocks
        blocking = [f for f in verdict.unmarked if f.get("severity", "P2") in blocks]
        if blocking:
            nums = ", ".join(f"#{f['n']}" for f in blocking)
            return (
                f"lane {lane_id}'s pre-land review has unmarked {'/'.join(blocks)} findings ({nums}): "
                f"mark each first ({review_record.mark_help(lane_id, prog=prog)})"
            )
    reviewed_tip = rev.reviewed_tip
    if not reviewed_tip or not review_record.OID_RE.fullmatch(reviewed_tip):
        return (
            f"lane {lane_id}'s pre-land review receipt has no valid reviewed commit SHA; "
            f"rerun {prog} review {lane_id} --force-reason <why>"
        )
    fixed_shas: set[str] = set()
    for finding in rev.findings:
        raw_mark: Any = finding.get("mark")
        if not isinstance(raw_mark, dict):
            continue
        mark = cast(dict[str, Any], raw_mark)
        if mark.get("kind") != "fixed":
            continue
        merge_sha: Any = mark.get("merge")
        if not isinstance(merge_sha, str) or not review_record.OID_RE.fullmatch(merge_sha):
            return (
                f"lane {lane_id}'s pre-land review finding #{finding.get('n')} has an invalid fixed commit SHA; "
                f"re-mark it or rerun {prog} review {lane_id} --force-reason <why>"
            )
        fixed_shas.add(merge_sha)
    for fix_sha in sorted(fixed_shas):
        if not git_ok(main, "merge-base", "--is-ancestor", fix_sha, tip):
            return (
                f"lane {lane_id} tip {tip[:10]} does not contain fix commit {fix_sha[:10]} from pre-land review; "
                f"restore the fix or rerun {prog} review {lane_id} --force-reason <why>"
            )
    if not git_ok(main, "merge-base", "--is-ancestor", reviewed_tip, tip):
        return (
            f"lane {lane_id} tip {tip[:10]} does not contain pre-land reviewed commit {reviewed_tip[:10]}; "
            f"rerun {prog} review {lane_id} --force-reason <why>"
        )
    reviewed_tree = rev.reviewed_tree or git(
        main, "rev-parse", "--verify", "--quiet", f"{reviewed_tip}^{{tree}}", check=False
    )
    if fixed_shas and (tip == reviewed_tip or (reviewed_tree and tree == reviewed_tree)):
        return (
            f"lane {lane_id} tree at {tip[:10]} matches the unfixed pre-land reviewed commit "
            f"{reviewed_tip[:10]} despite findings marked fixed; commit the fix or rerun "
            f"{prog} review {lane_id} --force-reason <why>"
        )
    if not fixed_shas and (tip == reviewed_tip or (reviewed_tree and tree == reviewed_tree)):
        return None
    int_head = git(integration, "rev-parse", "--verify", "--quiet", "HEAD", check=False)
    visited: set[str] = set()
    queue: list[str] = [tip]
    unreviewed: list[str] = []
    non_integration_merges: list[str] = []
    while queue:
        sha = queue.pop(0)
        if sha in visited:
            continue
        visited.add(sha)
        if sha == reviewed_tip or git_ok(main, "merge-base", "--is-ancestor", sha, reviewed_tip):
            continue
        parents_proc = subprocess.run(
            ["git", "rev-list", "--parents", "-n", "1", sha],
            cwd=main,
            capture_output=True,
            text=True,
        )
        if parents_proc.returncode != 0:
            return (
                f"lane {lane_id}: could not inspect commit {sha[:10]} since pre-land review at "
                f"{reviewed_tip[:10]}; rerun {prog} review {lane_id} --force-reason <why>"
            )
        parts = parents_proc.stdout.strip().split()
        if not parts:
            return (
                f"lane {lane_id}: could not inspect commit {sha[:10]} since pre-land review at "
                f"{reviewed_tip[:10]}; rerun {prog} review {lane_id} --force-reason <why>"
            )
        parents = parts[1:]
        if len(parents) > 1:
            is_clean, p1 = _is_clean_integration_merge(main, sha, int_head, reviewed_tip)
            if is_clean:
                queue.append(p1)
            else:
                non_integration_merges.append(sha)
            continue
        if not any(git_ok(main, "merge-base", "--is-ancestor", sha, fix_sha) for fix_sha in fixed_shas):
            unreviewed.append(sha)
        queue.extend(parents)
    if non_integration_merges:
        shown = ", ".join(sha[:10] for sha in non_integration_merges[:5]) + (
            f" and {len(non_integration_merges) - 5} more" if len(non_integration_merges) > 5 else ""
        )
        return (
            f"lane {lane_id} has unreviewed merge commits since pre-land review at {reviewed_tip[:10]} "
            f"({shown}); rerun {prog} review {lane_id} --force-reason <why>"
        )
    if unreviewed:
        shown = ", ".join(sha[:10] for sha in unreviewed[:5]) + (
            f" and {len(unreviewed) - 5} more" if len(unreviewed) > 5 else ""
        )
        return (
            f"lane {lane_id} has unreviewed commits since pre-land review at {reviewed_tip[:10]} "
            f"({shown}); mark them as fixes or rerun {prog} review {lane_id} --force-reason <why>"
        )
    return None


def landing_findings(record: Record, store: state.Store) -> tuple[list[LaneFinding], list[LaneFinding]]:
    """The open findings of other lanes here. A valid parent cannot hold the fix that will resolve its findings."""
    parents = set(valid_fix_parents(record, store))
    blocks = store.project.review_blocks
    other = (r for r in store.lanes() if r.get("common_dir") == record["common_dir"] and r["id"] != record["id"])
    held: list[LaneFinding] = []
    reported: list[LaneFinding] = []
    for lane, findings in review_record.ReviewRecord.unmarked(store, other):
        for finding in findings:
            if finding.get("severity", "P2") in blocks:
                if lane["id"] not in parents:
                    held.append((lane, finding))
            else:
                reported.append((lane, finding))
    return held, reported


def fix_round_commands(
    records: Iterable[Record], alongside: Iterable[str] = (), *, prog: str = "lanefather"
) -> list[str]:
    lines: list[str] = []
    landed: list[Record] = []
    for record in records:
        raw_landed: Any = record.get("landed")
        landing = cast(dict[str, Any], raw_landed) if isinstance(raw_landed, dict) else {}
        if landing.get("merge"):
            landed.append(record)
        else:
            lines.append(
                f"Apply the fix on {record['branch']} and mark {record['id']}'s findings before another land."
            )
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
