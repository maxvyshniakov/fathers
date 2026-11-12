"""What's new across the fathers (fathers#102): each tool's changelog entries through its `changelog --json`, the
installed versions, and which entries are read (docs/contracts/changelog.md).

Godfather's own entries come from the CHANGELOG.md it ships with; every other tool is asked through sources.run(),
never imported. A tool without `changelog --json` shows its version only, from `version --json` or else
`capabilities --json`. The read state is one version per tool in Godfather's own state directory."""
from __future__ import annotations

import contextlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple, Optional, cast

from . import sources as _sources
from . import version as _version

TOOLS = ("godfather", "slotfather", "lanefather", "junkfather")  # the versions row's order, and a date's ties
TIMEOUT = 2.0  # seconds for each call
CONTRACT = 1


def title(tool: str) -> str:
    """A tool's name as UI text says it: capitalised (owner, 2026-11-12); commands and paths stay lowercase."""
    return tool[:1].upper() + tool[1:]


# Godfather's own CHANGELOG.md: the Sid-Kit layout, the same rules as a tool's `changelog --json`

RELEASE = re.compile(r"^## \[?([^\]\s]+)\]?(?: - (\d{4}-\d\d-\d\d))?(?: \((\w+)\))?")
MARKUP = re.compile(r"\*\*|`|\[([^\]]*)\]\([^)]*\)")


class Release(NamedTuple):
    version: str
    date: str
    commit: str
    sections: list[tuple[str, list[str]]]  # (Added, [entries])


def plain(text: str) -> str:
    """Markdown taken out of a changelog entry: bold, code and links."""
    return MARKUP.sub(lambda found: found[1] or "", text)


def parse_changelog(text: str) -> list[Release]:
    """The releases of a Keep a Changelog file, newest first, with markdown taken out of the entries; an entry's
    continuation lines join it."""
    found: list[Release] = []
    for line in text.splitlines():
        heading = RELEASE.match(line)
        if heading:
            found.append(Release(heading[1], heading[2] or "", heading[3] or "", []))
        elif not found:
            continue
        elif line.startswith("### "):
            found[-1].sections.append((line[4:].strip(), []))
        elif line.startswith("- ") and found[-1].sections:
            found[-1].sections[-1][1].append(plain(line[2:].strip()))
        elif line.startswith("  ") and line.strip() and found[-1].sections and found[-1].sections[-1][1]:
            entries = found[-1].sections[-1][1]
            entries[-1] += " " + plain(line.strip())
    return found


def version_key(version: str) -> tuple[int, ...]:
    """A version's numbers, its build metadata (`+1138486`, a commit) left out: never ordered by a hash."""
    return tuple(int(part) for part in re.findall(r"\d+", version.split("+")[0])) or (-1,)


def released(version: str) -> bool:
    return version[:1].isdigit()


# The model

@dataclass(frozen=True)
class Item:
    """One changelog entry. An Unreleased one (installs come from main, so it is what is installed) has no
    `version`; its `commit` is the installed commit, `base` the installed version, and `date` the install date or
    empty."""
    tool: str
    version: str
    date: str
    section: str
    text: str
    commit: str = ""
    base: str = ""

    @property
    def unreleased(self) -> bool:
        return not self.version

    def token(self) -> str:
        """What the read state keeps for a tool read up to this entry: its version, or for an Unreleased one the
        installed version and commit (`0.1.0+81abd86`), so a later install's Unreleased entries are new again."""
        return f"{self.base}+{self.commit or 'unreleased'}" if self.unreleased else self.version


@dataclass(frozen=True)
class Installed:
    """A tool's row in the versions line. state: ok · stale (Godfather: a newer install waits for a restart) ·
    versions (no `changelog --json`) · absent · failed (no version either) · reading (not asked yet)."""
    tool: str
    version: Optional[str]
    state: str
    problem: Optional[str] = None


@dataclass(frozen=True)
class News:
    installed: tuple[Installed, ...]
    items: tuple[Item, ...]  # every tool's entries, Unreleased ones too, newest first (ordered())


def ordered(items: list[Item]) -> tuple[Item, ...]:
    """Newest first: by date (an Unreleased entry by its install date, above every dated one without it), a date's
    tools in TOOLS order, a tool's Unreleased entries above its releases, then the order its changelog gives (newest
    first), never by version: `0.1.0+1138486` and `0.1.0+7eca44a` differ only by a build hash."""
    rank = {tool: index for index, tool in enumerate(TOOLS)}
    indexed = list(enumerate(items))
    indexed.sort(key=lambda pair: (pair[1].date or ("~" if pair[1].unreleased else ""),
                                   -rank.get(pair[1].tool, len(TOOLS)), pair[1].unreleased, -pair[0]), reverse=True)
    return tuple(item for _, item in indexed)


def own(text: Optional[str] = None, stale: Optional[bool] = None) -> tuple[Installed, list[Item]]:
    """Godfather's row and its entries, Unreleased ones too (the running commit's), from its own CHANGELOG.md."""
    text = _version.changelog_text() if text is None else text
    if stale is None:
        installed = _version.installed_source()
        stale = installed is not None and installed != _version.running_source()
    commit = str(_version.running_identity().get("commit") or "")[:7]
    items: list[Item] = []
    for release in parse_changelog(text):
        for section, entries in release.sections:
            for entry in entries:
                if released(release.version) and release.date:  # a dated release, as `changelog --json` says
                    items.append(Item("godfather", release.version, release.date, section, entry))
                elif release.version.lower() == "unreleased":
                    items.append(Item("godfather", "", "", section, entry, commit, _version.VERSION))
    return Installed("godfather", _version.VERSION, "stale" if stale else "ok"), items


def from_reply(tool: str, value: Any) -> Optional[tuple[str, list[Item]]]:
    """A `changelog --json` reply as (version, entries), or None where it is not the contract: another `v` or tool,
    no version, entries that are not a list. An entry missing a string field is skipped; an Unreleased one
    (`unreleased: true`, `version` null) needs only its section and text, and its `source` is the installed commit."""
    if not isinstance(value, dict):
        return None
    reply = cast(dict[str, Any], value)
    if reply.get("v", CONTRACT) != CONTRACT or reply.get("tool") != tool or not isinstance(reply.get("version"), str) \
            or not isinstance(reply.get("entries"), list):
        return None
    items: list[Item] = []
    for entry in cast(list[Any], reply["entries"]):
        found: dict[str, Any] = cast(dict[str, Any], entry) if isinstance(entry, dict) else {}
        if found.get("unreleased") is True:
            section, text, date, commit = (found.get(name) for name in ("section", "text", "date", "source"))
            if found.get("version") is None and isinstance(section, str) and section and isinstance(text, str) \
                    and text:
                items.append(Item(tool, "", date if isinstance(date, str) else "", section, plain(text),
                                  commit[:7] if isinstance(commit, str) else "", str(reply["version"])))
            continue
        fields: list[Any] = [found.get(name) for name in ("version", "date", "section", "text")]
        if all(isinstance(field, str) and field for field in fields):
            version, date, section, text = cast(list[str], fields)
            items.append(Item(tool, version, date, section, plain(text)))
    return str(reply["version"]), items


def tool(name: str) -> tuple[Installed, list[Item]]:
    """One tool's row and entries: `changelog --json`, else its version alone from `version --json` or
    `capabilities --json`."""
    executable = _sources.which(name)
    if executable is None:
        return Installed(name, None, "absent"), []
    code, value, _ = _sources.run_json(name, ["changelog", "--json"], TIMEOUT, executable)
    found = from_reply(name, value) if code == 0 else None
    if found is not None:
        return Installed(name, found[0], "ok"), found[1]
    problem = f"{name} changelog --json exited {code}" if code else f"{name} changelog --json: not the contract"
    for argv in (["version", "--json"], ["capabilities", "--json"]):
        code, value, _ = _sources.run_json(name, argv, TIMEOUT, executable)
        version = cast(dict[str, Any], value).get("version") if code == 0 and isinstance(value, dict) else None
        if isinstance(version, str) and version:
            return Installed(name, version, "versions", problem), []
    return Installed(name, None, "failed", problem), []


def load(text: Optional[str] = None, stale: Optional[bool] = None) -> News:
    """Every tool's news, Godfather's first: what the overlay and the footer show."""
    row, items = own(text, stale)
    rows = [row]
    for name in TOOLS[1:]:
        found, more = tool(name)
        rows.append(found)
        items += more
    return News(tuple(rows), ordered(items))


def placeholder(text: Optional[str] = None) -> News:
    """Before the tools have answered: Godfather's own news, the others being read."""
    row, items = own(text, False)
    return News((row, *(Installed(name, None, "reading") for name in TOOLS[1:])), ordered(items))


# Read state

Read = dict[str, str]  # tool: the newest version read, or `version+commit` once its Unreleased entries were read


def read_version(value: str) -> tuple[int, ...]:
    return version_key(value.split("+")[0])


def unread(news: Optional[News], read: Read) -> list[Item]:
    """The entries not read yet; all of a tool's while it has none. An Unreleased entry until its install's token is
    read; a release above the one read in the timeline (newest first), or by version where the one read is gone."""
    if news is None:
        return []
    found: list[Item] = []
    passed: set[str] = set()  # tools whose read release the timeline has reached
    for item in news.items:
        value = read.get(item.tool)
        if value is None:
            found.append(item)
        elif item.unreleased:
            if value != item.token():
                found.append(item)
        elif item.version == value or item.tool in passed:
            passed.add(item.tool)
        elif any(other.tool == item.tool and other.version == value for other in news.items):
            found.append(item)  # above the release read
        elif version_key(item.version) > read_version(value):
            found.append(item)
    return found


def mark_read(news: Optional[News], read: Read) -> Read:
    """Every tool read up to its newest entry, its Unreleased ones where it has them; a tool with none keeps what it
    had."""
    marked = dict(read)
    newest: dict[str, Item] = {}
    for item in news.items if news is not None else ():  # newest first: a tool's first entry, Unreleased above all
        best = newest.get(item.tool)
        if best is None or (item.unreleased and not best.unreleased):
            newest[item.tool] = item
    for name, item in newest.items():
        marked[name] = item.token()
    return marked


def state_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return Path(base) / "godfather" / "news.json"


def load_read() -> Read:
    """The saved read versions; {} when there are none or they cannot be read."""
    with contextlib.suppress(OSError, ValueError):
        found: Any = json.loads(state_path().read_text(encoding="utf-8"))
        read: Any = cast(dict[str, Any], found).get("read") if isinstance(found, dict) else None
        if isinstance(read, dict):
            return {str(key): str(value) for key, value in cast(dict[str, Any], read).items()
                    if isinstance(value, str)}
    return {}


def save_read(read: Read) -> None:
    """Writes them atomically; a failure leaves the old file."""
    path = state_path()
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        pending = path.with_name(f".news-{os.getpid()}.json")
        pending.write_text(json.dumps({"v": 1, "read": read}, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(pending, path)
