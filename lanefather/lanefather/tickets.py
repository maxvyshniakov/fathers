"""The ticket lifecycle (#25): what a lane's issue shows from its land to a release, for a project whose
`[tickets]` in .lanefather.toml turns the rules on. Without that table nothing here runs.

- `fixes_trailer`: the landing merge's message ends with `Fixes #N` for each ticket of the lane's scope it completes
  (lanefather.scope), so GitHub closes each only when the merge reaches the default branch, and `Refs #N` for each
  it does only part of, which stays open.
- `integrated_label`, `land_comment`: after a green post-merge smoke, land labels the issue `integrated` (done and in
  the integration branch, not yet on main or released) each ticket the lane completes, and comments on every ticket
  of its scope the merge, its branch, whether origin has it (a
  local-only SHA does not resolve on GitHub, so it is not linked), the checks it passed and the preview.
- `release_comment`: promote --ship, and `shipped` after a project's release publish, comment the main SHA or the
  version on each issue a `Fixes #N` line in the shipped range names, and take its `integrated` label off.

A ticket of another repository (`owner/repo#N`, fathers#238) gets `Fixes owner/repo#N` or `Refs owner/repo#N`,
and its label and comments go to that repository. gh runs without a prompt against the repository it addresses. A
gh failure warns and goes to the ledger; it never fails the land or the promote, which have already happened.
"""
from __future__ import annotations

from collections.abc import Sequence
import os
import re
import sys
from typing import Any, cast

from . import config, github, journal, receipts, scope, state
from .errors import LaneError
from .gitops import common_dir, git, git_ok
from .state import Record

LABEL = "integrated"
LABEL_COLOR = "0E8A16"
LABEL_DESCRIPTION = "Done and landed in the integration/dev branch; not yet on main or released"
# done's ticket-comment check counts the implementer's evidence, not the comment land posts (lanefather.done).
LAND_MARKER = "<!-- lanefather:land -->"
# A ticket reference as GitHub reads it in a closing keyword: `#N`, or `owner/repo#N` for another repository.
_REF = r"((?:[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+)?#\d+)"
FIXES = re.compile(rf"^[ \t]*fixes[ \t]+{_REF}[ \t]*$", re.IGNORECASE | re.MULTILINE)
REFS = re.compile(rf"^[ \t]*refs[ \t]+{_REF}[ \t]*$", re.IGNORECASE | re.MULTILINE)
GH_TIMEOUT = 60


def has_ticket(record: Record) -> bool:
    return record["ticket"] != "none"


def no_ticket_warning(record: Record, project: config.Config = config.DEFAULT) -> str | None:
    """The warning start gives a lane without a ticket when the project turns the rules on."""
    if has_ticket(record) or not project.tickets.enabled():
        return None
    return (f"lane: warning: lane {record['id']} has no ticket, but [tickets] in {config.CONFIG_NAME} is on: its "
            "land labels, comments on and closes no issue. Register every lane with --ticket N")


def is_umbrella(issue: scope.Issue | str, rule: config.Tickets = config.DEFAULT.tickets,
                default_repo: str = "") -> bool:
    """Whether `issue` is listed in `[tickets] umbrella` (fathers#64)."""
    if not rule.umbrella:
        return False
    repo = default_repo or rule.repo
    key = issue.number if isinstance(issue, scope.Issue) else issue
    target = _norm(key, repo)
    return any(_norm(entry, repo) == target for entry in rule.umbrella)


def norm_key(key: str, default_repo: str = "") -> str:
    """Case-folded ticket identity for cross-repo matching."""
    return _norm(key, default_repo)


def _landed_partial(record: Record) -> tuple[bool, str | None]:
    """`(partial, remaining)` recorded on `record["landed"]` by `land --partial [--remaining TEXT]`."""
    raw: Any = record.get("landed")
    if not isinstance(raw, dict):
        return False, None
    info = cast(dict[str, Any], raw)
    is_partial = info.get("partial") is True
    raw_rem = info.get("remaining")
    remaining = raw_rem.strip() if isinstance(raw_rem, str) and raw_rem.strip() else None
    return is_partial, remaining


def merge_message(message: str, issues: Sequence[scope.Issue] | str,
                  rule: config.Tickets = config.DEFAULT.tickets, *, repo: str = "") -> str:
    """The landing merge's message, ending, when the project asks for it, with one line per ticket of the lane's
    scope in order: `Fixes #N` for a ticket it completes, `Refs #N` for a partial or umbrella one, and
    `Fixes owner/repo#N` or `Refs owner/repo#N` for a ticket of another repository, the form GitHub closes across
    repositories when the merge reaches the default branch (fathers#238, #64). A ticket the message already names
    on a `Fixes` or `Refs` line of its own keeps that line (an explicit `land -m` wins)."""
    if isinstance(issues, str):
        issues = [item for item in scope.from_ticket(issues) if isinstance(item, scope.Issue)]
    if not rule.fixes_trailer:
        return message
    default_repo = repo or rule.repo
    named = {_norm(key, default_repo) for ref in (*FIXES.findall(message), *REFS.findall(message))
             if (key := _key(ref)) is not None}
    lines = [f"{'Refs' if issue.partial or is_umbrella(issue, rule, default_repo) else 'Fixes'} {issue.ref}"
             for issue in issues
             if _norm(issue.number, default_repo) not in named]
    if not lines:
        return message
    return message.rstrip("\n") + "\n\n" + "\n".join(lines)


def _key(ref: str) -> str | None:
    """A `#N` or `owner/repo#N` reference as the record stores the ticket (scope.Issue.number), or None when invalid."""
    try:
        return scope.ticket_number(ref)
    except LaneError:
        return None


def _norm(key: str, default_repo: str = "") -> str:
    """Case-folded identity for deduplication, resolving bare numbers against `default_repo` when known."""
    issue = scope.Issue(key)
    local = issue.local.lstrip("0") or "0" if issue.local.isdigit() else issue.local
    repo = (issue.repo or default_repo).lower()
    return f"{repo}#{local}" if repo else local


def check_repair_partial(merge_sha: str, merge_body: str, issues: Sequence[scope.Issue],
                         rule: config.Tickets, repo: str, partial: bool) -> bool:
    """Derive or validate `--partial` on `land --repair` from an already-committed merge message."""
    if not rule.fixes_trailer:
        return partial
    default_repo = repo or rule.repo
    non_umbrella = [issue for issue in issues if not issue.partial and not is_umbrella(issue, rule, default_repo)]
    if not non_umbrella:
        return partial
    fixed = {_norm(k, default_repo) for ref in FIXES.findall(merge_body) if (k := _key(ref)) is not None}
    reffed = {_norm(k, default_repo) for ref in REFS.findall(merge_body) if (k := _key(ref)) is not None}
    has_fixes = any(_norm(issue.number, default_repo) in fixed for issue in non_umbrella)
    all_reffed = all(_norm(issue.number, default_repo) in reffed for issue in non_umbrella)
    if partial and has_fixes:
        raise LaneError(
            f"land --repair --partial refused: merge {merge_sha[:10]} already says Fixes; rerun without --partial"
        )
    if not partial and not has_fixes and all_reffed:
        return True
    return partial


def trailer_conflicts(cwd: str, base: str, head: str, issues: Sequence[scope.Issue],
                      message: str = "", *, rule: config.Tickets = config.DEFAULT.tickets,
                      repo: str = "") -> list[str]:
    """Each `Fixes #N` line in base..head, or in the merge message, for a partial or umbrella ticket: GitHub
    closes the ticket anyway when that commit reaches the default branch."""
    default_repo = repo or rule.repo
    partial_keys = {_norm(issue.number, default_repo) for issue in issues if issue.partial}
    if not partial_keys and not rule.umbrella:
        return []

    def conflict_reason(key: str) -> str | None:
        if _norm(key, default_repo) in partial_keys:
            return "partial"
        if is_umbrella(key, rule, default_repo):
            return "an umbrella ticket"
        return None

    found: list[str] = []
    log = git(cwd, "log", "--format=%H%x01%B%x00", f"{base}..{head}", check=False)
    for entry in log.split("\0"):
        sha, _, body = entry.strip("\n").partition("\x01")
        for ref in dict.fromkeys(FIXES.findall(body)):
            key = _key(ref)
            if key is not None and (why := conflict_reason(key)) is not None:
                found.append(f"lane commit {sha[:10]} says Fixes {ref}, which is {why}: GitHub closes {ref} "
                             "when it reaches the default branch")
    for ref in dict.fromkeys(FIXES.findall(message)):
        key = _key(ref)
        if key is not None and (why := conflict_reason(key)) is not None:
            found.append(f"the merge message says Fixes {ref}, which is {why}: GitHub closes {ref} "
                         "when it reaches the default branch")
    return found


def warn_trailer_conflicts(store: state.Store, record: Record, cwd: str, base: str, head: str,
                           message: str = "", *, issues: Sequence[scope.Issue] | None = None,
                           repo: str = "") -> list[str]:
    """Warn, before land merges, about a `Fixes` line that would close a partial or umbrella ticket; the land
    goes on."""
    checked = scope.issues(record) if issues is None else issues
    default_repo = repo or repository(record["common_dir"], store.project.tickets) or ""
    conflicts = trailer_conflicts(cwd, base, head, checked, message, rule=store.project.tickets, repo=default_repo)
    for conflict in conflicts:
        print(f"lane: warning: {conflict}", file=sys.stderr)
    if conflicts:
        journal.ledger(f"tickets land {record['id']}: {'; '.join(conflicts)}", kind="ticket.trailer_conflict",
                       level="warn", lane=record, details={"conflicts": conflicts}, store=store)
    return conflicts


def fixed_issues(cwd: str, base: str, head: str, *, repo: str = "") -> list[str]:
    """The tickets `Fixes #N` and `Fixes owner/repo#N` lines name in base..head, oldest commit first, once each, as
    the record stores them (`N`, `owner/repo#N`)."""
    bodies = git(cwd, "log", "--reverse", "--format=%B%x00", f"{base}..{head}")
    seen: set[str] = set()
    result: list[str] = []
    for ref in FIXES.findall(bodies):
        key = _key(ref)
        if key is None:
            continue
        folded = _norm(key, repo)
        if folded not in seen:
            seen.add(folded)
            result.append(key)
    return result


def repository(common: str, rule: config.Tickets | None = None) -> str | None:
    """owner/name for gh: the configured repo, else origin's GitHub repository."""
    return github.repository(common, rule)


def gh(repo: str, *args: str, project: config.Config | None = None) -> str | None:
    """Run gh without a prompt against repo; None when it succeeded, else what went wrong."""
    env = dict(os.environ, GH_PROMPT_DISABLED="1", GH_NO_UPDATE_NOTIFIER="1", NO_COLOR="1")
    return github._run_status(args, project=project, repo=repo, env=env, timeout=GH_TIMEOUT)  # pyright: ignore[reportPrivateUsage]


def _gh_env() -> dict[str, str]:
    return dict(os.environ, GH_PROMPT_DISABLED="1", GH_NO_UPDATE_NOTIFIER="1", NO_COLOR="1")


def add_label(repo: str, number: str, *, project: config.Config | None = None) -> str | None:
    """Label the issue `integrated`, creating the label once when the repository lacks it."""
    env = _gh_env()
    if github.issue_edit(repo, number, "--add-label", LABEL, project=project, env=env, timeout=GH_TIMEOUT) is None:
        return None
    created = github.label_create(repo, LABEL, color=LABEL_COLOR, description=LABEL_DESCRIPTION,
                                  project=project, env=env, timeout=GH_TIMEOUT)
    if created and "already exists" not in created:
        return created
    return github.issue_edit(repo, number, "--add-label", LABEL, project=project, env=env, timeout=GH_TIMEOUT)


def remove_label(repo: str, number: str, *, project: config.Config | None = None) -> str | None:
    """Take the `integrated` label off the issue; a repository without the label has nothing to remove."""
    problem = github.issue_edit(
        repo, number, "--remove-label", LABEL, project=project, env=_gh_env(), timeout=GH_TIMEOUT
    )
    return None if problem and f"'{LABEL}' not found" in problem else problem


def warn(what: str, problems: list[str], *, store: state.Store | None = None) -> None:
    for problem in problems:
        print(f"lane: warning: {what}: {problem}", file=sys.stderr)
    journal.ledger(f"tickets {what} warning: {'; '.join(problems)}".replace("\n", " "), kind="ticket.failed",
                   level="warn", subject={"type": "ticket", "id": what}, details={"problems": problems},
                   directory=store.directory if store else None, project=store.project if store else None)


def on_origin(cwd: str, sha: str) -> list[str]:
    """The origin branches, as last fetched, that have sha."""
    refs = git(cwd, "for-each-ref", "--contains", sha, "--format=%(refname:lstrip=3)", "refs/remotes/origin/",
               check=False)
    return [ref for ref in refs.splitlines() if ref and ref != "HEAD"]


def substitute(template: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        template = template.replace("{" + key + "}", value)
    return template


def receipt_line(receipt: dict[str, Any] | None, record: Record) -> str:
    return receipts.check_receipt_line(receipt, record)


def land_comment(record: Record, merge: str, branch: str, repo: str, pushed: list[str],
                 receipt: dict[str, Any] | None, smoke: str, *,
                 rule: config.Tickets = config.DEFAULT.tickets, issue: scope.Issue | None = None,
                 partial_override: bool = False, remaining: str | None = None,
                 umbrella: bool | None = None) -> str:
    """The comment land posts on one ticket of the lane's scope (the primary ticket when `issue` is None); `repo` is
    the lane's own repository, whose commit the comment links, also on a ticket of another repository."""
    issue = issue or scope.Issue(record["ticket"])
    items = scope.of(record)
    short = merge[:10]
    where = (f"[`{short}`](https://github.com/{repo}/commit/{merge}) (pushed: origin/{', origin/'.join(pushed)})"
             if pushed else f"`{short}` (local only: origin does not have this merge yet)")
    lines = [LAND_MARKER, f"Landed on `{branch}` as {where}.", "",
             f"- Lane: `{record['id']}`, branch `{record['branch']}`",
             f"- Integration branch: `{branch}`" + (", on origin" if branch in pushed else ", local only"),
             f"- Checks passed: {receipt_line(receipt, record)}; post-merge smoke green ({smoke})"]
    if scope.explicit(items):
        lines.append(f"- Lane scope: {scope.describe(items)}")
    if rule.preview:
        lines.append("- Try it: " + substitute(rule.preview, {"sha": merge, "short": short, "branch": branch,
                                                              "ticket": record["ticket"], "lane": record["id"]}))
    is_umbrella_ticket = is_umbrella(issue, rule, repo) if umbrella is None else umbrella
    is_partial = issue.partial or partial_override
    notes: list[str] = []
    if is_umbrella_ticket:
        refs = f" (`Refs {issue.ref}`)" if rule.fixes_trailer else ""
        rem = f"; remaining: {remaining}" if remaining else ""
        notes.append(f"Umbrella ticket: this merge references {issue.ref}{refs}{rem}; the issue stays open and is "
                     f"not labelled `{LABEL}`.")
    elif is_partial:
        refs = f" (`Refs {issue.ref}`)" if rule.fixes_trailer else ""
        rem = f"; remaining: {remaining}" if remaining else ""
        notes.append(f"Partial: this merge does part of {issue.ref}{refs}{rem}; the issue stays open and is not "
                     f"labelled `{LABEL}`.")
    else:
        if rule.integrated_label:
            notes.append(f"Labelled `{LABEL}`: done and landed in `{branch}`, not yet on the default branch or "
                         "released.")
        if rule.fixes_trailer and issue.repo:
            notes.append(f"The merge says `Fixes {issue.ref}`, so GitHub closes this issue when the merge reaches "
                         f"the default branch of {repo}.")
        elif rule.fixes_trailer:
            notes.append(f"The merge says `Fixes #{issue.number}`, so GitHub closes this issue when it reaches the "
                         "default branch.")
    return "\n".join(lines + ([""] + notes if notes else []))


def after_land(store: state.Store, record: Record, merge: str, integration: str, branch: str,
               receipt: dict[str, Any] | None, smoke_status: str, smoke: str) -> None:
    """Label each ticket the lane completes and comment on every ticket of its scope after its land; a partial
    ticket gets the comment and no label. A red smoke leaves them all for the fix's land."""
    rule = store.project.tickets
    issues = scope.issues(record)
    if not issues or not (rule.integrated_label or rule.land_comment):
        return
    numbers = ", ".join(issue.ref for issue in issues)
    if smoke_status != "green":
        for issue in issues:
            journal.ledger(f"tickets land {record['id']} {issue.ref}: not marked, the post-merge smoke is "
                           f"{smoke_status}", kind="ticket.skipped", lane=record,
                           details={"issue": issue.number, "smoke": smoke_status},
                           directory=store.directory, project=store.project)
        print(f"lane: {numbers} {'is' if len(issues) == 1 else 'are'} not marked integrated: the post-merge smoke "
              f"is {smoke_status}")
        return
    repo = repository(record["common_dir"], rule)
    if not repo:
        warn(f"land {record['id']} {numbers}",
             ["no GitHub repository: origin is not on GitHub and [tickets] names no repo"], store=store)
        return
    pushed = on_origin(integration, merge) if rule.land_comment else []
    seen: set[str] = set()
    for issue in issues:
        folded = _norm(issue.number, repo)
        if folded in seen:
            continue
        seen.add(folded)
        mark_issue(store, record, issue, repo, merge, branch, pushed, receipt, smoke)


def mark_issue(store: state.Store, record: Record, issue: scope.Issue, repo: str, merge: str, branch: str,
               pushed: list[str], receipt: dict[str, Any] | None, smoke: str) -> None:
    """after_land for one ticket: the label when the lane completes it, the land comment, and one ledger event. A
    ticket of another repository is labelled and commented there; `repo` stays the lane's own."""
    rule = store.project.tickets
    target = issue.repo or repo
    number, what = issue.local, f"land {record['id']} {issue.ref}"
    land_partial, remaining = _landed_partial(record)
    umbrella = is_umbrella(issue, rule, repo)
    is_partial = issue.partial or land_partial
    problems: list[str] = []
    done: list[str] = []
    if rule.integrated_label and not is_partial and not umbrella:
        problem = add_label(target, number, project=store.project)
        if problem:
            problems.append(f"label {LABEL}: {problem}")
        else:
            done.append(f"labelled {LABEL}")
    if rule.land_comment:
        body = land_comment(record, merge, branch, repo, pushed, receipt, smoke, rule=rule, issue=issue,
                            partial_override=land_partial, remaining=remaining, umbrella=umbrella)
        problem = gh(target, "issue", "comment", number, "--body", body, project=store.project)
        if problem:
            problems.append(f"comment: {problem}")
        else:
            done.append("commented")
    if done:
        details: dict[str, Any] = {"issue": issue.number, "done": done, "repository": target}
        if is_partial:
            details["partial"] = True
        if remaining:
            details["remaining"] = remaining
        if umbrella:
            details["umbrella"] = True
        journal.ledger(f"tickets {what}: {', '.join(done)} ({target})", kind="ticket.marked", lane=record,
                       details=details, directory=store.directory, project=store.project)
        suffix = ""
        if rule.integrated_label:
            if umbrella:
                suffix = " (umbrella: not labelled)"
            elif is_partial:
                suffix = " (partial: not labelled)"
        print(f"lane: {issue.ref} {' and '.join(done)} in {target}{suffix}")
    if problems:
        warn(what, problems, store=store)


def _fixed_issue_commits(cwd: str, base: str, head: str, *, repo: str = "") -> list[tuple[str, str, set[str]]]:
    """Each ticket named by a `Fixes #N` or `Fixes owner/repo#N` line in base..head, oldest commit first, as
    `(key, folded, commit_shas)`."""
    log = git(cwd, "log", "--reverse", "--format=%H%x01%B%x00", f"{base}..{head}")
    by_folded: dict[str, tuple[str, set[str]]] = {}
    for entry in log.split("\0"):
        sha, _, body = entry.strip("\n").partition("\x01")
        if not sha:
            continue
        for ref in FIXES.findall(body):
            key = _key(ref)
            if key is None:
                continue
            folded = _norm(key, repo)
            if folded not in by_folded:
                by_folded[folded] = (key, {sha})
            else:
                by_folded[folded][1].add(sha)
    return [(key, folded, shas) for folded, (key, shas) in by_folded.items()]


def _partial_lane_commits(store: state.Store, cwd: str, common: str, base: str, head: str,
                          default_repo: str, candidate_keys: set[str]) -> dict[str, set[str]]:
    """Map each normalized ticket key in `candidate_keys` to commit SHAs inside `previous..merge` of lanes in
    `base..head` that landed that ticket partially."""
    real_common = os.path.realpath(common)
    by_ticket: dict[str, set[str]] = {}
    for record in store.lanes():
        rec_common = record.get("common_dir")
        if not isinstance(rec_common, str) or not rec_common or os.path.realpath(rec_common) != real_common:
            continue
        raw_landed: Any = record.get("landed")
        if not isinstance(raw_landed, dict):
            continue
        landed_info = cast(dict[str, Any], raw_landed)
        merge_sha = landed_info.get("merge")
        prev_sha = landed_info.get("previous")
        if not isinstance(merge_sha, str) or not merge_sha:
            continue
        land_partial = landed_info.get("partial") is True
        relevant = [
            folded for issue in scope.issues(record)
            if (land_partial or issue.partial) and (folded := _norm(issue.number, default_repo)) in candidate_keys
        ]
        if not relevant:
            continue
        if not git_ok(cwd, "merge-base", "--is-ancestor", merge_sha, head):
            continue
        if git_ok(cwd, "merge-base", "--is-ancestor", merge_sha, base):
            continue
        if isinstance(prev_sha, str) and prev_sha and git_ok(cwd, "rev-parse", "--verify", "--quiet", prev_sha):
            lane_shas = set(git(cwd, "rev-list", f"{prev_sha}..{merge_sha}", check=False).splitlines())
        else:
            lane_shas = {merge_sha}
        for folded in relevant:
            by_ticket.setdefault(folded, set()).update(lane_shas)
    return by_ticket


def release_comments(store: state.Store, cwd: str, common: str, base: str, head: str, version: str | None) -> int:
    """Comment the version (or the main SHA) on each issue a `Fixes #N` or `Fixes owner/repo#N` line in base..head
    names, in its own repository; the number commented. Umbrella tickets and tickets whose `Fixes` commits all lie
    inside partial lanes in the range are skipped. A failure warns."""
    rule = store.project.tickets
    if not rule.release_comment:
        return 0
    if not git_ok(cwd, "merge-base", "--is-ancestor", base, head):
        warn(f"release {head[:10]}", [f"{base[:10]} is not an ancestor of {head[:10]}: no release comments"],
             store=store)
        return 0
    repo = repository(common, rule)
    default_repo = repo or rule.repo
    candidates = [
        (key, folded, shas)
        for key, folded, shas in _fixed_issue_commits(cwd, base, head, repo=default_repo)
        if not is_umbrella(key, rule, default_repo)
    ]
    if not candidates:
        return 0
    partial_commits = _partial_lane_commits(
        store, cwd, common, base, head, default_repo, {folded for _, folded, _ in candidates}
    )
    numbers = [
        key for key, folded, shas in candidates
        if not (folded in partial_commits and shas <= partial_commits[folded])
    ]
    if not numbers:
        return 0
    what = f"release {version or head[:10]}"
    if not repo:
        warn(what, ["no GitHub repository: origin is not on GitHub and [tickets] names no repo"], store=store)
        return 0
    link = f"[`{head[:10]}`](https://github.com/{repo}/commit/{head})"
    body = (f"Released in `{version}` ({link})." if version else f"Shipped to the default branch as {link}.")
    problems: list[str] = []
    commented = unlabelled = 0
    for key in numbers:
        issue = scope.Issue(key)
        target, number = issue.repo or repo, issue.local
        problem = gh(target, "issue", "comment", number, "--body", body, project=store.project)
        if problem:
            problems.append(f"{issue.ref}: {problem}")
        else:
            commented += 1
        # Released, the issue is no longer "not yet on main": its integrated label goes.
        problem = remove_label(target, number, project=store.project)
        if problem:
            problems.append(f"{issue.ref}: remove label {LABEL}: {problem}")
        else:
            unlabelled += 1
    refs = ", ".join(scope.Issue(key).ref for key in numbers)
    journal.ledger(f"tickets {what}: commented {commented} of {len(numbers)} fixed issues ({refs}) in {repo}, "
                   f"label {LABEL} removed from {unlabelled}",
                   kind="ticket.release_commented", subject={"type": "commit", "id": head},
                   details={"version": version, "issues": numbers, "commented": commented,
                            "unlabelled": unlabelled, "repository": repo},
                   store=store)
    print(f"lane: {what}: commented on {commented} of {len(numbers)} issues it fixes ({refs})")
    if problems:
        warn(what, problems, store=store)
    return commented


def shipped(args: Any, store: state.Store) -> int:
    """After a project's release publish: comment the version on each issue its range fixes."""
    cwd = os.getcwd()
    if not store.project.tickets.release_comment:
        print(f"lane: [tickets] release_comment is off in {config.CONFIG_NAME}; nothing to comment")
        return 0
    head = git(cwd, "rev-parse", "--verify", args.head + "^{commit}")
    base = git(cwd, "rev-parse", "--verify", args.since + "^{commit}")
    release_comments(store, cwd, common_dir(cwd), base, head, args.version)
    return 0
