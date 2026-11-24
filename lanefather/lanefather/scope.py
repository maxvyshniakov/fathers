"""A lane's accepted scope (fathers#192): the tickets it completes, the tickets it does only part of, and the owner
decisions and specs it answers to, in the order the coordinator gave them.

The record keeps `ticket`, the primary ticket every older reader uses: the first issue the lane completes, else its
first issue, else `none`. The optional `scope` key holds the whole list:

    "scope": [{"issue": "161"}, {"issue": "64", "partial": true},
              {"decision": "https://github.com/o/r/issues/161#issuecomment-2541"}, {"spec": "docs/specs/lanes.md"}]

A record without `scope` (every record before fathers#192, and a lane that completes one ticket and names nothing
else, which therefore stays byte-identical to them) reads as `[{"issue": ticket}]`, or as no scope for `none`. A
malformed `scope` warns and reads the same way; it never fails a command.

The lane review reads the whole scope (`resolve`) and shows it to the reviewer (`prompt_text`): each ticket's title
and body without its comments, each decision's one comment, each spec as it stands at the review's base. What it
cannot read it names, so the reviewer reports it instead of the review silently covering less.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
import hashlib
import re
import subprocess
import sys
from typing import Any, Literal, cast

from . import config, github
from .errors import LaneError
from .gitops import main_checkout
from .state import Record

NONE = "none"
# A ticket is an issue number as start always took it: GitHub's digits, or a project's own token.
TICKET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
# A ticket of another repository (fathers#238): `owner/repo#N`, or its issue URL, which reads as that form.
_OWNER = r"[A-Za-z0-9][A-Za-z0-9-]{0,38}"
_REPO = r"(?!\.\.?/|\.\.?#)[A-Za-z0-9_.-]{1,100}"
FOREIGN = re.compile(rf"^({_OWNER})/({_REPO})#(\d+)$")
ISSUE_URL = re.compile(rf"^https://github\.com/({_OWNER})/({_REPO})/issues/(\d+)/?$")
DECISION = re.compile(
    r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/issues/(\d+)#issuecomment-(\d+)$")
_WARNED: set[tuple[str, str]] = set()


@dataclass(frozen=True)
class Issue:
    """A ticket of the lane's scope; a partial one the lane does only part of, so its merge says `Refs`.

    `number` is the ticket as the record stores it: the lane's own repository's number (`12`), as always, or
    `owner/repo#N` for a ticket of another repository (fathers#238)."""
    number: str
    partial: bool = False

    @property
    def repo(self) -> str | None:
        """The other repository a foreign ticket belongs to; None for the lane's own."""
        head, sep, _ = self.number.rpartition("#")
        return head if sep and "/" in head else None

    @property
    def local(self) -> str:
        """The issue number within its repository."""
        return self.number.rpartition("#")[2]

    @property
    def ref(self) -> str:
        """How GitHub refers to it from the lane's repository: `#12`, or `owner/repo#97`."""
        return self.number if self.repo else f"#{self.number}"

    @property
    def key(self) -> str:
        """Case-folded identity for deduplication: GitHub owner/repo names are case-insensitive."""
        return f"{self.repo.lower()}#{self.local}" if self.repo else self.number


def display_ticket(ticket: Any) -> str:
    """How a ticket prints in prose: `#42` for a local ticket, `owner/repo#97` for a foreign one, or `none`."""
    value = str(ticket).strip()
    if not value or value.lower() == NONE:
        return NONE
    return Issue(value.lstrip("#")).ref


@dataclass(frozen=True)
class Decision:
    """An owner or coordinator decision: one GitHub issue comment, never an implementer's report."""
    url: str
    repo: str
    issue: str
    comment: str


@dataclass(frozen=True)
class Spec:
    """A spec file, relative to the repository."""
    path: str


Item = Issue | Decision | Spec


def ticket_number(value: str) -> str:
    """A ticket as the record stores it: an issue number as given (`#12` or `12`), or `owner/repo#N` for a ticket
    of another repository, given so or as its issue URL; else a refusal."""
    given = str(value).strip()
    found = FOREIGN.match(given) or ISSUE_URL.match(given)
    if found:
        owner, name, number = found.groups()
        return f"{owner}/{name}#{number}"
    number = given.lstrip("#")
    if not TICKET.match(number) or number.lower() == NONE:
        raise LaneError(f"ticket {value!r}: give an issue number, owner/repo#N, or "
                        "https://github.com/<owner>/<repo>/issues/<n>")
    return number


def parse_decision(url: str) -> Decision:
    """https://github.com/<owner>/<repo>/issues/<n>#issuecomment-<id>, else a refusal."""
    found = DECISION.match(str(url).strip())
    if not found:
        raise LaneError(f"decision {url!r}: give the URL of one issue comment, "
                        "https://github.com/<owner>/<repo>/issues/<n>#issuecomment-<id>")
    owner, name, issue, comment = found.groups()
    return Decision(url=found.group(0), repo=f"{owner}/{name}", issue=issue, comment=comment)


def parse_spec(path: str) -> Spec:
    """A spec path relative to the repository, without `..`, else a refusal."""
    given = str(path).strip()
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in given):
        raise LaneError(f"spec {path!r}: give a file path relative to the repository, without '..'")
    cleaned = given
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    parts = cleaned.split("/")
    if not cleaned or cleaned.startswith("/") or "\\" in cleaned or ".." in parts or "." in parts or "" in parts:
        raise LaneError(f"spec {path!r}: give a file path relative to the repository, without '..'")
    return Spec(path=cleaned)


def validate(items: Sequence[Item]) -> None:
    """Refuse an issue, decision or spec named twice."""
    seen: set[tuple[str, str]] = set()
    for item in items:
        if isinstance(item, Issue):
            key, shown = ("issue", item.key), item.ref
        elif isinstance(item, Decision):
            key, shown = ("decision", item.url), item.url
        else:
            key, shown = ("spec", item.path), item.path
        if key in seen:
            raise LaneError(f"scope names {key[0]} {shown} twice")
        seen.add(key)


def from_options(issues: Iterable[tuple[str, bool]], decisions: Iterable[str] = (),
                 specs: Iterable[str] = ()) -> tuple[Item, ...]:
    """start's --ticket, --partial (in the order given), --decision and --spec options as a scope."""
    given = list(issues)
    for value, partial in given:
        if partial and str(value).strip().lstrip("#").lower() == NONE:
            raise LaneError("--partial none is invalid: use --ticket none for a lane without a ticket")
    named = [(value, partial) for value, partial in given if str(value).strip().lstrip("#").lower() != NONE]
    if named and len(named) != len(given):
        raise LaneError("--ticket none names no ticket: drop it, or drop the other tickets")
    items: list[Item] = [Issue(ticket_number(value), partial) for value, partial in named]
    items += [parse_decision(url) for url in decisions]
    items += [parse_spec(path) for path in specs]
    validate(items)
    return tuple(items)


def from_ticket(ticket: str) -> tuple[Item, ...]:
    """The scope of a record without `scope`: its one ticket, or nothing for `none`."""
    number = str(ticket).strip().lstrip("#")
    return () if number.lower() == NONE else (Issue(number),)


def primary(items: Iterable[Item]) -> str:
    """The ticket older readers see: the first issue the lane completes, else its first issue, else `none`."""
    found = [item for item in items if isinstance(item, Issue)]
    first = next((item for item in found if not item.partial), found[0] if found else None)
    return first.number if first else NONE


def to_json(item: Item) -> dict[str, Any]:
    if isinstance(item, Issue):
        return {"issue": item.number, "partial": True} if item.partial else {"issue": item.number}
    if isinstance(item, Decision):
        return {"decision": item.url}
    return {"spec": item.path}


def from_json(raw: Any) -> tuple[Item, ...]:
    """The scope a record's `scope` key holds; ValueError or LaneError when it is malformed."""
    if not isinstance(raw, list):
        raise ValueError("scope is not a list")
    items: list[Item] = []
    for entry in cast(list[Any], raw):
        if not isinstance(entry, dict):
            raise ValueError(f"scope entry {entry!r} is not an object")
        fields = cast(dict[str, Any], entry)
        if "issue" in fields and set(fields) <= {"issue", "partial"}:
            if not isinstance(fields["issue"], str):
                raise ValueError(f"scope entry {fields!r}: issue is not a string")
            partial = fields.get("partial", False)
            if not isinstance(partial, bool):
                raise ValueError(f"scope entry {fields!r}: partial is not a boolean")
            items.append(Issue(ticket_number(fields["issue"]), partial))
        elif set(fields) == {"decision"}:
            if not isinstance(fields["decision"], str):
                raise ValueError(f"scope entry {fields!r}: decision is not a string")
            items.append(parse_decision(fields["decision"]))
        elif set(fields) == {"spec"}:
            if not isinstance(fields["spec"], str):
                raise ValueError(f"scope entry {fields!r}: spec is not a string")
            items.append(parse_spec(fields["spec"]))
        else:
            raise ValueError(f"scope entry {fields!r} is not an issue, decision or spec")
    validate(items)
    return tuple(items)


def of(record: Record) -> tuple[Item, ...]:
    """The lane's scope: `scope` when it is valid, else the legacy reading of `ticket` (with one warning)."""
    raw = record.get("scope")
    if raw is None:
        return from_ticket(str(record.get("ticket", NONE)))
    try:
        return from_json(raw)
    except (ValueError, LaneError) as error:
        key = (str(record.get("id")), repr(raw))
        if key not in _WARNED:
            _WARNED.add(key)
            print(f"lane: warning: lane {record.get('id')} has a malformed scope ({error}); reading its ticket "
                  f"{display_ticket(record.get('ticket'))} alone", file=sys.stderr)
        return from_ticket(str(record.get("ticket", NONE)))


def issues(record: Record) -> list[Issue]:
    """The lane's tickets, in scope order."""
    return [item for item in of(record) if isinstance(item, Issue)]


def explicit(items: Sequence[Item]) -> bool:
    """Whether the scope needs the `scope` key: anything but no ticket or one ticket the lane completes."""
    return not (len(items) == 0 or (len(items) == 1 and isinstance(items[0], Issue) and not items[0].partial))


def write(record: Record, items: Sequence[Item]) -> None:
    """Set the record's primary `ticket`, and `scope` only when the scope is more than that one ticket."""
    validate(items)
    record["ticket"] = primary(items)
    if explicit(items):
        record["scope"] = [to_json(item) for item in items]
    else:
        record.pop("scope", None)


def as_partial(items: Iterable[Item]) -> tuple[Item, ...]:
    """The scope with every ticket partial: a fix lane's, whose merge refers to its parent's tickets and closes
    none of them again."""
    return tuple(replace(item, partial=True) if isinstance(item, Issue) else item for item in items)


def issue_label(issue: Issue) -> str:
    return issue.ref + (" (partial)" if issue.partial else "")


def describe(items: Sequence[Item]) -> str:
    """One line: `#161, #64 (partial), 1 decision, 1 spec`, or `none`."""
    words = [issue_label(item) for item in items if isinstance(item, Issue)]
    for kind, name in ((Decision, "decision"), (Spec, "spec")):
        count = sum(1 for item in items if isinstance(item, kind))
        if count:
            words.append(f"{count} {name}{'s' if count > 1 else ''}")
    return ", ".join(words) or NONE


def label(item: Item) -> str:
    """`ticket #64 (partial)`, `decision <url>`, `spec <path>`."""
    if isinstance(item, Issue):
        return f"ticket {issue_label(item)}"
    return f"decision {item.url}" if isinstance(item, Decision) else f"spec {item.path}"


def entry_line(item: Item) -> str:
    if isinstance(item, Issue):
        return f"ticket {item.ref} ({'partial: its merge says Refs' if item.partial else 'completes: Fixes'})"
    if isinstance(item, Decision):
        return f"decision {item.url}"
    return f"spec {item.path}"


# The review's reading of the scope (fathers#192).
TEXT_LIMIT = 20_000
GH_TIMEOUT = 60
Status = Literal["ok", "missing", "unreadable", "refused", "truncated"]
READ: tuple[Status, ...] = ("ok", "truncated")
# What gh says of an issue or comment that is not there: deleted, never there, or moved to another repository.
GONE = re.compile(r"could not resolve|not found|\b(?:404|410)\b|deleted|transferred|no longer exists", re.IGNORECASE)


@dataclass(frozen=True)
class Resolved:
    """One scope item as the review read it: `text` exactly as the reviewer sees it, `digest` its sha256, `source`
    a URL or `<path>@<base>`, `version` the issue's or comment's update time or the spec's blob id."""
    item: Item
    status: Status
    title: str = ""
    text: str = ""
    source: str | None = None
    version: str | None = None
    digest: str | None = None
    reason: str | None = None

    @property
    def read(self) -> bool:
        return self.status in READ


def _shown(item: Item, title: str, text: str, source: str | None, version: str | None) -> Resolved:
    """An item read: its text capped at TEXT_LIMIT characters, with a visible note when cut, and the digest of
    exactly that text."""
    status: Status = "ok"
    if len(text) > TEXT_LIMIT:
        status = "truncated"
        text = text[:TEXT_LIMIT] + f"\n\n[truncated at {TEXT_LIMIT} chars]"
    digest = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    return Resolved(item, status, title, text, source, version, digest)


def _gh_error(error: str) -> str:
    return " ".join(error.split())[:300] or "gh failed"


def _resolve_issue(issue: Issue, record: Record, project: config.Config) -> Resolved:
    """The ticket's number, title, body, URL, state and update time; never its comments, which hold the
    implementer's reports and land comments the reviewer must not see."""
    rule = project.tickets
    try:
        data, error = github.issue_view(
            issue.local, json_fields="number,title,body,url,state,updatedAt",
            common=None if issue.repo or rule.repo else record["common_dir"], repo=issue.repo or rule.repo or None,
            project=project,
            timeout=GH_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as failure:
        return Resolved(issue, "unreadable", reason=f"gh could not run: {failure}")
    if data is None:
        status: Status = "missing" if GONE.search(error) else "unreadable"
        return Resolved(issue, status, reason=f"gh could not read {issue.ref}: {_gh_error(error)}")
    number = data.get("number")
    if number is not None and str(number) != issue.local:
        return Resolved(issue, "missing", reason=f"{issue.ref} now reads as #{number}: moved or renumbered")
    body = str(data.get("body") or "").strip() or "(empty body)"
    url = str(data.get("url") or "") or None
    state = str(data.get("state") or "")
    title = str(data.get("title") or "") + (f" [{state.lower()}]" if state and state.upper() != "OPEN" else "")
    return _shown(issue, title, body, url, str(data.get("updatedAt") or "") or None)


def _resolve_decision(decision: Decision, project: config.Config) -> Resolved:
    """The one registered comment: its body, author and update time. A land comment is Lanefather's own report,
    never a decision."""
    from .tickets import LAND_MARKER  # tickets reads scope; the marker is needed only here

    try:
        data, error = github.issue_comment_view(decision.repo, decision.comment, project=project, timeout=GH_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as failure:
        return Resolved(decision, "unreadable", source=decision.url, reason=f"gh could not run: {failure}")
    if data is None:
        status: Status = "missing" if GONE.search(error) else "unreadable"
        return Resolved(decision, status, source=decision.url,
                        reason=f"gh could not read the comment: {_gh_error(error)}")
    issue_url = str(data.get("issue_url") or "").rstrip("/")
    expected_issue = f"/repos/{decision.repo}/issues/{decision.issue}".lower()
    if issue_url and not issue_url.lower().endswith(expected_issue):
        return Resolved(
            decision, "refused", source=decision.url,
            reason=f"the comment belongs to another issue or repository than {decision.repo}#{decision.issue}",
        )
    body = str(data.get("body") or "")
    if LAND_MARKER in body:
        return Resolved(decision, "refused", source=decision.url,
                        reason="a land comment is Lanefather's report of a merge, not a decision")
    raw_user: Any = data.get("user")
    user = cast(dict[str, Any], raw_user) if isinstance(raw_user, dict) else {}
    login = str(user.get("login") or "unknown")
    title = f"comment by @{login} on #{decision.issue}"
    return _shown(decision, title, body.strip() or "(empty comment)", decision.url,
                  str(data.get("updated_at") or "") or None)


def _git_bytes(cwd: str, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True)


def _resolve_spec(spec: Spec, record: Record, base: str | None) -> Resolved:
    """The spec as it stands at the review's base, never at the lane tip or in a worktree: a spec the lane adds or
    rewrites is part of the change under review, not the scope it answers to."""
    if not base:
        return Resolved(spec, "unreadable", reason="no review base to read the spec at")
    source = f"{spec.path}@{base}"
    main = main_checkout(record["common_dir"])
    listed = _git_bytes(main, "--literal-pathspecs", "ls-tree", "-z", "--full-tree", base, "--", spec.path)
    if listed.returncode:
        return Resolved(spec, "unreadable", source=source,
                        reason=f"git could not list {spec.path} at {base[:10]}: "
                               f"{listed.stderr.decode(errors='replace').strip()}")
    entry = next((e for e in listed.stdout.split(b"\0") if e.endswith(b"\t" + spec.path.encode())), None)
    if entry is None:
        return Resolved(spec, "missing", source=source,
                        reason=f"{spec.path} is not in the base {base[:10]}: a spec that is not at the base is not "
                               "accepted scope")
    mode, kind, blob = entry.split(b"\t", 1)[0].decode().split()
    if kind != "blob" or mode == "120000":
        what = {"tree": "a directory", "commit": "a submodule"}.get(kind, "a symbolic link")
        return Resolved(spec, "unreadable", source=source, version=blob,
                        reason=f"{spec.path} is {what} at the base, not a file")
    content = _git_bytes(main, "cat-file", "blob", blob)
    if content.returncode:
        return Resolved(spec, "unreadable", source=source, version=blob,
                        reason=f"git could not read {spec.path}: {content.stderr.decode(errors='replace').strip()}")
    try:
        text = content.stdout.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    if text is None or "\0" in text:
        return Resolved(spec, "unreadable", source=source, version=blob,
                        reason=f"{spec.path} is not UTF-8 text at the base")
    return _shown(spec, spec.path, text.strip() or "(empty file)", source, blob)


def resolve(record: Record, base: str | None, project: config.Config = config.DEFAULT) -> list[Resolved]:
    """Read the lane's scope for its review, in scope order: tickets without comments, decisions by their one
    comment, specs at `base` (the review's merge-base)."""
    found: list[Resolved] = []
    for item in of(record):
        if isinstance(item, Issue):
            found.append(_resolve_issue(item, record, project))
        elif isinstance(item, Decision):
            found.append(_resolve_decision(item, project))
        else:
            found.append(_resolve_spec(item, record, base))
    return found


def complete(resolved: Sequence[Resolved]) -> bool:
    """Whether the reviewer saw every item, whole or truncated."""
    return all(entry.read for entry in resolved)


def receipt_entry(entry: Resolved) -> dict[str, Any]:
    """One item of the review receipt's `scope`."""
    return {"item": to_json(entry.item), "status": entry.status, "source": entry.source, "version": entry.version,
            "digest": entry.digest, **({"reason": entry.reason} if entry.reason else {})}


def heading(entry: Resolved) -> str:
    item = entry.item
    if isinstance(item, Issue):
        what = f"Ticket {item.ref}" + (" (partial: the lane does only part of it)" if item.partial
                                            else " (the lane completes it)")
    elif isinstance(item, Decision):
        what = "Decision"
    else:
        what = "Spec"
    return what + (f": {entry.title}" if entry.title else "")


def _count(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def summary(items: Sequence[Item]) -> str:
    """`3 items: 2 tickets (1 partial), 1 decision`."""
    tickets = [item for item in items if isinstance(item, Issue)]
    partial = sum(1 for item in tickets if item.partial)
    parts = [_count(len(tickets), "ticket") + (f" ({partial} partial)" if partial else "")] if tickets else []
    for kind, word in ((Decision, "decision"), (Spec, "spec")):
        count = sum(1 for item in items if isinstance(item, kind))
        if count:
            parts.append(_count(count, word))
    return f"{_count(len(items), 'item')}: {', '.join(parts)}"


def prompt_text(resolved: Sequence[Resolved]) -> str:
    """What the change is for, as the reviewer reads it: every scope item's text with its source, version and
    digest, and the items that could not be read under `Unavailable scope`."""
    if not resolved:
        return "The lane has no ticket."
    items = [entry.item for entry in resolved]
    lines = [f"The lane's accepted scope, {summary(items)}. Review the change against all of it."]
    if any(isinstance(item, Issue) and item.partial for item in items):
        lines.append("A partial ticket is done only in part by this lane: review the part the change does, and do "
                     "not report the ticket's other parts as missing.")
    if any(isinstance(item, Decision) for item in items):
        lines.append("A decision is an owner's or coordinator's comment; where it settles what a ticket leaves "
                     "open, it holds.")
    if any(isinstance(item, Spec) for item in items):
        lines.append("A spec is shown as it stands at the review's base; a change the lane makes to it is part of "
                     "the diff under review.")
    for number, entry in enumerate(resolved, 1):
        if not entry.read:
            continue
        details = [f"Source: {entry.source or '-'}"]
        if entry.version:
            details.append(f"updated {entry.version}" if not isinstance(entry.item, Spec)
                           else f"blob {entry.version}")
        details.append(f"digest {entry.digest}")
        lines += ["", f"### {number}. {heading(entry)}", "", "; ".join(details), "", entry.text]
    unavailable = [(number, entry) for number, entry in enumerate(resolved, 1) if not entry.read]
    if unavailable:
        lines += ["", "### Unavailable scope", "",
                  "The review could not read the scope below. Report one P3 finding, with no file, that "
                  "names each of these items and says the review did not see it; do not guess what it says.", ""]
        lines += [f"- {number}. {label(entry.item)}: {entry.status} ({entry.reason})"
                  for number, entry in unavailable]
    return "\n".join(lines)
