"""The tokenfather command line (docs/cli.md, fathers#199)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import textwrap
from typing import Any, NoReturn

from fathers_common.cli import UsageParser, format_top_help

from . import __version__, events
from . import changelog as _changelog
from .attribution import LaneIndex, discover_lane_dirs, load_lanes
from .pricing import load_pricing
from .report import build_report, format_report
from .store import RollupStore

WIDTH = 78


def version() -> str:
    try:
        built = (Path(__file__).with_name("COMMIT")).read_text(encoding="utf-8").strip()
    except OSError:
        built = "from a checkout"
    return f"tokenfather {__version__} ({built})"


class Command:
    def __init__(self, summary: str, usage: str, example: str):
        self.summary, self.usage, self.example = summary, usage, example


COMMANDS: dict[str, Command] = {
    "collect": Command(
        "incrementally collect transcripts into the daily rollup",
        "[--state-dir DIR] [--json]",
        "--json",
    ),
    "report": Command(
        "usage report across providers, models, roles and lanes",
        "[--since D] [--until D] [--project P] [--no-collect] [--state-dir DIR] [--json]",
        "--since 2026-11-15T22:00Z --until 2026-11-17 --json",
    ),
    "changelog": Command(
        "the installed release's changes, Unreleased first",
        "[--json] [--since VERSION]",
        "--since 0.1.0",
    ),
    "capabilities": Command(
        "versioned capabilities and installed identity",
        "[--json]",
        "--json",
    ),
}


class Parser(UsageParser):
    def __init__(self, *args: Any, row: Command | None = None, tool: str = "tokenfather", **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.row, self.tool = row, tool
        self.children: dict[str, Parser] = {}

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

    def format_help(self) -> str:
        if self.row is None:
            return format_top_help(
                f"{self.tool} <command> ...",
                "Multi-provider agent token usage collector, daily rollup and lane reporter.",
                (("commands", [(name, row.summary) for name, row in COMMANDS.items()]),),
                (("-h, --help", "show this help and exit"), ("--version", "show version and exit")),
                f"Run '{self.tool} <command> --help' for options and an example.",
            )
        return super().format_help()

    def option_name(self, name: str) -> str:
        return repr(name)


def _add(root: Parser, commands: Any, name: str, details: str = "") -> Parser:
    row = COMMANDS[name]
    text = row.summary[0].upper() + row.summary[1:] + "." + (" " + details if details else "")
    child: Parser = commands.add_parser(
        name,
        help=row.summary,
        row=row,
        tool="tokenfather",
        prog=f"tokenfather {name}",
        parent=root,
        usage=f"%(prog)s {row.usage}",
        description=textwrap.fill(text, WIDTH),
        epilog=f"example: tokenfather {name} {row.example}".rstrip(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    root.children[name] = child
    return child


def cmd_collect(args: Any) -> int:
    store = RollupStore(Path(args.state_dir) if args.state_dir else None)
    summary = store.collect()
    payload = {
        "v": 1,
        "files_scanned": summary.files_scanned,
        "files_updated": summary.files_updated,
        "new_requests": summary.new_requests,
        "days_updated": summary.days_updated,
        "by_provider": summary.by_provider,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(
            f"Collected {summary.new_requests} new requests from {summary.files_updated}/{summary.files_scanned} files"
        )
    return 0


def cmd_report(args: Any) -> int:
    from .schema import parse_timestamp

    if args.since and parse_timestamp(args.since) is None:
        print(f"tokenfather report: invalid --since timestamp: {args.since!r}", file=sys.stderr)
        return 2
    if args.until and parse_timestamp(args.until) is None:
        print(f"tokenfather report: invalid --until timestamp: {args.until!r}", file=sys.stderr)
        return 2
    store = RollupStore(Path(args.state_dir) if args.state_dir else None)
    if not args.no_collect:
        store.collect()
    lane_index = LaneIndex(load_lanes(discover_lane_dirs()))
    records = store.read_records(since=args.since, until=args.until, project=args.project, lane_index=lane_index)
    pricing = load_pricing(Path(args.models) if args.models else None)
    rep = build_report(
        records,
        pricing,
        lane_index=lane_index,
        since=args.since,
        until=args.until,
        project=args.project,
    )
    if args.json:
        print(json.dumps(rep, indent=2))
    else:
        print(format_report(rep))
    return 0


def cmd_changelog(args: Any) -> int:
    try:
        found = _changelog.contract(args.since)
    except ValueError as err:
        print(f"tokenfather changelog: {err}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(found))
    else:
        print(_changelog.text(found["entries"]) or "no changelog entries")
    return 0


def cmd_capabilities(args: Any) -> int:
    payload = {
        "v": 1,
        "tool": "tokenfather",
        "version": __version__,
        "providers": ["claude-code", "codex", "antigravity"],
        "dashboard": {"v": 1, "tag": events.TAG, "events": {"path": str(events.path()), "versions": [1]}},
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"tokenfather {__version__} (providers: claude-code, codex, antigravity)")
    return 0


def parser() -> Parser:
    root = Parser(
        prog="tokenfather",
        tool="tokenfather",
        usage="%(prog)s <command> ...",
        description="Multi-provider agent token usage collector, daily rollup, and lane-attributed reporter.",
        epilog="'tokenfather <command> --help' gives a command's options and an example.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    root.add_argument("--version", action="version", version=version())
    commands = root.add_subparsers(dest="command", required=True, metavar="<command>", title="commands")

    p = _add(root, commands, "collect", "Reads only newly appended transcript lines and updates daily rollups.")
    p.add_argument("--state-dir", metavar="DIR",
                   help="override the state directory (default ~/.local/state/tokenfather)")
    p.add_argument("--json", action="store_true", help="print the collection summary as JSON")
    p.set_defaults(run=cmd_collect)

    p = _add(root, commands, "report", "Summarizes usage per provider, model/effort, role, day, week, and lane.")
    p.add_argument("--since", metavar="D", help="include requests at or after ISO-8601 date/timestamp D")
    p.add_argument("--until", metavar="D", help="include requests before ISO-8601 date/timestamp D")
    p.add_argument("--project", metavar="P", help="filter to projects or lanes matching substring P")
    p.add_argument("--no-collect", action="store_true", help="read existing daily rollups without scanning transcripts")
    p.add_argument("--state-dir", metavar="DIR", help="override the state directory")
    p.add_argument("--models", metavar="PATH", help="override models.toml path for price weights")
    p.add_argument("--json", action="store_true", help="print the full report as JSON")
    p.set_defaults(run=cmd_report)

    p = _add(root, commands, "changelog")
    p.add_argument("--since", metavar="VERSION", help="only Unreleased and releases newer than VERSION")
    p.add_argument("--json", action="store_true", help="print the changelog contract reply as JSON")
    p.set_defaults(run=cmd_changelog)

    p = _add(root, commands, "capabilities")
    p.add_argument("--json", action="store_true", help="print capabilities as JSON")
    p.set_defaults(run=cmd_capabilities)

    return root


def parse(argv: list[str] | None = None) -> argparse.Namespace:
    root = parser()
    args, extra = root.parse_known_args(argv)
    if extra:
        chosen = root.children.get(args.command, root)
        if chosen.rejects_option(extra[0]):
            chosen.unknown_option(extra[0])
        chosen.usage_error(f"unrecognized arguments: {' '.join(extra)}")
    return args


def main(argv: list[str] | None = None) -> int:
    parsed = parse(argv)
    return int(parsed.run(parsed))
