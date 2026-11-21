"""The tokenfather command line (docs/cli.md, fathers#199)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import textwrap
from typing import Any, NoReturn

from fathers_common.cli import UsageParser

from . import __version__, events
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


def _parse_version(raw: str) -> tuple[int, ...] | None:
    cleaned = raw.strip().lstrip("v")
    parts = cleaned.split(".")
    if not parts or not all(part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def filter_changelog_since(text: str, since: str) -> str:
    since_ver = _parse_version(since)
    if since_ver is None:
        raise ValueError(f"invalid --since version: {since!r}")
    lines = text.splitlines()
    header: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    current_heading: str | None = None
    current_lines: list[str] = []
    for line in lines:
        if line.startswith("## "):
            if current_heading is not None:
                sections.append((current_heading, current_lines))
            current_heading = line
            current_lines = [line]
        elif current_heading is None:
            header.append(line)
        else:
            current_lines.append(line)
    if current_heading is not None:
        sections.append((current_heading, current_lines))
    kept: list[str] = list(header)
    for heading, sec_lines in sections:
        if heading.strip() == "## Unreleased":
            body = [ln for ln in sec_lines[1:] if ln.strip()]
            if body:
                kept.extend(sec_lines)
            continue
        ver_token = heading[3:].strip().split()[0] if len(heading) > 3 else ""
        ver = _parse_version(ver_token)
        if ver is not None and ver > since_ver:
            kept.extend(sec_lines)
    return "\n".join(kept).rstrip()


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
    candidates = [Path(__file__).with_name("CHANGELOG.md"), Path(__file__).resolve().parents[1] / "CHANGELOG.md"]
    text = ""
    source: str | None = None
    for candidate in candidates:
        if candidate.is_file():
            text = candidate.read_text(encoding="utf-8")
            source = str(candidate)
            break
    if args.since:
        try:
            text = filter_changelog_since(text, args.since)
        except ValueError as err:
            print(f"tokenfather changelog: {err}", file=sys.stderr)
            return 2
    if args.json:
        print(json.dumps({"v": 1, "version": __version__, "source": source, "since": args.since, "text": text}))
    else:
        print(text.rstrip())
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
    p.add_argument("--since", metavar="VERSION", help="show entries since VERSION")
    p.add_argument("--json", action="store_true", help="print changelog metadata as JSON")
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
