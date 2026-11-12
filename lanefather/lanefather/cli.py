"""The lanefather command line.

  start <id> --ticket N --claims GLOB... [--base REF] [--after ID...] [--create]
  next [--start] [--base REF]        the first ready entry of the ranked queue: its brief header;
                                     --start registers it as start would
  adopt <id> <worktree> <branch>     register the worktree an Agent created
  check <id>                         in the lane: the checks the tree's plan selects for the lane's diff ->
                                     receipt for the tree (lanefather.checks)
  land <id> [--into WORKTREE] [--keep]
                                     merge after a current green receipt, start the review, run the post-merge
                                     smoke, finish the lane (lanefather.land)
  review <id> [--mark N fixed:<merge>|rejected:<reason>] [--fix-lane]
  review --overall <sha> --since <base>
                                     the independent review (lanefather.review)
  done <id>                          after landing: stop what the lane left running, remove its worktree,
                                     branches, build directories and scratch
  promote <sha> [--ship] [--no-push] [--install [--deploy HOST]]
  sync [--drop-local]
  perf <sha>                         the batch gate, promotion, sync and the perf suite (lanefather.promote)
  released <version> --since REF     after a release publish: comment the version on each issue a Fixes #N line
                                     in REF..HEAD names (lanefather.tickets, [tickets] release_comment)
  list [--all] [--json]
  status [--json]                    every registered project's lanes on this machine (lanefather.status)
  settings [check|set|reset KEY [VALUE]] [--json]
                                     this machine's settings: review start, rerun wait, slot priorities
                                     (lanefather.settings)

--config PATH (or LANEFATHER_CONFIG) names the project's .lanefather.toml; by default it is read from the top of the
repository the command runs in. State: one JSON file per lane in the configured state directory (LANEFATHER_STATE
replaces it), receipts beside it, and every action and override in ledger.log. Refusals name the rule;
`--force-reason` overrides a cap or a missing receipt and is logged.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

from . import (
    __version__,
    checks,
    config,
    done,
    gc,
    land,
    lanes,
    promote,
    registry,
    review,
    runtime,
    settings,
    show,
    status,
    tickets,
)
from .errors import LaneError
from .host import CHECK_MAX_JOBS


def version() -> str:
    """The version, and the commit install.py recorded beside the package (none in a checkout)."""
    try:
        built = (Path(__file__).with_name("COMMIT")).read_text().strip()
    except OSError:
        built = "from a checkout"
    return f"lanefather {__version__} ({built})"


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog=config.prog(), description=__doc__,
                                   formatter_class=argparse.RawDescriptionHelpFormatter)
    root.add_argument("--config", metavar="PATH", help="the project's .lanefather.toml")
    root.add_argument("--version", action="version", version=version())
    commands = root.add_subparsers(dest="command", required=True)
    p = commands.add_parser("capabilities", help="versioned capabilities and verified installed source identity")
    p.add_argument("--json", action="store_true")
    p.set_defaults(run=runtime.capabilities)
    p = commands.add_parser("project-hook", help="run one configured project policy in the installed runtime")
    p.add_argument("name")
    p.add_argument("arguments", nargs=argparse.REMAINDER)
    p.set_defaults(run=runtime.project_hook)
    p = commands.add_parser("start", help="register a lane: lane cap, path claims and agent brief")
    p.add_argument("id")
    p.add_argument("--ticket", required=True, help="issue number, or none")
    p.add_argument("--claims", nargs="+", required=True, metavar="GLOB")
    p.add_argument("--base", help="integration branch or commit (default: this worktree's branch)")
    p.add_argument("--branch", help="lane branch (default: lane/<id>)")
    p.add_argument("--after", nargs="+", default=[], metavar="ID",
                   help="land after these lanes; their claims may overlap")
    p.add_argument("--scratch", nargs="+", default=[], metavar="DIR", help="extra scratch dirs done removes")
    p.add_argument("--create", action="store_true", help="create the worktree now instead of adopting one")
    p.add_argument("--force-reason", help="start despite the lane cap, disk or load; logged")
    p.set_defaults(run=lanes.start)
    p = commands.add_parser("next", help="the first ready entry of the queue: its brief header; --start registers it")
    p.add_argument("--start", action="store_true", help="register the lane, as start would")
    p.add_argument("--base", help="integration branch (default: the queue's base, else this worktree's branch)")
    p.add_argument("--create", action="store_true", help="with --start: create the worktree now")
    p.add_argument("--force-reason", help="with --start: start despite the lane cap, disk or load; logged")
    p.set_defaults(run=lanes.next_lane)
    p = commands.add_parser("adopt", help="register the worktree an Agent created")
    p.add_argument("id")
    p.add_argument("worktree")
    p.add_argument("branch")
    p.set_defaults(run=lanes.adopt)
    p = commands.add_parser("check", help="run the lane's affected checks and write its receipt")
    p.add_argument("id")
    p.add_argument("--rerun", action="store_true", help="run every suite even if a receipt covers the tree")
    p.add_argument("--slots", type=int, choices=range(1, 5),
                   help="verification worker budget passed as verify --jobs (default: slotfather advise, at most "
                        f"{CHECK_MAX_JOBS})")
    p.set_defaults(run=checks.check)
    p = commands.add_parser("land", help="merge a lane with a current green receipt, run the smoke, then finish it")
    p.add_argument("id")
    p.add_argument("--into", help="integration worktree (default: the worktree on the lane's base branch)")
    p.add_argument("-m", "--message")
    p.add_argument("--slots", type=int, choices=range(1, 5),
                   help="worker budget passed as verify --jobs for the post-merge smoke "
                        "(default: slotfather advise --max 3)")
    p.add_argument("--keep", action="store_true",
                   help="leave the landed lane for done (another lane needs its artifacts)")
    p.add_argument("--force-reason", help="land without a current green receipt; logged")
    p.add_argument("--wait", type=float, default=120, metavar="SECONDS", help="wait this long for the landing lock")
    p.set_defaults(run=land.land)
    p = commands.add_parser("promote", help="the batch gate on a SHA, then fast-forward local dev (and main) to it "
                                            "and push it")
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
    p = commands.add_parser("sync", help="fetch, then fast-forward local dev and main to the remote under the landing "
                                         "lock; --drop-local resets them to it, keeping a backup branch")
    p.add_argument("--drop-local", action="store_true",
                   help="reset local dev and main to the remote; the commits it lacks go to backup/<branch>-<date>")
    p.add_argument("--remote", default="origin")
    p.add_argument("--wait", type=float, default=120, metavar="SECONDS", help="wait this long for the landing lock")
    p.set_defaults(run=promote.sync)
    p = commands.add_parser("perf", help="the perf suite (#579) on a SHA in the gate worktree, alone on the machine; "
                                         "moves nothing")
    p.add_argument("sha")
    p.add_argument("--remote", default="origin", help="whose main a red run bisects from (default: origin)")
    p.set_defaults(run=promote.perf)
    p = commands.add_parser("released", help="after a release publish: comment the version on each issue its "
                                             "Fixes #N lines close ([tickets] release_comment)")
    p.add_argument("version", help="the published version, for example v1.4.0")
    p.add_argument("--since", required=True, metavar="REF", help="the previous release (tag or commit)")
    p.add_argument("--head", default="HEAD", metavar="REF", help="the released commit (default: HEAD)")
    p.set_defaults(run=tickets.released)
    p = commands.add_parser("review", help="the lane's one independent review (Sol, or Opus when Codex is low), its "
                                           "marks and its fix lane; --overall for promote --ship")
    p.add_argument("id", nargs="?")
    p.add_argument("--mark", nargs=2, action="append", metavar=("N", "fixed:MERGE|rejected:REASON"),
                   help="mark finding N fixed by a follow-up merge (or the fix lane's id) or rejected with a reason")
    p.add_argument("--fix-lane", action="store_true",
                   help="register <id>-review-fixes on the landed lane's worktree for the fix round")
    p.add_argument("--overall", metavar="SHA",
                   help="the overall review of --since..SHA; its findings become one ticket")
    p.add_argument("--since", metavar="BASE", help="with --overall: the base, origin/main for promote --ship")
    p.add_argument("--force-reason", help="review again although the lane has a review, or register the fix lane "
                                          "over the lane cap; logged")
    p.set_defaults(run=review.review)
    p = commands.add_parser("done", help="remove a landed lane's worktree, branches, targets and scratch")
    p.add_argument("id")
    p.add_argument("--force-reason", help="clean up a lane that is not landed or lacks a ticket comment; logged")
    p.set_defaults(run=done.done)
    p = commands.add_parser("gc", help="remove merged idle agent worktrees and unused branches")
    p.add_argument("--dry-run", action="store_true", help="report removals without changing anything")
    p.add_argument("--json", action="store_true", help="machine-readable removals, retained paths and errors")
    p.set_defaults(run=gc.collect)
    p = commands.add_parser("show", help="read-only lane details for people and dashboards")
    p.add_argument("id")
    p.add_argument("--json", action="store_true", help="version-1 lane summary")
    p.add_argument("--state-dir", metavar="DIR", help="explicit lane-state directory, without creating it")
    p.add_argument("--project-dir", metavar="DIR", help="resolve project config and verify lane repository identity")
    p.set_defaults(run=show.show)
    p = commands.add_parser("status", help="every registered project's lanes on this machine, read-only, for "
                                           "dashboards")
    p.add_argument("--json", action="store_true", help="the version-1 snapshot")
    p.set_defaults(run=status.status)
    p = commands.add_parser("settings", help="this machine's Lanefather settings: show, check, set or reset")
    p.add_argument("action", nargs="?", choices=("check", "set", "reset"))
    p.add_argument("key", nargs="?")
    p.add_argument("value", nargs="?")
    p.add_argument("--all", action="store_true", help="with reset: every setting")
    p.add_argument("--revision", help="with set or reset: refuse when the settings changed since this revision")
    p.add_argument("--json", action="store_true")
    p.set_defaults(run=settings.settings)
    p = commands.add_parser("list", help="lanes that are not done, with claims, receipts, disk and load")
    p.add_argument("--all", action="store_true", help="include done lanes")
    p.add_argument("--json", action="store_true", help="the lane records with their worktree and receipt state, the "
                                                        "cap and the host, as JSON")
    p.set_defaults(run=lanes.list_lanes)
    return root


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


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        given = config_option(argv)
        config.use(config.load(Path(given)) if given else None)
        args = parser().parse_args(argv)
        if args.command not in registry.READ_ONLY:
            registry.note(config.current())
        return args.run(args)
    except LaneError as error:
        print(f"lane: {error}", file=sys.stderr)
        return 1
