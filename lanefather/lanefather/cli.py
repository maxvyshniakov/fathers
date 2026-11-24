"""The lanefather command line: one table of commands (COMMANDS) gives the one-screen top-level help, each command's
usage and example for its own --help, and the two lines a misused command prints (fathers#175, docs/cli.md).

--config PATH (or LANEFATHER_CONFIG) names the project's .lanefather.toml; by default it is read from the top of the
repository the command runs in. State: one JSON file per lane in the configured state directory (LANEFATHER_STATE
replaces it), receipts beside it, and every action and override in ledger.log. Refusals name the rule;
`--force-reason` overrides a cap or a missing receipt and is logged.
"""
from __future__ import annotations

import argparse
from collections.abc import Sequence
import os
from pathlib import Path
import re
import sys
import textwrap
from typing import Any, NoReturn

from fathers_common.cli import UsageParser

from . import (
    __version__,
    admission,
    changelog,
    checks,
    config,
    done,
    events,
    gate,
    gc,
    history,
    land,
    lanes,
    lifecycle,
    postmerge,
    promote,
    receipts,
    registry,
    results,
    review,
    review_gate,
    runtime,
    settings,
    show,
    state,
    status,
    tickets,
    wait,
)
from .errors import LaneError, LaneUsageError
from .gitops import current_branch, git
from .host import CHECK_MAX_JOBS


def version() -> str:
    """The version, and the commit install.py recorded beside the package (none in a checkout)."""
    try:
        built = (Path(__file__).with_name("COMMIT")).read_text().strip()
    except OSError:
        built = "from a checkout"
    return f"lanefather {__version__} ({built})"


class Command:
    """One row of the command table: the one-line summary the top-level help lists, the usage after the command's
    name, and one example of it."""

    def __init__(self, summary: str, usage: str, example: str):
        self.summary, self.usage, self.example = summary, usage, example


COMMANDS: dict[str, Command] = {
    "start": Command("register a lane, or activate a planned lane with --create",
                     "<id> --ticket N --claims GLOB... [--ticket N|--partial N...] [--decision URL...] "
                     "[--spec PATH...] [--review none|evidence|full|double] [--gate NAME] "
                     "[--base REF] [--after ID...] [--fix-of ID,ID] [--create] | <id> --create",
                     "fix-login --ticket 12 --claims 'app/**' --create"),
    "next": Command("the first ready queue entry; --start registers it", "[--start] [--base REF] [--create]",
                    "--start --create"),
    "adopt": Command("register the worktree an agent created", "<id> <worktree> <branch>",
                     'fix-login "$PWD" lane/fix-login'),
    "scope": Command("a lane's accepted scope: tickets, decisions, specs",
                     "<id> [--add-ticket N] [--add-partial N] [--add-decision URL] [--add-spec PATH] "
                     "[--mark-partial N] [--mark-complete N] [--json]",
                     "fix-login --add-partial 64"),
    "check": Command("run the lane's checks; the receipt is the evidence", "<id> [--rerun] [--slots N]",
                     "fix-login"),
    "wait": Command("block until a lane is registered, landed or done",
                    "<id> --for registered|landed|done [--timeout S] [--json]",
                    "fix-login --for landed --timeout 3600"),
    "land": Command("merge a green lane, smoke it, start its review",
                    "<id> [--into WORKTREE] [--keep] [--partial [--remaining TEXT]] [--slots N] [-m MESSAGE]",
                    "fix-login --into .claude/worktrees/integrate"),
    "review": Command("a lane's review, its marks and its fix lane",
                      "<id> [--mark N fixed:<merge>|rejected:<reason>] [--fix-lane] [--compare] "
                      "[--record FILE --reviewer ALIAS] [--reviewer ALIAS|both] [--audit] | "
                      "--overall <sha> [--since <base>] [--lanes ID,ID] [--record FILE --reviewer ALIAS]",
                      "fix-login --mark 1 fixed:fix-login-review-fixes"),
    "findings": Command("whether a review holds a SHA, for a publish", "<sha> [--since REF] [--json]",
                        "HEAD --json"),
    "done": Command("clean up a landed lane: worktree, branches, scratch", "<id> | --finished [--dry-run]",
                    "fix-login"),
    "drop": Command("remove a lane that adds nothing to its base", "<id> [<id> ...]", "fix-login"),
    "park": Command("park a lane on park/<id>; frees cap, claims, worktree",
                    "<id> --reason REASON", "fix-login --reason 'waiting on API'"),
    "unpark": Command("resume a parked lane from park/<id>",
                      "<id> [--base REF] [--agent KIND] [--force-reason REASON]", "fix-login"),
    "abandon": Command("abandon an unlanded lane and remove its worktree and claims",
                       "<id> --reason REASON [--keep-branch REF] [--force-reason REASON]",
                       "fix-login --reason 'superseded'"),
    "recheck": Command("rerun a landed lane's red post-merge smoke", "<id> [--slots N]", "fix-login"),
    "smoke": Command("post-merge smoke status, rerun or accept",
                     "<id> [--rerun [--slots N] | --accept REASON]", "fix-login --rerun"),
    "keep": Command("keep a lane from auto-done; --off releases it", "<id> [--off]", "fix-login"),
    "list": Command("this repository's non-done lanes: claims, receipts, load",
                    "[--all] [--all-projects] [--json]", "--json"),
    "show": Command("one lane's details, read-only",
                    "<id> [--json] [--state-dir DIR] [--project-dir DIR]", "fix-login --json"),
    "status": Command("every registered project's lanes, read-only", "[--json]", "--json"),
    "gc": Command("remove merged idle worktrees and unused branches", "[--dry-run] [--json]", "--dry-run"),
    "promote": Command("the batch gate on a SHA, then move dev (and main)",
                       "<sha> [--ship] [--no-push] [--jobs N] [--install [--deploy HOST]]", "1a2b3c4 --no-push"),
    "sync": Command("fast-forward local dev and main to the remote", "[--drop-local] [--remote NAME]", ""),
    "perf": Command("the perf suite on a SHA, alone on the machine", "<sha> [--remote NAME]", "1a2b3c4"),
    "shipped": Command("comment a published version on the issues it fixes",
                       "<version> --since REF [--head REF]", "v1.4.0 --since v1.3.0"),
    "results": Command("check results by tree and plan",
                       "key|lookup|record <rev> (--full | --checks A,B) [--status S] [--json]",
                       "lookup HEAD --full"),
    "stats": Command("lane flow, outcomes, overrides, review and check health", "[--days N] [--lane ID] [--json]",
                     "--days 3 --json"),
    "settings": Command("this machine's settings: show, check, set, reset",
                        "[check|set|reset KEY [VALUE]] [--all] [--revision R] [--json]",
                        "set host.min_disk_gb 150"),
    "changelog": Command("the installed release's changes, Unreleased first", "[--json] [--since VERSION]",
                         "--since 0.1.0"),
    "capabilities": Command("versioned capabilities and installed identity", "[--json]", "--json"),
    "project-hook": Command("run one configured project policy", "<name> [-- ARGS...]", "readiness -- --json"),
}

WIDTH = 78


class Parser(UsageParser):
    """An argparse parser whose usage errors follow docs/cli.md: `<tool>: <reason>` and one more line; for a command,
    that line gives its usage, one example and the pointer to its --help, so an agent recovers without a help turn
    (fathers#175)."""

    def __init__(self, *args: Any, row: Command | None = None, tool: str = "lanefather", **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.row, self.tool = row, tool
        self.children: dict[str, Parser] = {}
        # An option that is required unless one of these options is given (start's --ticket, or --partial).
        self.required_unless: list[tuple[argparse.Action, tuple[str, ...]]] = []

    def usage_error(self, message: str) -> NoReturn:
        unknown = re.match(r"argument <command>: invalid choice: '?([^' ]+)'?", message)
        if unknown:
            message = f"unknown command '{unknown.group(1)}'"
        if self.row is None:
            pointer = f"Try '{self.tool} --help'."
        else:
            example = f" Example: {self.prog} {self.row.example}".rstrip() + "." if self.row.example else ""
            pointer = f"Usage: {self.prog} {self.row.usage}.{example} Try '{self.prog} --help'."
        self.exit(2, f"{self.tool}: {message}\n{pointer}\n")

    def option_name(self, name: str) -> str:
        return repr(name)


def add(root: Parser, commands: Any, name: str, details: str = "", *, prog: str = "lanefather") -> Parser:
    """A command's parser from its row: its usage line, its summary and details, and its example."""
    row = COMMANDS[name]
    text = row.summary[0].upper() + row.summary[1:] + "." + (" " + details if details else "")
    child: Parser = commands.add_parser(name, help=row.summary, row=row, tool=prog, prog=f"{prog} {name}",
                                        parent=root,
                                        usage=f"%(prog)s {row.usage}", description=textwrap.fill(text, WIDTH),
                                        epilog=f"example: {prog} {name} {row.example}".rstrip(),
                                        formatter_class=argparse.RawDescriptionHelpFormatter)
    root.children[name] = child
    return child


class ScopeIssue(argparse.Action):
    """--ticket and --partial (and scope's --add-ticket and --add-partial) append (number, partial) to one list, so
    the lane's scope keeps the order they were given in."""

    def __init__(self, option_strings: Sequence[str], dest: str, *, partial: bool = False, **kwargs: Any):
        super().__init__(option_strings, dest, **kwargs)
        self.partial = partial

    def __call__(self, parser: argparse.ArgumentParser, namespace: argparse.Namespace, values: Any,
                 option_string: str | None = None) -> None:
        given: list[tuple[str, bool]] = list(getattr(namespace, self.dest, None) or [])
        given.append((str(values), self.partial))
        setattr(namespace, self.dest, given)


def agent_kind(value: str) -> str:
    cleaned = value.strip().lower()
    if not events.AGENT_KIND_RE.fullmatch(cleaned):
        raise argparse.ArgumentTypeError(
            f"invalid agent kind {value!r}: use lowercase letters, digits, '.', '_' or '-'")
    return cleaned


def parser(project: config.Config = config.DEFAULT) -> Parser:
    prog = project.command
    blocks = project.review_blocks
    root = Parser(prog=prog, tool=prog, usage="%(prog)s [--config PATH] <command> ...",
                  description="Parallel changes in one repository: lanes registered, checked, landed, reviewed\n"
                              "and cleaned up.",
                  epilog=f"'{prog} <command> --help' gives a command's options and an example.\nRefusals name the "
                         "rule; --force-reason overrides one, logged.",
                  formatter_class=argparse.RawDescriptionHelpFormatter)
    root.add_argument("--config", metavar="PATH", help="the project's .lanefather.toml (or LANEFATHER_CONFIG)")
    root.add_argument("--version", action="version", version=version())
    commands = root.add_subparsers(dest="command", required=True, metavar="<command>", title="commands")
    p = add(root, commands, "start", "Prints the brief header for the lane's prompt and the lane's next commands. "
            "A fix lane named with --fix-of lands without a review of its own (fathers#174).", prog=prog)
    p.add_argument("id")
    ticket = p.add_argument("--ticket", dest="issues", action=ScopeIssue, required=True, metavar="N",
                            help="an issue the lane completes, its merge saying Fixes #N (repeatable; the first is "
                                 "the primary ticket), or none")
    p.required_unless.append((ticket, ("--partial", "--create")))
    p.add_argument("--partial", dest="issues", action=ScopeIssue, partial=True, metavar="N",
                   help="an issue the lane does only part of, its merge saying Refs #N (repeatable, in order with "
                        "--ticket)")
    p.add_argument("--decision", action="append", metavar="URL",
                   help="an owner or coordinator decision the lane answers to: the URL of one issue comment "
                        "(repeatable)")
    p.add_argument("--spec", action="append", metavar="PATH",
                   help="a spec file the lane answers to, relative to the repository (repeatable)")
    claims_arg = p.add_argument("--claims", nargs="+", action="extend", required=True, metavar="GLOB")
    p.required_unless.append((claims_arg, ("--create",)))
    p.add_argument("--base", help="integration branch or commit (default: this worktree's branch)")
    p.add_argument("--branch", help="lane branch (default: lane/<id>)")
    p.add_argument("--after", nargs="+", action="extend", default=[], metavar="ID",
                   help="land after these lanes; their claims may overlap (repeatable)")
    p.add_argument("--scratch", nargs="+", default=[], metavar="DIR", help="extra scratch dirs done removes")
    p.add_argument("--create", action="store_true",
                   help="create the worktree now instead of adopting one (or activate an existing planned lane)")
    p.add_argument("--fix-of", action="append", metavar="ID,ID",
                   help="a fix lane for the review findings of these landed lanes: its land is exempt from review")
    p.add_argument("--review", choices=("none", "evidence", "full", "double"),
                   help="review risk class checked at land (none, evidence, full, double)")
    p.add_argument("--review-decision", metavar="TEXT",
                   help="with --review evidence: owner decision quote and date (or use --decision URL)")
    p.add_argument("--gate", metavar="NAME",
                   help="with --review evidence: check or test name that guards the change")
    p.add_argument("--review-reason", metavar="TEXT",
                   help="optional reason for review class none or evidence, listed at promote")
    p.add_argument("--agent", type=agent_kind,
                   help="the agent starting the lane (e.g. claude-code, codex, antigravity, t3-code)")
    p.add_argument("--force-reason", help="start despite the lane cap or disk floor; logged")
    p.set_defaults(run=lanes.start)
    p = add(root, commands, "next", "Prints the first ready entry of the ranked queue as a brief header.", prog=prog)
    p.add_argument("--start", action="store_true", help="register the lane, as start would")
    p.add_argument("--base", help="integration branch (default: the queue's base, else this worktree's branch)")
    p.add_argument("--create", action="store_true", help="with --start: create the worktree now")
    p.add_argument("--force-reason", help="with --start: start despite the lane cap or disk floor; logged")
    p.set_defaults(run=lanes.next_lane)
    p = add(root, commands, "adopt", "Prints the lane's next commands.", prog=prog)
    p.add_argument("id")
    p.add_argument("worktree")
    p.add_argument("branch")
    p.add_argument("--agent", type=agent_kind,
                   help="the agent adopting the worktree (e.g. claude-code, codex, antigravity, t3-code)")
    p.set_defaults(run=lanes.adopt)
    p = add(root, commands, "scope", "Prints the lane's tickets, decisions and specs in order, or changes a planned "
            "or active lane's: a ticket it completes lands as Fixes #N, a partial one as Refs #N; the first "
            "ticket it completes is the primary one (lanefather.scope).", prog=prog)
    p.add_argument("id")
    p.add_argument("--add-ticket", dest="add_issues", action=ScopeIssue, metavar="N",
                   help="add an issue the lane completes (repeatable)")
    p.add_argument("--add-partial", dest="add_issues", action=ScopeIssue, partial=True, metavar="N",
                   help="add an issue the lane does only part of (repeatable)")
    p.add_argument("--add-decision", action="append", metavar="URL",
                   help="add an owner or coordinator decision: the URL of one issue comment (repeatable)")
    p.add_argument("--add-spec", action="append", metavar="PATH",
                   help="add a spec file, relative to the repository (repeatable)")
    p.add_argument("--mark-partial", action="append", metavar="N", help="mark one of its tickets partial")
    p.add_argument("--mark-complete", action="append", metavar="N", help="mark one of its tickets complete")
    p.add_argument("--add-gate", metavar="NAME",
                   help="set or update the guarding check/test name for --review evidence")
    p.add_argument("--add-review-decision", metavar="TEXT",
                   help="set or update the inline owner decision quote+date for --review evidence")
    p.add_argument("--json", action="store_true", help="the version-1 scope: lane, ticket and scope")
    p.set_defaults(run=lanes.scope_command)
    p = add(root, commands, "check", "In the lane's worktree, on a clean committed tree: the checks the tree's plan "
            "selects for the lane's diff, and the receipt for the tree (lanefather.checks).", prog=prog)
    p.add_argument("id")
    p.add_argument("--rerun", action="store_true", help="run every suite even if a receipt covers the tree")
    p.add_argument("--agent", type=agent_kind,
                   help="the agent that built the lane, e.g. claude-code, codex, antigravity, t3-code "
                        "(default: the agent session this check runs in)")
    p.add_argument("--slots", type=int, choices=range(1, 5),
                   help="verification worker budget passed as verify --jobs (default: slotfather advise, at most "
                        f"{CHECK_MAX_JOBS})")
    p.set_defaults(run=checks.check)
    p = add(root, commands, "wait", "Instead of a loop over list: sleeps with a growing pause and reads the lane's "
            "record. Exits 0 once the lane is in the state, 75 with the current state when the timeout passes "
            f"first (default {wait.DEFAULT_TIMEOUT:g} s; 0 answers at once), 3 when it cannot come (a dropped lane "
            "never lands). A lane done before the wait began is not taken for a new registration.", prog=prog)
    p.add_argument("id")
    p.add_argument("--for", dest="target", required=True, choices=wait.TARGETS)
    p.add_argument("--timeout", type=seconds, default=wait.DEFAULT_TIMEOUT, metavar="S",
                   help=f"give up after S seconds (default {wait.DEFAULT_TIMEOUT:g})")
    p.add_argument("--json", action="store_true", help="the version-1 reply: lane, for, status, reached, waited")
    p.set_defaults(run=wait.command)
    land_review = (
        f"Earlier finished reviews with unmarked {'/'.join(blocks)} findings hold the merge (fathers#177)."
        if blocks
        else "Unmarked findings from earlier finished reviews are reported without holding the merge (fathers#177)."
    )
    p = add(root, commands, "land", "After a current green receipt: merge, start the review and run the post-merge "
            f"smoke. {land_review}", prog=prog)
    p.add_argument("id")
    p.add_argument("--into", help="integration worktree (default: the worktree on the lane's base branch)")
    p.add_argument("-m", "--message")
    p.add_argument("--slots", type=int, choices=range(1, 5),
                   help="worker budget passed as verify --jobs for the post-merge smoke "
                        "(default: slotfather advise --max 3)")
    p.add_argument("--keep", action="store_true",
                   help="leave the landed lane for done (another lane needs its artifacts)")
    p.add_argument("--partial", action="store_true",
                   help="treat this land as partial for every ticket of the lane: emit Refs #N and skip the "
                        "integrated label")
    p.add_argument("--remaining", metavar="TEXT",
                   help="with --partial: note what work remains open on the ticket in the land comment")
    p.add_argument("--repair", action="store_true",
                   help="finish landing an active lane whose branch is already merged into the integration worktree")
    p.add_argument("--force-reason", help="override a missing green receipt or a review hold, with a logged reason")
    p.add_argument("--wait", type=float, default=120, metavar="SECONDS", help="wait this long for the landing lock")
    p.set_defaults(run=land.land)
    p = add(root, commands, "review", "The lane's one independent review (Sol, Opus, or Neon when another family "
            "or quota calls for it; or the [review] panel), its marks and its fix lane; --overall for promote "
            "--ship (lanefather.review).", prog=prog)
    p.add_argument("id", nargs="?")
    p.add_argument("--mark", nargs=2, action="append", metavar=("N", "fixed:COMMIT|rejected:REASON"),
                   help="mark finding N fixed by a commit/merge (or lane id) or rejected with a reason")
    p.add_argument("--fix-lane", action="store_true",
                   help="register <id>-review-fixes on a landed lane's worktree (or open a pre-land fix round)")
    p.add_argument("--reviewer", metavar="ALIAS|both",
                   help="run this reviewer (a model alias, provider/CLI name or model id of the models file, such as "
                        "sol, opus, neon, antigravity, google, agy or gemini-4-neon) whatever the Codex limit, or "
                        "both: the configured panel, else sol and opus")
    p.add_argument("--compare", action="store_true",
                   help="each reviewer's findings side by side with their marks (fixed or rejected)")
    p.add_argument("--record", metavar="FILE",
                   help="record a review report written outside lanefather (requires --reviewer)")
    p.add_argument("--audit", action="store_true",
                   help="run or record a non-blocking post-hoc audit review of a none/evidence lane")
    p.add_argument("--overall", metavar="SHA",
                   help="the overall review of --since..SHA; its findings become one ticket")
    p.add_argument("--since", metavar="BASE", help="with --overall: the base, origin/main for promote --ship")
    p.add_argument("--lanes", action="append", metavar="ID,ID",
                   help="with --overall: landed lane ids covered by this batch review (satisfies each in review gate)")
    p.add_argument("--force-reason", help="review again although the lane has a review, or register the fix lane "
                                          "over the lane cap; logged")
    p.set_defaults(run=review.review)
    p = add(root, commands, "findings", "Unfinished reviews and unmarked blocking findings, as promote applies them "
            "(fathers#109).", prog=prog)
    p.add_argument("sha")
    p.add_argument("--since", metavar="REF", help="lanes landed after REF (default: local dev)")
    p.add_argument("--json", action="store_true", help="the version-1 report")
    p.add_argument("--force-reason", help="answer 0 although a review holds the SHA; logged")
    p.set_defaults(run=review_gate.findings)
    p = add(root, commands, "done", "After landing: stops what the lane left running, removes its worktree, branches, "
            "build directories and scratch. A red post-merge smoke that a later green gate or smoke on the "
            "integration branch cleared does not refuse. --finished: done on every finished lane of the registered "
            "projects.", prog=prog)
    p.add_argument("id", nargs="?")
    p.add_argument("--finished", action="store_true",
                   help="every finished lane of the registered projects: landed, smoke green, review finished or "
                        "exempt with every finding marked, not kept")
    p.add_argument("--dry-run", action="store_true", help="with --finished: list the lanes and sizes, remove nothing")
    p.add_argument("--force-reason", help="clean up a lane that is not landed or lacks a ticket comment; logged")
    p.set_defaults(run=done.done)
    p = add(root, commands, "drop", "A planned or active lane with no commits beyond its base, or commits that change "
            "nothing: done's cleanup, with no override; refuses otherwise. Several ids drop each in turn.", prog=prog)
    p.add_argument("id", nargs="+")
    p.set_defaults(run=done.drop)
    p = add(root, commands, "park", "Saves the lane's branch tip on park/<id>, removes its worktree and lane branch, "
            "keeps its receipts, and marks it done with outcome parked (does not satisfy --after).", prog=prog)
    p.add_argument("id")
    p.add_argument("--reason", required=True, help="why the lane is parked")
    p.set_defaults(run=_run_park)
    p = add(root, commands, "unpark", "Re-registers a parked lane from park/<id>, preserving its scope, claims and "
            "base, and creates a fresh worktree.", prog=prog)
    p.add_argument("id")
    p.add_argument("--base", help="integration branch or commit (default: the parked lane's base)")
    p.add_argument("--agent", type=agent_kind,
                   help="the agent unparking the lane (e.g. claude-code, codex, antigravity, t3-code)")
    p.add_argument("--force-reason", help="unpark despite the lane cap, disk floor, or abandoned --after; logged")
    p.set_defaults(run=lanes.unpark)
    p = add(root, commands, "abandon", "Marks a planned or active lane done with outcome abandoned and removes its "
            "worktree, branch, scratch and claims. Refuses when the branch has commits not merged anywhere unless "
            "--keep-branch or --force-reason is given.", prog=prog)
    p.add_argument("id")
    p.add_argument("--reason", required=True, help="why the lane is abandoned")
    p.add_argument("--keep-branch", metavar="REF",
                   help="save the lane's branch tip at REF (e.g. park/<id>) before removing it")
    p.add_argument("--force-reason", help="discard unmerged commits or uncommitted changes; logged")
    p.set_defaults(run=_run_abandon)
    p = add(root, commands, "recheck", "On the integration tip that holds its merge; green replaces the red result "
            "(lanefather.land).", prog=prog)
    p.add_argument("id")
    p.add_argument("--slots", type=int, choices=range(1, 5),
                   help="worker budget passed as verify --jobs (default: slotfather advise --max 3)")
    p.set_defaults(run=land.recheck)
    p = add(root, commands, "smoke", "Shows the lane's post-merge smoke status, reruns a red, killed or missing "
            "post-merge smoke (--rerun), or accepts a red, killed or missing smoke with an audited reason "
            "(--accept REASON).", prog=prog)
    p.add_argument("id")
    p.add_argument("--rerun", action="store_true",
                   help="rerun a red, killed or missing post-merge smoke")
    p.add_argument("--accept", metavar="REASON",
                   help="accept a red, killed or missing post-merge smoke with an audited reason")
    p.add_argument("--slots", type=int, choices=range(1, 5),
                   help="with --rerun: worker budget passed as verify --jobs (default: slotfather advise --max 3)")
    p.set_defaults(run=_run_smoke)
    p = add(root, commands, "keep", "Auto-done, done --finished and start's disk repair leave a kept lane (its scratch "
            "is in use).", prog=prog)
    p.add_argument("id")
    p.add_argument("--off", action="store_true", help="release the lane to auto-done again")
    p.set_defaults(run=done.keep)
    p = add(root, commands, "list", prog=prog)
    p.add_argument("--all", action="store_true", help="include done lanes")
    p.add_argument("--all-projects", action="store_true",
                   help="every repository's lanes in the Store, each naming its project (default: this repository's)")
    p.add_argument("--json", action="store_true", help="the lane records with their worktree and receipt state, the "
                                                        "cap and the host, as JSON")
    p.set_defaults(run=lanes.list_lanes)
    p = add(root, commands, "show", "For people and dashboards.", prog=prog)
    p.add_argument("id")
    p.add_argument("--json", action="store_true", help="version-1 lane summary")
    p.add_argument("--state-dir", metavar="DIR", help="explicit lane-state directory, without creating it")
    p.add_argument("--project-dir", metavar="DIR", help="resolve project config and verify lane repository identity")
    p.set_defaults(run=show.show)
    p = add(root, commands, "status", "Every registered project's lanes on this machine, for dashboards "
            "(lanefather.status).", prog=prog)
    p.add_argument("--json", action="store_true", help="the version-1 snapshot")
    p.set_defaults(run=status.status)
    p = add(root, commands, "gc", prog=prog)
    p.add_argument("--dry-run", action="store_true", help="report removals without changing anything")
    p.add_argument("--json", action="store_true", help="machine-readable removals, retained paths and errors")
    p.set_defaults(run=gc.collect)
    p = add(root, commands, "promote", "Fast-forwards local dev (and main) to the SHA and pushes it "
            "(lanefather.promote).", prog=prog)
    p.add_argument("sha")
    p.add_argument("--ship", action="store_true", help="also main: pushes <sha>:dev and <sha>:main together")
    p.add_argument("--no-push", action="store_true",
                   help="move the local branches only, and only to a SHA the remote already has (#666)")
    p.add_argument("--jobs", type=int, choices=range(1, 9), metavar="{1..8}",
                   help="worker budget passed as verify --jobs for the batch gate "
                        "(default: slotfather advise --max 2, sized after any load wait)")
    p.add_argument("--remote", default="origin")
    p.add_argument("--wait", type=float, default=120, metavar="SECONDS", help="wait this long for the landing lock")
    p.add_argument("--install", action="store_true",
                   help="then run the project's install steps from the gate worktree")
    p.add_argument("--deploy", metavar="HOST", help="with --install: then run the project's deploy steps for HOST")
    p.add_argument("--force-reason", help="move dev although a lane landed since dev lacks a finished, fully marked "
                                          "review; logged")
    p.set_defaults(run=promote.promote)
    p = add(root, commands, "sync", "Fetches, then moves them under the landing lock; --drop-local resets them to the "
            "remote, keeping a backup branch.", prog=prog)
    p.add_argument("--drop-local", action="store_true",
                   help="reset local dev and main to the remote; the commits it lacks go to backup/<branch>-<date>")
    p.add_argument("--remote", default="origin")
    p.add_argument("--wait", type=float, default=120, metavar="SECONDS", help="wait this long for the landing lock")
    p.set_defaults(run=promote.sync)
    p = add(root, commands, "perf", "The perf suite (#579) in the gate worktree; moves nothing.", prog=prog)
    p.add_argument("sha")
    p.add_argument("--remote", default="origin", help="whose main a red run bisects from (default: origin)")
    p.set_defaults(run=gate.perf)
    p = add(root, commands, "shipped", "After a release publish: comments the version on each issue a Fixes #N line in "
            "REF..HEAD names (lanefather.tickets, [tickets] release_comment).", prog=prog)
    p.add_argument("version", help="the published version, for example v1.4.0")
    p.add_argument("--since", required=True, metavar="REF", help="the previous release (tag or commit)")
    p.add_argument("--head", default="HEAD", metavar="REF", help="the released commit (default: HEAD)")
    p.set_defaults(run=tickets.shipped)
    p = add(root, commands, "results", "The key, whether a plan is verified on a tree, or a result produced outside "
            "Lanefather (lanefather.results, docs/results.md).", prog=prog)
    p.add_argument("action", choices=("key", "lookup", "record"))
    p.add_argument("rev", help="a commit, branch or tree")
    p.add_argument("--full", action="store_true", help="the full gate")
    p.add_argument("--checks", metavar="A,B", help="check ids; for record, A=passed,B=failed")
    p.add_argument("--status", help="with record: green or red for --full, passed or failed for --checks")
    p.add_argument("--source", default="manual", help="with record: who produced the result (external:SOURCE)")
    p.add_argument("--receipt", help="with record: the report or run that holds the result")
    p.add_argument("--platform", help="the platform the result is for, <os>-<arch> such as linux-x64 (default: this "
                                      "host's); a result stands only for its own platform")
    p.add_argument("--json", action="store_true")
    p.set_defaults(run=results.command)
    p = add(root, commands, "stats", "Lane flow, outcomes, overrides, review and per-check p50/p90 from lane records, "
            "receipts, events and run history (lanefather.history).", prog=prog)
    p.add_argument("--days", type=float, default=7, help="the window in days (default 7; 0: everything kept)")
    p.add_argument("--lane", help="scope all stats blocks to this lane")
    p.add_argument("--json", action="store_true", help="the version-1 report")
    p.set_defaults(run=history.command)
    p = add(root, commands, "settings", "Review start, rerun wait, slot priorities, free-disk floor "
            "(lanefather.settings).", prog=prog)
    p.add_argument("action", nargs="?", choices=("check", "set", "reset"))
    p.add_argument("key", nargs="?")
    p.add_argument("value", nargs="?")
    p.add_argument("--all", action="store_true", help="with reset: every setting")
    p.add_argument("--revision", help="with set or reset: refuse when the settings changed since this revision")
    p.add_argument("--json", action="store_true")
    p.set_defaults(run=settings.settings)
    p = add(root, commands, "changelog", "Godfather's what's new (docs/api.md).", prog=prog)
    p.add_argument("--json", action="store_true")
    p.add_argument("--since", metavar="VERSION", help="only releases newer than VERSION, and Unreleased")
    p.set_defaults(run=changelog.command)
    p = add(root, commands, "capabilities", "And the verified installed source identity, for project launchers.",
            prog=prog)
    p.add_argument("--json", action="store_true")
    p.set_defaults(run=runtime.capabilities)
    p = add(root, commands, "project-hook", "In the installed runtime ([hooks.project]).", prog=prog)
    p.add_argument("name")
    p.add_argument("arguments", nargs=argparse.REMAINDER)
    p.set_defaults(run=runtime.project_hook)
    return root


def seconds(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        number = -1.0
    if not number >= 0:
        raise argparse.ArgumentTypeError(f"takes a number of seconds, not {value!r}")
    return number


def config_option(argv: list[str]) -> str | None:
    """--config PATH or --config=PATH before the command, read before the parser so --help names the project's
    command."""
    for index, arg in enumerate(argv):
        if arg == "--config" and index + 1 < len(argv):
            return argv[index + 1]
        if arg.startswith("--config="):
            return arg.split("=", 1)[1]
        if not arg.startswith("-"):
            return None
    return None


def resolve_cli_config(argv: list[str], cwd: str) -> tuple[config.Config | None, LaneError | None]:
    """Load explicit `--config` immediately; otherwise try `config.find(cwd)` and defer any error for commands that
    can run without a project config (`changelog`, `status`, `capabilities`, `settings`, `show --state-dir`)."""
    given = config_option(argv)
    if given is not None:
        return config.load(Path(given)), None
    try:
        return config.load(config.find(cwd)), None
    except LaneError as error:
        return None, error


def parse(argv: list[str], project: config.Config = config.DEFAULT) -> argparse.Namespace:
    """The arguments, or a usage error (exit 2) that names the word it refuses, before any missing argument."""
    root = parser(project)
    for child in root.children.values():
        for action, alternatives in child.required_unless:
            if any(arg == option or arg.startswith(option + "=") for arg in argv for option in alternatives):
                action.required = False
    args, extra = root.parse_known_args(argv)
    if extra:
        chosen = command_parser(root, args.command)
        if chosen.rejects_option(extra[0]):
            chosen.unknown_option(extra[0])
        chosen.usage_error(f"unrecognized arguments: {' '.join(extra)}")
    return args


def command_parser(root: Parser, name: str) -> Parser:
    return root.children.get(name, root)


def _rerun_unrecorded_smoke(store: state.Store, record: state.Record, slots: int | None) -> int:
    """Rerun the initial post-merge smoke for a landed lane whose smoke was killed or never recorded."""
    lane_id = str(record["id"])
    landed_info = postmerge.landed(record)
    raw_merge = landed_info.get("merge")
    merge_sha = raw_merge if isinstance(raw_merge, str) and raw_merge else ""
    if not merge_sha:
        raise LaneError(f"smoke --rerun refused: lane {lane_id} has no recorded merge commit")
    integration = postmerge.integration_of(record)
    if integration is None:
        raise LaneError(f"smoke --rerun refused: lane {lane_id}'s integration worktree is gone")
    raw_prev = landed_info.get("previous")
    previous = raw_prev if isinstance(raw_prev, str) and raw_prev else git(
        integration, "rev-parse", "--verify", "--quiet", f"{merge_sha}^1", check=False,
    )
    if not previous:
        raise LaneError(f"smoke --rerun refused: cannot resolve parent commit of merge {merge_sha[:10]}")
    landed_obj = land.Landed(
        record=record,
        merge=merge_sha,
        previous=previous,
        integration=integration,
        onto=current_branch(integration),
        keep=lifecycle.kept(record),
        receipt=receipts.read(store, lane_id, record=record),
    )
    smoke_result = land.post_merge(store, landed_obj, slots)
    return land.after_land(store, landed_obj, smoke_result)


def _run_park(args: Any, store: state.Store) -> int:
    """`lanefather park <id> --reason REASON`."""
    done.park_lane(args.id, args.reason, store)
    return 0


def _run_abandon(args: Any, store: state.Store) -> int:
    """`lanefather abandon <id> --reason REASON [--keep-branch REF] [--force-reason REASON]`."""
    done.abandon_lane(args.id, args.reason, store, keep_branch=args.keep_branch, force_reason=args.force_reason)
    return 0


def _run_smoke(args: Any, store: state.Store) -> int:
    """`lanefather smoke <id> [--rerun [--slots N] | --accept REASON]`."""
    if args.rerun and args.accept is not None:
        raise LaneUsageError("pass either --rerun or --accept, not both")
    if args.slots is not None and not args.rerun:
        raise LaneUsageError("--slots goes with --rerun")
    if args.accept is not None:
        pm = postmerge.PostMergeRecord.accept_smoke(store, args.id, reason=args.accept)
        saved_reason = postmerge.post_merge(pm.record).get("reason")
        shown_reason = saved_reason if isinstance(saved_reason, str) else args.accept
        print(f"Lane {args.id}: post-merge smoke accepted ({shown_reason}); run {store.prog} done {args.id} to finish")
        return 0
    if args.rerun:
        record = store.load(args.id)
        if not lifecycle.awaiting_done(record):
            raise LaneError(f"smoke --rerun refused: lane {args.id} is {record.get('status')}, not landed")
        phase, live_status = postmerge.live_phase(record)
        if phase is postmerge.PostMergePhase.RUNNING:
            raise LaneError(f"smoke --rerun refused: lane {args.id}'s post-merge smoke is already running")
        if phase in postmerge.SMOKE_OK_PHASES:
            shown = "green" if phase is postmerge.PostMergePhase.CLEARED else (live_status or "green")
            raise LaneError(f"smoke --rerun refused: lane {args.id}'s post-merge smoke is already {shown}")
        if postmerge.red_receipt(record, store) is not None:
            return land.recheck(args, store)
        return _rerun_unrecorded_smoke(store, record, args.slots)
    record = store.load(args.id)
    pm = postmerge.PostMergeRecord.load(store, record)
    shown = pm.raw_status or "not run"
    post = postmerge.post_merge(record)
    extra = ""
    if pm.phase is postmerge.PostMergePhase.RUNNING and isinstance(post.get("pid"), int):
        extra = f" (pid {post['pid']})"
    elif pm.phase is postmerge.PostMergePhase.ACCEPTED and isinstance(post.get("reason"), str):
        extra = f" ({post['reason']})"
    elif pm.phase is postmerge.PostMergePhase.CLEARED and pm.cleared_by:
        extra = f" (cleared by {postmerge.words(pm.cleared_by)})"
    print(f"Lane {args.id}: post-merge {shown}{extra}")
    return 0


# The commands that act on one registered lane (fathers#236): from another repository's checkout they refuse, as
# they would act from the wrong checkout. show is read-only and names the lane's project, so it reads any lane.
LANE_COMMANDS = (
    "check", "wait", "land", "review", "done", "drop", "park", "unpark", "abandon", "recheck", "smoke", "keep",
    "scope",
)


def lane_ids(args: Any) -> list[str]:
    value = getattr(args, "id", None)
    return [] if value is None else [value] if isinstance(value, str) else list(value)


def refuse_foreign(store: state.Store, command: str, lane_id: str) -> None:
    """Refuse a lane of another repository than the one the command runs in, naming where it runs."""
    admission.refuse_foreign(store, command, lane_id)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    loaded_cfg: config.Config | None = None
    chosen_command: str | None = None
    try:
        loaded_cfg, cfg_err = resolve_cli_config(argv, os.getcwd())
        args = parse(argv, loaded_cfg or config.DEFAULT)
        chosen_command = args.command
        if args.command in ("changelog", "status"):
            return args.run(args)
        if args.command in ("capabilities", "settings"):
            return args.run(args, loaded_cfg)
        if args.command == "show":
            return show.show(args, loaded_cfg, cfg_err)
        if cfg_err is not None:
            raise cfg_err
        assert loaded_cfg is not None
        if args.command == "project-hook":
            return runtime.project_hook(args, loaded_cfg)
        if args.command not in registry.READ_ONLY:
            registry.note(loaded_cfg)
        store = state.Store(loaded_cfg)
        if args.command in LANE_COMMANDS and args.command != "drop":
            for lane_id in lane_ids(args):
                refuse_foreign(store, args.command, lane_id)
        return args.run(args, store)
    except LaneUsageError as error:
        target = command_parser(parser(loaded_cfg or config.DEFAULT), chosen_command or "")
        target.usage_error(str(error))
    except LaneError as error:
        print(f"lane: {error}", file=sys.stderr)
        return error.code
