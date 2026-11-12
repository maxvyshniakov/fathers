"""The ticket lifecycle (#25): what a lane's issue shows from its land to a release, for a project whose
`[tickets]` in .lanefather.toml turns the rules on. Without that table nothing here runs.

- `fixes_trailer`: the landing merge's message ends with `Fixes #N`, so GitHub closes the issue only when the merge
  reaches the default branch.
- `integrated_label`, `land_comment`: after a green post-merge smoke, land labels the issue `integrated` (done and in
  the integration branch, not yet on main or released) and comments the merge, its branch, whether origin has it (a
  local-only SHA does not resolve on GitHub, so it is not linked), the checks it passed and the preview.
- `release_comment`: promote --ship, and `released` after a project's release publish, comment the main SHA or the
  version on each issue a `Fixes #N` line in the shipped range names, and take its `integrated` label off.

gh runs without a prompt against the project's repository. A gh failure warns and goes to the ledger; it never fails
the land or the promote, which have already happened.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import Any

from . import config, journal, review
from .gitops import common_dir, git, git_ok
from .state import Record

LABEL = "integrated"
LABEL_COLOR = "0E8A16"
LABEL_DESCRIPTION = "Done and landed in the integration/dev branch; not yet on main or released"
# done's ticket-comment check counts the implementer's evidence, not the comment land posts (lanefather.done).
LAND_MARKER = "<!-- lanefather:land -->"
FIXES = re.compile(r"^[ \t]*fixes[ \t]+#(\d+)[ \t]*$", re.IGNORECASE | re.MULTILINE)
GH_TIMEOUT = 60


def rules() -> config.Tickets:
    return config.current().tickets


def has_ticket(record: Record) -> bool:
    return record["ticket"] != "none"


def no_ticket_warning(record: Record) -> str | None:
    """The warning start gives a lane without a ticket when the project turns the rules on."""
    if has_ticket(record) or not rules().enabled():
        return None
    return (f"lane: warning: lane {record['id']} has no ticket, but [tickets] in {config.CONFIG_NAME} is on: its "
            "land labels, comments on and closes no issue. Register every lane with --ticket N")


def merge_message(message: str, ticket: str) -> str:
    """The landing merge's message, ending with `Fixes #N` when the project asks for it and it has none."""
    if not rules().fixes_trailer or ticket == "none" or ticket in FIXES.findall(message):
        return message
    return message.rstrip("\n") + f"\n\nFixes #{ticket}"


def fixed_issues(cwd: str, base: str, head: str) -> list[str]:
    """The issue numbers `Fixes #N` lines name in base..head, oldest commit first, once each."""
    bodies = git(cwd, "log", "--reverse", "--format=%B%x00", f"{base}..{head}")
    return list(dict.fromkeys(FIXES.findall(bodies)))


def repository(common: str) -> str | None:
    """owner/name for gh: the configured repo, else origin's GitHub repository."""
    if rules().repo:
        return rules().repo
    found = review.repo_args(common)
    return found[1] if found else None


def gh(repo: str, *args: str) -> str | None:
    """Run gh without a prompt against repo; None when it succeeded, else what went wrong."""
    env = dict(os.environ, GH_PROMPT_DISABLED="1", GH_NO_UPDATE_NOTIFIER="1", NO_COLOR="1")
    argv = [review.gh_command(), *args, "--repo", repo]
    try:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, env=env,
                                timeout=GH_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"gh {' '.join(args[:2])}: {error}"
    if result.returncode:
        return f"gh {' '.join(args[:2])} exited {result.returncode}: {(result.stderr or result.stdout).strip()}"
    return None


def add_label(repo: str, number: str) -> str | None:
    """Label the issue `integrated`, creating the label once when the repository lacks it."""
    if gh(repo, "issue", "edit", number, "--add-label", LABEL) is None:
        return None
    created = gh(repo, "label", "create", LABEL, "--color", LABEL_COLOR, "--description", LABEL_DESCRIPTION)
    if created and "already exists" not in created:
        return created
    return gh(repo, "issue", "edit", number, "--add-label", LABEL)


def remove_label(repo: str, number: str) -> str | None:
    """Take the `integrated` label off the issue; a repository without the label has nothing to remove."""
    problem = gh(repo, "issue", "edit", number, "--remove-label", LABEL)
    return None if problem and f"'{LABEL}' not found" in problem else problem


def warn(what: str, problems: list[str]) -> None:
    for problem in problems:
        print(f"lane: warning: {what}: {problem}", file=sys.stderr)
    journal.ledger(f"tickets {what} warning: {'; '.join(problems)}".replace("\n", " "), kind="ticket.failed",
                   level="warn", subject={"type": "ticket", "id": what}, details={"problems": problems})


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
    if not receipt:
        return "lane receipt: none"
    suites = ", ".join(r["suite"] for r in receipt.get("results", []) if r.get("status") == "passed") or "none"
    count = len(receipt.get("check_results") or {})
    line = (f"lane receipt {receipt.get('status', '?')} on tree `{str(receipt.get('tree', ''))[:10]}`: suites "
            f"{suites}" + (f" ({count} checks)" if count else ""))
    forced = [o["reason"] for o in record.get("overrides", []) if o.get("refusals")
              and any("receipt" in refusal for refusal in o["refusals"])]
    return line + (f"; landed with --force-reason: {forced[-1]}" if forced else "")


def land_comment(record: Record, merge: str, branch: str, repo: str, pushed: list[str],
                 receipt: dict[str, Any] | None, smoke: str) -> str:
    rule = rules()
    short = merge[:10]
    where = (f"[`{short}`](https://github.com/{repo}/commit/{merge}) (pushed: origin/{', origin/'.join(pushed)})"
             if pushed else f"`{short}` (local only: origin does not have this merge yet)")
    lines = [LAND_MARKER, f"Landed on `{branch}` as {where}.", "",
             f"- Lane: `{record['id']}`, branch `{record['branch']}`",
             f"- Integration branch: `{branch}`" + (", on origin" if branch in pushed else ", local only"),
             f"- Checks passed: {receipt_line(receipt, record)}; post-merge smoke green ({smoke})"]
    if rule.preview:
        lines.append("- Try it: " + substitute(rule.preview, {"sha": merge, "short": short, "branch": branch,
                                                              "ticket": record["ticket"], "lane": record["id"]}))
    notes: list[str] = []
    if rule.integrated_label:
        notes.append(f"Labelled `{LABEL}`: done and landed in `{branch}`, not yet on the default branch or released.")
    if rule.fixes_trailer:
        notes.append(f"The merge says `Fixes #{record['ticket']}`, so GitHub closes this issue when it reaches the "
                     "default branch.")
    return "\n".join(lines + ([""] + notes if notes else []))


def after_land(record: Record, merge: str, integration: str, branch: str, receipt: dict[str, Any] | None,
               smoke_status: str, smoke: str) -> None:
    """Label and comment the lane's issue after its land; a red smoke leaves it for the fix's land."""
    rule = rules()
    if not has_ticket(record) or not (rule.integrated_label or rule.land_comment):
        return
    number, what = record["ticket"], f"land {record['id']} #{record['ticket']}"
    if smoke_status != "green":
        journal.ledger(f"tickets {what}: not marked, the post-merge smoke is {smoke_status}",
                       kind="ticket.skipped", lane=record, details={"issue": number, "smoke": smoke_status})
        print(f"lane: #{number} is not marked integrated: the post-merge smoke is {smoke_status}")
        return
    repo = repository(record["common_dir"])
    if not repo:
        warn(what, ["no GitHub repository: origin is not on GitHub and [tickets] names no repo"])
        return
    problems: list[str] = []
    done: list[str] = []
    if rule.integrated_label:
        problem = add_label(repo, number)
        if problem:
            problems.append(f"label {LABEL}: {problem}")
        else:
            done.append(f"labelled {LABEL}")
    if rule.land_comment:
        body = land_comment(record, merge, branch, repo, on_origin(integration, merge), receipt, smoke)
        problem = gh(repo, "issue", "comment", number, "--body", body)
        if problem:
            problems.append(f"comment: {problem}")
        else:
            done.append("commented")
    if done:
        journal.ledger(f"tickets {what}: {', '.join(done)} ({repo})", kind="ticket.marked", lane=record,
                       details={"issue": number, "done": done, "repository": repo})
        print(f"lane: #{number} {' and '.join(done)} in {repo}")
    if problems:
        warn(what, problems)


def release_comments(cwd: str, common: str, base: str, head: str, version: str | None) -> int:
    """Comment the version (or the main SHA) on each issue a `Fixes #N` line in base..head names; the number
    commented. A failure warns."""
    if not rules().release_comment:
        return 0
    if not git_ok(cwd, "merge-base", "--is-ancestor", base, head):
        warn(f"release {head[:10]}", [f"{base[:10]} is not an ancestor of {head[:10]}: no release comments"])
        return 0
    numbers = fixed_issues(cwd, base, head)
    if not numbers:
        return 0
    what = f"release {version or head[:10]}"
    repo = repository(common)
    if not repo:
        warn(what, ["no GitHub repository: origin is not on GitHub and [tickets] names no repo"])
        return 0
    link = f"[`{head[:10]}`](https://github.com/{repo}/commit/{head})"
    body = (f"Released in `{version}` ({link})." if version else f"Shipped to the default branch as {link}.")
    problems: list[str] = []
    commented = unlabelled = 0
    for number in numbers:
        problem = gh(repo, "issue", "comment", number, "--body", body)
        if problem:
            problems.append(f"#{number}: {problem}")
        else:
            commented += 1
        # Released, the issue is no longer "not yet on main": its integrated label goes.
        problem = remove_label(repo, number)
        if problem:
            problems.append(f"#{number}: remove label {LABEL}: {problem}")
        else:
            unlabelled += 1
    journal.ledger(f"tickets {what}: commented {commented} of {len(numbers)} fixed issues "
                   f"({', '.join('#' + n for n in numbers)}) in {repo}, label {LABEL} removed from {unlabelled}",
                   kind="ticket.release_commented", subject={"type": "commit", "id": head},
                   details={"version": version, "issues": numbers, "commented": commented,
                            "unlabelled": unlabelled, "repository": repo})
    print(f"lane: {what}: commented on {commented} of {len(numbers)} issues it fixes "
          f"({', '.join('#' + n for n in numbers)})")
    if problems:
        warn(what, problems)
    return commented


def released(args: Any) -> int:
    """After a project's release publish: comment the version on each issue its range fixes."""
    cwd = os.getcwd()
    if not rules().release_comment:
        print(f"lane: [tickets] release_comment is off in {config.CONFIG_NAME}; nothing to comment")
        return 0
    head = git(cwd, "rev-parse", "--verify", args.head + "^{commit}")
    base = git(cwd, "rev-parse", "--verify", args.since + "^{commit}")
    release_comments(cwd, common_dir(cwd), base, head, args.version)
    return 0
