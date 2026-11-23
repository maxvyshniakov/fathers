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

gh runs without a prompt against the project's repository. A gh failure warns and goes to the ledger; it never fails
the land or the promote, which have already happened.
"""
from __future__ import annotations

from collections.abc import Sequence
import os
import re
import sys
from typing import Any

from . import config, github, journal, receipts, scope, state
from .gitops import common_dir, git, git_ok
from .state import Record

LABEL = "integrated"
LABEL_COLOR = "0E8A16"
LABEL_DESCRIPTION = "Done and landed in the integration/dev branch; not yet on main or released"
# done's ticket-comment check counts the implementer's evidence, not the comment land posts (lanefather.done).
LAND_MARKER = "<!-- lanefather:land -->"
FIXES = re.compile(r"^[ \t]*fixes[ \t]+#(\d+)[ \t]*$", re.IGNORECASE | re.MULTILINE)
REFS = re.compile(r"^[ \t]*refs[ \t]+#(\d+)[ \t]*$", re.IGNORECASE | re.MULTILINE)
GH_TIMEOUT = 60


def has_ticket(record: Record) -> bool:
    return record["ticket"] != "none"


def no_ticket_warning(record: Record, project: config.Config = config.DEFAULT) -> str | None:
    """The warning start gives a lane without a ticket when the project turns the rules on."""
    if has_ticket(record) or not project.tickets.enabled():
        return None
    return (f"lane: warning: lane {record['id']} has no ticket, but [tickets] in {config.CONFIG_NAME} is on: its "
            "land labels, comments on and closes no issue. Register every lane with --ticket N")


def merge_message(message: str, issues: Sequence[scope.Issue] | str,
                  rule: config.Tickets = config.DEFAULT.tickets) -> str:
    """The landing merge's message, ending, when the project asks for it, with one line per ticket of the lane's
    scope in order: `Fixes #N` for a ticket it completes, `Refs #N` for a partial one. A ticket the message already
    names on a `Fixes` or `Refs` line of its own keeps that line (an explicit `land -m` wins)."""
    if isinstance(issues, str):
        issues = [item for item in scope.from_ticket(issues) if isinstance(item, scope.Issue)]
    if not rule.fixes_trailer:
        return message
    named = {*FIXES.findall(message), *REFS.findall(message)}
    lines = [f"{'Refs' if issue.partial else 'Fixes'} #{issue.number}" for issue in issues if issue.number not in named]
    if not lines:
        return message
    return message.rstrip("\n") + "\n\n" + "\n".join(lines)


def trailer_conflicts(cwd: str, base: str, head: str, issues: Sequence[scope.Issue],
                      message: str = "") -> list[str]:
    """Each `Fixes #N` line in base..head, or in the merge message, for a ticket the lane does only part of: GitHub
    closes the ticket anyway when that commit reaches the default branch."""
    partial = {issue.number for issue in issues if issue.partial}
    if not partial:
        return []
    found: list[str] = []
    log = git(cwd, "log", "--format=%H%x01%B%x00", f"{base}..{head}", check=False)
    for entry in log.split("\0"):
        sha, _, body = entry.strip("\n").partition("\x01")
        found += [f"lane commit {sha[:10]} says Fixes #{n}, which is partial: GitHub closes #{n} when it reaches "
                  "the default branch" for n in dict.fromkeys(FIXES.findall(body)) if n in partial]
    found += [f"the merge message says Fixes #{n}, which is partial: GitHub closes #{n} when it reaches the "
              "default branch" for n in dict.fromkeys(FIXES.findall(message)) if n in partial]
    return found


def warn_trailer_conflicts(store: state.Store, record: Record, cwd: str, base: str, head: str,
                           message: str = "") -> list[str]:
    """Warn, before land merges, about a `Fixes` line that would close a ticket the lane does only part of; the land
    goes on."""
    conflicts = trailer_conflicts(cwd, base, head, scope.issues(record), message)
    for conflict in conflicts:
        print(f"lane: warning: {conflict}", file=sys.stderr)
    if conflicts:
        journal.ledger(f"tickets land {record['id']}: {'; '.join(conflicts)}", kind="ticket.trailer_conflict",
                       level="warn", lane=record, details={"conflicts": conflicts}, store=store)
    return conflicts


def fixed_issues(cwd: str, base: str, head: str) -> list[str]:
    """The issue numbers `Fixes #N` lines name in base..head, oldest commit first, once each."""
    bodies = git(cwd, "log", "--reverse", "--format=%B%x00", f"{base}..{head}")
    return list(dict.fromkeys(FIXES.findall(bodies)))


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
                 rule: config.Tickets = config.DEFAULT.tickets, issue: scope.Issue | None = None) -> str:
    """The comment land posts on one ticket of the lane's scope (the primary ticket when `issue` is None)."""
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
    notes: list[str] = []
    if issue.partial:
        refs = f" (`Refs #{issue.number}`)" if rule.fixes_trailer else ""
        notes.append(f"Partial: this merge does part of #{issue.number}{refs}; the issue stays open and is not "
                     f"labelled `{LABEL}`.")
    else:
        if rule.integrated_label:
            notes.append(f"Labelled `{LABEL}`: done and landed in `{branch}`, not yet on the default branch or "
                         "released.")
        if rule.fixes_trailer:
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
    numbers = ", ".join(f"#{issue.number}" for issue in issues)
    if smoke_status != "green":
        for issue in issues:
            journal.ledger(f"tickets land {record['id']} #{issue.number}: not marked, the post-merge smoke is "
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
    for issue in issues:
        mark_issue(store, record, issue, repo, merge, branch, pushed, receipt, smoke)


def mark_issue(store: state.Store, record: Record, issue: scope.Issue, repo: str, merge: str, branch: str,
               pushed: list[str], receipt: dict[str, Any] | None, smoke: str) -> None:
    """after_land for one ticket: the label when the lane completes it, the land comment, and one ledger event."""
    rule = store.project.tickets
    number, what = issue.number, f"land {record['id']} #{issue.number}"
    problems: list[str] = []
    done: list[str] = []
    if rule.integrated_label and not issue.partial:
        problem = add_label(repo, number, project=store.project)
        if problem:
            problems.append(f"label {LABEL}: {problem}")
        else:
            done.append(f"labelled {LABEL}")
    if rule.land_comment:
        body = land_comment(record, merge, branch, repo, pushed, receipt, smoke, rule=rule, issue=issue)
        problem = gh(repo, "issue", "comment", number, "--body", body, project=store.project)
        if problem:
            problems.append(f"comment: {problem}")
        else:
            done.append("commented")
    if done:
        details: dict[str, Any] = {"issue": number, "done": done, "repository": repo}
        if issue.partial:
            details["partial"] = True
        journal.ledger(f"tickets {what}: {', '.join(done)} ({repo})", kind="ticket.marked", lane=record,
                       details=details, directory=store.directory, project=store.project)
        print(f"lane: #{number} {' and '.join(done)} in {repo}" + (" (partial: not labelled)"
                                                                     if issue.partial and rule.integrated_label
                                                                     else ""))
    if problems:
        warn(what, problems, store=store)


def release_comments(store: state.Store, cwd: str, common: str, base: str, head: str, version: str | None) -> int:
    """Comment the version (or the main SHA) on each issue a `Fixes #N` line in base..head names; the number
    commented. A failure warns."""
    rule = store.project.tickets
    if not rule.release_comment:
        return 0
    if not git_ok(cwd, "merge-base", "--is-ancestor", base, head):
        warn(f"release {head[:10]}", [f"{base[:10]} is not an ancestor of {head[:10]}: no release comments"],
             store=store)
        return 0
    numbers = fixed_issues(cwd, base, head)
    if not numbers:
        return 0
    what = f"release {version or head[:10]}"
    repo = repository(common, rule)
    if not repo:
        warn(what, ["no GitHub repository: origin is not on GitHub and [tickets] names no repo"], store=store)
        return 0
    link = f"[`{head[:10]}`](https://github.com/{repo}/commit/{head})"
    body = (f"Released in `{version}` ({link})." if version else f"Shipped to the default branch as {link}.")
    problems: list[str] = []
    commented = unlabelled = 0
    for number in numbers:
        problem = gh(repo, "issue", "comment", number, "--body", body, project=store.project)
        if problem:
            problems.append(f"#{number}: {problem}")
        else:
            commented += 1
        # Released, the issue is no longer "not yet on main": its integrated label goes.
        problem = remove_label(repo, number, project=store.project)
        if problem:
            problems.append(f"#{number}: remove label {LABEL}: {problem}")
        else:
            unlabelled += 1
    journal.ledger(f"tickets {what}: commented {commented} of {len(numbers)} fixed issues "
                   f"({', '.join('#' + n for n in numbers)}) in {repo}, label {LABEL} removed from {unlabelled}",
                   kind="ticket.release_commented", subject={"type": "commit", "id": head},
                   details={"version": version, "issues": numbers, "commented": commented,
                            "unlabelled": unlabelled, "repository": repo},
                   store=store)
    print(f"lane: {what}: commented on {commented} of {len(numbers)} issues it fixes "
          f"({', '.join('#' + n for n in numbers)})")
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
