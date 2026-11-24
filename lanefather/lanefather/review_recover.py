"""Recover historical review evidence from GitHub issue comments without rerunning completed reviews (fathers#179).

Fetches explicit `--source URL` comments (or discovers review comments on linked tickets via `github.py`), parses
historical dispositions (`fixed: <sha>`, `rejected: <reason>`, unmarked findings, or standalone clean-review
statements), verifies git ancestry of named fix commits against the target ref and checks reviewed/landed scope,
linked ticket identity, and creation timeline against the lane record (guarding against lane ID reuse).

Strict truthfulness invariants:
- Never synthesizes an executed review or invents missing severity, model, reviewer, or tree metadata.
- Lists any unverifiable facts explicitly in `missing_facts`.
- Preserves any prior interrupted/partial/replaced review receipt under `preserved_artifacts` instead of destroying it.
- Partial or unverified evidence is recorded visibly as incomplete (`complete: False`) and never satisfies the review
  gate or `done`.
- Repeated recovery is idempotent and never downgrades complete recovered evidence without `--force-reason`.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import datetime
import os
import re
from typing import Any, cast

from . import events, github, journal, lifecycle, review_record, scope, state, tickets
from .errors import LaneError
from .gitops import git, git_ok, main_checkout
from .state import Record

COMMENT_URL_RE = re.compile(
    r"^https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/(?:issues|pull)/(\d+)#issuecomment-(\d+)$"
)
RANGE_RE = re.compile(
    r"(?:reviewed(?:\s+range)?\s*[:=]\s*|review\s+of\s+[A-Za-z0-9_.-]+\s*\(\s*|\(\s*)"
    r"`?([0-9a-f]{7,64})\.{2,3}([0-9a-f]{7,64})`?",
    re.IGNORECASE,
)
MERGE_REF_RE = re.compile(
    r"(?:landed\s+merge|merge\s+commit|landed|merge)\s*[:=]\s*`?([0-9a-f]{7,64})`?",
    re.IGNORECASE,
)
LANE_MENTION_RE = re.compile(
    r"(?:^|\b)(?:review\s+of\s+lane\s+|review\s+of\s+|lane\s*[:=]\s*)`?([A-Za-z0-9][A-Za-z0-9_-]{0,63})`?",
    re.IGNORECASE | re.MULTILINE,
)
_LANE_STOPWORDS = frozenset({
    "lane",
    "review",
    "findings",
    "finding",
    "clean",
    "the",
    "a",
    "an",
    "this",
    "that",
    "pr",
    "pull",
    "issue",
    "commit",
    "branch",
    "merge",
    "diff",
    "code",
    "changes",
})
MODEL_RE = re.compile(r"(?:^|\b)model\s*[:=]\s*`?([A-Za-z0-9_.:/-]+)`?", re.IGNORECASE | re.MULTILINE)
REVIEWER_RE = re.compile(r"(?:^|\b)reviewer\s*[:=]\s*`?([A-Za-z0-9_.:+-]+)`?", re.IGNORECASE | re.MULTILINE)
CLEAN_REVIEW_RE = re.compile(
    r"^\s*(?:review(?:\s+of\s+[A-Za-z0-9_.-]+(?:\s*\([^)]*\))?)?\s*:\s*)?"
    r"(?:0\s+findings|no\s+findings|clean\s+review|review\s+clean|no\s+issues\s+found)\.?\s*$",
    re.IGNORECASE | re.MULTILINE,
)
UNPARSED_FINDING_RE = re.compile(r"^\s*[-*]\s*\*?\*?P[0-3]\*?\*?\b", re.IGNORECASE)
NUMBERED_ITEM_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:finding\s*#?(\d+)|#(\d+)|(\d+)\.)\s*[:\-–—]?\s*(.+)$",
    re.IGNORECASE,
)
SEVERITY_PREFIX_RE = re.compile(r"^\s*\*?\*?(P[0-3])\*?\*?(?:\s+\[[^\]]+\])?\s+(.*)$")
FILE_LINE_PREFIX_RE = re.compile(r"^`?([A-Za-z0-9_./-]+\.[A-Za-z0-9_-]+)(?::(\d+))?`?\s+(.+)$")
FIXED_RE = re.compile(
    r"(?:\[\s*)?fixed\s*[:=]\s*`?([0-9a-f]{7,64})`?(?:\s*\])?",
    re.IGNORECASE,
)
REJECTED_RE = re.compile(
    r"(?:\[\s*)?rejected\s*[:=]\s*([^\]]+?)(?:\s*\])?$",
    re.IGNORECASE,
)
REJECTED_BRACKET_RE = re.compile(
    r"\[\s*rejected\s*[:=]\s*([^\]]+)\s*\]",
    re.IGNORECASE,
)
UNMARKED_HINT_RE = re.compile(r"\[\s*(?:unmarked|open|unresolved|pending)\s*\]", re.IGNORECASE)


@dataclass(frozen=True)
class SourceComment:
    """One fetched GitHub issue comment."""

    url: str
    body: str
    repo: str | None = None
    issue_number: str | None = None
    created_at: str | None = None
    author: str | None = None


@dataclass(frozen=True)
class ParsedEvidence:
    """Structured historical review data extracted strictly from source comments."""

    dispositions: tuple[dict[str, Any], ...]
    clean_statement: bool
    unparsed_finding_lines: tuple[str, ...]
    reviewed_base: str | None
    reviewed_tip: str | None
    mentioned_merge: str | None
    mentioned_lanes: tuple[str, ...]
    model: str | None
    reviewer: str | None


def parse_comment_url(url: str) -> tuple[str, str, str]:
    """Parse a GitHub issue/PR comment URL into `(repo, issue_number, comment_id)`."""
    cleaned = url.strip()
    match = COMMENT_URL_RE.fullmatch(cleaned)
    if not match:
        raise LaneError(
            f"--source {url!r}: give a GitHub issue comment URL, "
            "https://github.com/<owner>/<repo>/issues/<n>#issuecomment-<id>"
        )
    return match.group(1), match.group(2), match.group(3)


def _actor_label() -> str:
    who = events.actor()
    if who.get("type") == "agent" and isinstance(who.get("name"), str):
        session = who.get("session")
        return f"{who['name']}:{session}" if isinstance(session, str) and session else str(who["name"])
    user = os.environ.get("USER", "").strip()
    if user:
        return user
    return f"pid:{os.getpid()}"


def _looks_like_review_comment(body: str) -> bool:
    if not body or body.lstrip().startswith(tickets.LAND_MARKER):
        return False
    if FIXED_RE.search(body) or REJECTED_BRACKET_RE.search(body) or CLEAN_REVIEW_RE.search(body):
        return True
    lower = body.lower()
    return "review" in lower and (
        "rejected:" in lower
        or "fixed:" in lower
        or any(NUMBERED_ITEM_RE.match(line) for line in body.splitlines())
    )


def fetch_sources(
    urls: Sequence[str],
    store: state.Store,
) -> tuple[list[SourceComment], list[dict[str, str]]]:
    """Fetch explicit `--source` comment URLs via `github.issue_comment_view`."""
    fetched: list[SourceComment] = []
    unavailable: list[dict[str, str]] = []
    for url in dict.fromkeys(u.strip() for u in urls if u.strip()):
        repo, issue_num, comment_id = parse_comment_url(url)
        data, err = github.issue_comment_view(repo, comment_id, project=store.project)
        if data is None:
            unavailable.append({"url": url, "error": err or "comment unavailable"})
            continue
        raw_body = data.get("body")
        body = raw_body if isinstance(raw_body, str) else ""
        raw_created = data.get("created_at") or data.get("createdAt")
        created_at = raw_created if isinstance(raw_created, str) and raw_created else None
        raw_user: Any = data.get("user") or data.get("author")
        author = (
            str(cast(dict[str, Any], raw_user).get("login"))
            if isinstance(raw_user, dict) and cast(dict[str, Any], raw_user).get("login")
            else None
        )
        fetched.append(
            SourceComment(
                url=url,
                body=body,
                repo=repo,
                issue_number=issue_num,
                created_at=created_at,
                author=author,
            )
        )
    return fetched, unavailable


def discover_sources(
    record: Record,
    store: state.Store,
) -> tuple[list[SourceComment], list[dict[str, str]]]:
    """Discover historical review comments from the lane's linked ticket(s) via `github.issue_view`."""
    linked = scope.issues(record)
    if not linked:
        raise LaneError(
            f"review --recover refused: lane {record['id']} has no linked ticket; pass --source <comment-url>"
        )
    lane_id = str(record["id"])
    rule = store.project.tickets
    common = str(record.get("common_dir") or "")
    fetched: list[SourceComment] = []
    unavailable: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for issue in linked:
        repo = issue.repo or rule.repo or (github.repository(common, rule) if common else None)
        data, err = github.issue_view(
            issue.local,
            json_fields="comments,url",
            common=None if repo else (common or None),
            repo=repo,
            project=store.project,
        )
        if data is None:
            unavailable.append({
                "url": f"issue:{issue.ref}",
                "error": err or f"could not read {issue.ref}",
            })
            continue
        raw_issue_url = data.get("url")
        issue_url = raw_issue_url if isinstance(raw_issue_url, str) and raw_issue_url else ""
        raw_comments: Any = data.get("comments")
        if not isinstance(raw_comments, list):
            continue
        for raw_comment in cast(list[Any], raw_comments):
            if not isinstance(raw_comment, dict):
                continue
            comment = cast(dict[str, Any], raw_comment)
            raw_body = comment.get("body")
            body = raw_body if isinstance(raw_body, str) else ""
            if not _looks_like_review_comment(body):
                continue
            mentioned = _extract_mentioned_lanes(body)
            if mentioned and lane_id not in mentioned:
                continue
            raw_url = comment.get("url")
            if isinstance(raw_url, str) and raw_url:
                comment_url = raw_url
            else:
                cid = comment.get("databaseId") or comment.get("id")
                if issue_url and cid:
                    comment_url = f"{issue_url}#issuecomment-{cid}"
                elif repo and cid:
                    comment_url = f"https://github.com/{repo}/issues/{issue.local}#issuecomment-{cid}"
                else:
                    comment_url = f"issue:{issue.ref}#comment"
            if comment_url in seen_urls:
                continue
            seen_urls.add(comment_url)
            raw_created = comment.get("createdAt") or comment.get("created_at")
            created_at = raw_created if isinstance(raw_created, str) and raw_created else None
            raw_author: Any = comment.get("author") or comment.get("user")
            author = (
                str(cast(dict[str, Any], raw_author).get("login"))
                if isinstance(raw_author, dict) and cast(dict[str, Any], raw_author).get("login")
                else None
            )
            fetched.append(
                SourceComment(
                    url=comment_url,
                    body=body,
                    repo=repo,
                    issue_number=issue.local,
                    created_at=created_at,
                    author=author,
                )
            )
    return fetched, unavailable


def _extract_mentioned_lanes(body: str) -> list[str]:
    found: list[str] = []
    for match in LANE_MENTION_RE.finditer(body):
        candidate = match.group(1)
        if candidate.lower() in _LANE_STOPWORDS:
            continue
        if candidate not in found:
            found.append(candidate)
    return found


def _parse_line_disposition(
    line: str,
    *,
    source_url: str,
    next_auto_n: int,
) -> tuple[dict[str, Any] | None, int]:
    """Parse one line into a disposition dict if it describes a finding or disposition."""
    stripped = line.strip()
    if not stripped:
        return None, next_auto_n
    numbered = NUMBERED_ITEM_RE.match(stripped)
    explicit_n: int | None = None
    rest = stripped
    if numbered:
        explicit_n = int(numbered.group(1) or numbered.group(2) or numbered.group(3))
        rest = numbered.group(4).strip()
    elif not (FIXED_RE.search(stripped) or REJECTED_BRACKET_RE.search(stripped) or REJECTED_RE.search(stripped)):
        return None, next_auto_n

    fixed_match = FIXED_RE.search(rest)
    rej_bracket = REJECTED_BRACKET_RE.search(rest)
    rej_tail = None if rej_bracket else REJECTED_RE.search(rest)

    kind: str | None = None
    raw_commit: str | None = None
    reason: str | None = None
    body_text = rest

    if fixed_match:
        kind = "fixed"
        raw_commit = fixed_match.group(1).lower()
        body_text = (rest[: fixed_match.start()] + " " + rest[fixed_match.end() :]).strip()
    elif rej_bracket:
        kind = "rejected"
        reason = rej_bracket.group(1).strip()
        body_text = (rest[: rej_bracket.start()] + " " + rest[rej_bracket.end() :]).strip()
    elif rej_tail:
        kind = "rejected"
        reason = rej_tail.group(1).strip()
        body_text = rest[: rej_tail.start()].strip()
    elif numbered:
        kind = "unmarked"
        body_text = UNMARKED_HINT_RE.sub("", rest).strip()
    else:
        return None, next_auto_n

    body_text = re.sub(r"[\s\-–—:(]+$", "", body_text).strip()
    body_text = re.sub(r"^[\s\-–—:]+", "", body_text).strip()

    # Extract optional explicit severity (NEVER invent one!).
    severity: str | None = None
    sev_match = SEVERITY_PREFIX_RE.match(body_text)
    if sev_match:
        severity = sev_match.group(1)
        body_text = sev_match.group(2).strip()

    file_path: str | None = None
    line_no: int | None = None
    file_match = FILE_LINE_PREFIX_RE.match(body_text)
    if file_match:
        file_path = file_match.group(1)
        line_no = int(file_match.group(2)) if file_match.group(2) else None
        body_text = file_match.group(3).strip()

    n = explicit_n if explicit_n is not None else next_auto_n
    entry: dict[str, Any] = {
        "n": n,
        "kind": kind,
        "severity": severity,
        "title": body_text or f"historical finding #{n}",
        "file": file_path,
        "line": line_no,
        "source": source_url,
    }
    if kind == "fixed":
        entry["raw_commit"] = raw_commit
        entry["commit"] = raw_commit
        entry["in_target_history"] = False
        entry["verified"] = False
    elif kind == "rejected":
        entry["reason"] = reason or ""
        entry["verified"] = bool(reason)
    else:
        entry["verified"] = False
    return entry, max(next_auto_n, n + 1)


def parse_historical_evidence(comments: Sequence[SourceComment]) -> ParsedEvidence:
    """Parse dispositions and explicit scope/metadata references from fetched comments."""
    dispositions: list[dict[str, Any]] = []
    seen_numbers: dict[int, int] = {}
    unparsed_finding_lines: list[str] = []
    next_n = 1
    clean_statement = False
    reviewed_base: str | None = None
    reviewed_tip: str | None = None
    mentioned_merge: str | None = None
    mentioned_lanes: list[str] = []
    model: str | None = None
    reviewer_name: str | None = None

    for comment in comments:
        body = comment.body
        if CLEAN_REVIEW_RE.search(body):
            clean_statement = True
        if reviewed_base is None or reviewed_tip is None:
            range_match = RANGE_RE.search(body)
            if range_match:
                reviewed_base = range_match.group(1).lower()
                reviewed_tip = range_match.group(2).lower()
        if mentioned_merge is None:
            merge_match = MERGE_REF_RE.search(body)
            if merge_match:
                mentioned_merge = merge_match.group(1).lower()
        for lane_name in _extract_mentioned_lanes(body):
            if lane_name not in mentioned_lanes:
                mentioned_lanes.append(lane_name)
        if model is None:
            model_match = MODEL_RE.search(body)
            if model_match:
                model = model_match.group(1)
        if reviewer_name is None:
            rev_match = REVIEWER_RE.search(body)
            if rev_match:
                reviewer_name = rev_match.group(1)

        for raw_line in body.splitlines():
            parsed, next_n = _parse_line_disposition(raw_line, source_url=comment.url, next_auto_n=next_n)
            if parsed is None:
                if UNPARSED_FINDING_RE.match(raw_line):
                    unparsed_finding_lines.append(raw_line.strip())
                continue
            num = int(parsed["n"])
            if num in seen_numbers:
                prev_idx = seen_numbers[num]
                prev = dispositions[prev_idx]
                if prev.get("kind") == "unmarked" and parsed.get("kind") in ("fixed", "rejected"):
                    merged = {**prev, **parsed}
                    if not parsed.get("severity") and prev.get("severity"):
                        merged["severity"] = prev["severity"]
                    if not parsed.get("file") and prev.get("file"):
                        merged["file"] = prev["file"]
                        merged["line"] = prev.get("line")
                    if parsed.get("title", "").startswith("historical finding #") and prev.get("title"):
                        merged["title"] = prev["title"]
                    dispositions[prev_idx] = merged
                else:
                    parsed["n"] = next_n
                    seen_numbers[next_n] = len(dispositions)
                    dispositions.append(parsed)
                    next_n += 1
            else:
                seen_numbers[num] = len(dispositions)
                dispositions.append(parsed)

    if unparsed_finding_lines:
        clean_statement = False

    return ParsedEvidence(
        dispositions=tuple(dispositions),
        clean_statement=clean_statement,
        unparsed_finding_lines=tuple(unparsed_finding_lines),
        reviewed_base=reviewed_base,
        reviewed_tip=reviewed_tip,
        mentioned_merge=mentioned_merge,
        mentioned_lanes=tuple(mentioned_lanes),
        model=model,
        reviewer=reviewer_name,
    )


def _resolve_commit(main: str, ref: str) -> str:
    return git(main, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}", check=False)


def _resolve_target_sha(main: str, record: Record, target_ref: str | None) -> str:
    """Resolve the target commit SHA to check fix-commit ancestry against."""
    if target_ref:
        sha = _resolve_commit(main, target_ref)
        if not sha:
            raise LaneError(f"--target {target_ref!r}: not a commit in {main}")
        return sha
    raw_landed: Any = record.get("landed")
    landed = cast(dict[str, Any], raw_landed) if isinstance(raw_landed, dict) else {}
    integration = landed.get("integration") or record.get("integration")
    if isinstance(integration, str) and integration and os.path.isdir(integration):
        head = git(integration, "rev-parse", "--verify", "--quiet", "HEAD^{commit}", check=False)
        if head:
            return head
    base = record.get("base")
    if isinstance(base, str) and base:
        base_head = git(main, "rev-parse", "--verify", "--quiet", f"refs/heads/{base}^{{commit}}", check=False)
        if base_head:
            return base_head
    head = git(main, "rev-parse", "--verify", "--quiet", "HEAD^{commit}", check=False)
    if head:
        return head
    landed_merge = landed.get("merge")
    if isinstance(landed_merge, str) and landed_merge:
        return landed_merge
    raise LaneError(f"review --recover: could not determine target commit in {main}; pass --target <ref>")


def _sha_matches(candidate: str, full_sha: str) -> bool:
    c = candidate.strip().lower()
    f = full_sha.strip().lower()
    return bool(c and f) and (f == c or f.startswith(c) or c.startswith(f))


def _parse_and_resolve_range_arg(main: str, range_arg: str) -> tuple[str, str]:
    cleaned = range_arg.strip()
    if "..." in cleaned:
        left, _, right = cleaned.partition("...")
    elif ".." in cleaned:
        left, _, right = cleaned.partition("..")
    else:
        raise LaneError(f"--reviewed-range {range_arg!r}: expected BASE..TIP")
    left, right = left.strip(), right.strip()
    if not left or not right:
        raise LaneError(f"--reviewed-range {range_arg!r}: expected BASE..TIP")
    resolved_base = _resolve_commit(main, left)
    if not resolved_base:
        raise LaneError(f"--reviewed-range {range_arg!r}: base {left!r} is not a commit in {main}")
    resolved_tip = _resolve_commit(main, right)
    if not resolved_tip:
        raise LaneError(f"--reviewed-range {range_arg!r}: tip {right!r} is not a commit in {main}")
    return resolved_base, resolved_tip


def _artifact_kind(existing: review_record.ReviewRecord) -> str:
    if existing.is_recovered:
        return "recovered_review"
    verdict = existing.verdict
    if isinstance(verdict, review_record.Died):
        return "died_review"
    if isinstance(verdict, review_record.Failed):
        return "failed_review"
    if isinstance(verdict, review_record.PartialPanel):
        return "partial_panel_review"
    if isinstance(verdict, review_record.Running):
        return "running_review"
    return "executed_review"


def _snapshot_existing_artifact(
    existing: review_record.ReviewRecord,
    *,
    preserved_at: str,
) -> dict[str, Any]:
    raw = dict(existing.raw or {})
    files: list[str] = []
    for key in ("output", "log"):
        val = raw.get(key)
        if isinstance(val, str) and val and os.path.exists(val) and val not in files:
            files.append(val)
    return {
        "kind": _artifact_kind(existing),
        "phase": existing.phase.value,
        "preserved_at": preserved_at,
        "files": files,
        "receipt": raw,
    }


def _semantic_recovery_key(payload: dict[str, Any]) -> dict[str, Any]:
    """Fields that determine whether repeated recovery produced identical evidence (for idempotency)."""
    return {
        "complete": payload.get("complete"),
        "incomplete_reasons": payload.get("incomplete_reasons"),
        "sources": payload.get("sources"),
        "unavailable_sources": payload.get("unavailable_sources"),
        "target": payload.get("target"),
        "reviewed_scope": payload.get("reviewed_scope"),
        "landed_scope": payload.get("landed_scope"),
        "dispositions": payload.get("dispositions"),
        "missing_facts": payload.get("missing_facts"),
        "model": payload.get("model"),
        "reviewer": payload.get("reviewer"),
    }


def _parse_iso_utc(raw: str | None) -> datetime.datetime | None:
    if not raw:
        return None
    try:
        parsed = journal.parse_time(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=datetime.UTC)
    return parsed.astimezone(datetime.UTC)


def _source_matches_linked_tickets(
    comment: SourceComment,
    linked: Sequence[scope.Issue],
    default_repo: str | None,
) -> bool:
    if not linked or not comment.issue_number:
        return True
    comment_repo = comment.repo.lower() if comment.repo else None
    for issue in linked:
        if issue.local != comment.issue_number:
            continue
        expected_repo = (issue.repo or default_repo or "").lower()
        if not expected_repo or not comment_repo or expected_repo == comment_repo:
            return True
    return False


def recover_lane(
    store: state.Store,
    record: Record,
    *,
    sources: Sequence[str] = (),
    target: str | None = None,
    reviewed_range: str | None = None,
    force_reason: str | None = None,
    audit_override: Callable[[str], None] | None = None,
) -> tuple[review_record.ReviewRecord, bool]:
    """Recover historical review evidence for `record`, verify git ancestry & scope, and persist via `ReviewRecord`."""
    lane_id = str(record["id"])
    prog = store.prog
    raw_landed: Any = record.get("landed")
    landed_info = cast(dict[str, Any], raw_landed) if isinstance(raw_landed, dict) else {}
    landed_merge = str(landed_info.get("merge") or "")
    if not lifecycle.landed(record) or not landed_merge:
        raise LaneError(
            f"review --recover refused: lane {lane_id} is {lifecycle.display_state(record)}, not landed"
        )
    common = record.get("common_dir")
    if not isinstance(common, str) or not common or not os.path.isdir(common):
        raise LaneError(f"review --recover refused: lane {lane_id} names no repository that exists")
    main = main_checkout(common)

    existing = review_record.ReviewRecord.load(store, lane_id, record=record)
    existing_raw = existing.raw
    existing_is_recovered = existing.is_recovered

    if not existing_is_recovered:
        if isinstance(existing.verdict, review_record.Running) and not force_reason:
            raise LaneError(
                f"review --recover refused: lane {lane_id} has a running review (pid {existing.verdict.pid}); "
                "wait for it to finish or pass --force-reason"
            )
        if isinstance(
            existing.verdict,
            (review_record.Complete, review_record.Unmarked, review_record.Exempt, review_record.Stale),
        ) and not force_reason:
            raise LaneError(
                f"review --recover refused: lane {lane_id} already has an executed review receipt "
                f"({existing.summary(prog=prog)[1]})"
            )

    # Validate explicit arguments upfront before network calls.
    explicit_range: tuple[str, str] | None = (
        _parse_and_resolve_range_arg(main, reviewed_range) if reviewed_range else None
    )
    target_sha = _resolve_target_sha(main, record, target)

    if sources:
        fetched, unavailable = fetch_sources(sources, store)
        source_urls = list(dict.fromkeys(u.strip() for u in sources if u.strip()))
    else:
        fetched, unavailable = discover_sources(record, store)
        source_urls = [c.url for c in fetched]

    parsed = parse_historical_evidence(fetched)

    incomplete_reasons: list[str] = []
    for item in unavailable:
        incomplete_reasons.append(f"source unavailable ({item['url']}): {item['error']}")

    if not fetched and not unavailable:
        incomplete_reasons.append("no historical review comments found on linked ticket(s)")

    if parsed.unparsed_finding_lines:
        incomplete_reasons.append(
            f"source contains {len(parsed.unparsed_finding_lines)} unparsed finding-like line(s)"
        )

    # Check source ticket & timeline binding against lane record.
    linked_issues = scope.issues(record)
    default_repo = github.repository(common, store.project.tickets)
    lane_created_dt = _parse_iso_utc(state.lane_created_of(record))
    for comment in fetched:
        if linked_issues and not _source_matches_linked_tickets(comment, linked_issues, default_repo):
            ref = f"{comment.repo}#{comment.issue_number}" if comment.repo else f"#{comment.issue_number}"
            incomplete_reasons.append(
                f"scope mismatch: source {comment.url} is on {ref}, not the lane's linked ticket(s)"
            )
        comment_dt = _parse_iso_utc(comment.created_at)
        if lane_created_dt is not None and comment_dt is not None and comment_dt < lane_created_dt:
            incomplete_reasons.append(
                f"scope mismatch: source {comment.url} predates lane creation "
                f"({comment.created_at} < {record.get('created')})"
            )

    landed_tip = str(landed_info.get("tip") or "")
    landed_prev = str(landed_info.get("previous") or "")
    landed_tree = str(landed_info.get("tree") or "")
    landed_scope: dict[str, Any] = {
        k: v
        for k, v in (
            ("merge", landed_merge),
            ("tip", landed_tip),
            ("previous", landed_prev),
            ("tree", landed_tree),
        )
        if v
    }

    # Verify target holds the lane's landed merge.
    if not git_ok(main, "merge-base", "--is-ancestor", landed_merge, target_sha):
        incomplete_reasons.append(
            f"scope mismatch: lane's landed merge {landed_merge[:10]} is not in target {target_sha[:10]}"
        )

    # Verify lane ID mention if comments explicitly name another lane and not this lane.
    if parsed.mentioned_lanes and lane_id not in parsed.mentioned_lanes:
        incomplete_reasons.append(
            f"scope mismatch: source comment(s) name lane {', '.join(parsed.mentioned_lanes)}, not {lane_id}"
        )

    # Verify merge SHA mentioned in comment against landed merge.
    verified_merge_match = False
    if parsed.mentioned_merge:
        resolved_mentioned_merge = _resolve_commit(main, parsed.mentioned_merge)
        if resolved_mentioned_merge:
            if resolved_mentioned_merge == landed_merge:
                verified_merge_match = True
            else:
                incomplete_reasons.append(
                    f"scope mismatch: source names merge {resolved_mentioned_merge[:10]}, "
                    f"lane landed {landed_merge[:10]}"
                )
        elif _sha_matches(parsed.mentioned_merge, landed_merge):
            verified_merge_match = True
        else:
            incomplete_reasons.append(
                f"scope mismatch: source names merge {parsed.mentioned_merge[:10]}, "
                f"lane landed {landed_merge[:10]}"
            )

    # Build and verify reviewed_scope (from --reviewed-range or parsed comment range).
    reviewed_scope: dict[str, Any] | None = None
    raw_base_ref = explicit_range[0] if explicit_range else parsed.reviewed_base
    raw_tip_ref = explicit_range[1] if explicit_range else parsed.reviewed_tip
    if raw_base_ref and raw_tip_ref:
        resolved_base = _resolve_commit(main, raw_base_ref)
        resolved_tip = _resolve_commit(main, raw_tip_ref)
        reviewed_scope = {
            "base": resolved_base or raw_base_ref,
            "tip": resolved_tip or raw_tip_ref,
            "verified": False,
        }
        if not resolved_base or not resolved_tip:
            incomplete_reasons.append(
                f"scope mismatch: reviewed range {raw_base_ref[:10]}..{raw_tip_ref[:10]} is not in git history"
            )
        else:
            range_ok = True
            if landed_tip:
                tip_matches = resolved_tip in {landed_tip, landed_merge}
                if not tip_matches:
                    range_ok = False
                    incomplete_reasons.append(
                        f"scope mismatch: reviewed tip {resolved_tip[:10]} does not match "
                        f"landed tip {landed_tip[:10]}"
                    )
            if landed_prev and git_ok(main, "merge-base", "--is-ancestor", resolved_tip, landed_prev):
                range_ok = False
                incomplete_reasons.append(
                    f"scope mismatch: reviewed tip {resolved_tip[:10]} predates lane's landed base "
                    f"{landed_prev[:10]} (possible lane ID reuse)"
                )
            if not git_ok(main, "merge-base", "--is-ancestor", resolved_base, resolved_tip):
                range_ok = False
                incomplete_reasons.append(
                    f"scope mismatch: reviewed base {resolved_base[:10]} is not an ancestor of "
                    f"reviewed tip {resolved_tip[:10]}"
                )
            reviewed_scope["verified"] = range_ok

    # Verify each disposition and its fix commit ancestry.
    verified_dispositions: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    now = journal.now()
    for disp in parsed.dispositions:
        entry = dict(disp)
        kind = entry.get("kind")
        n = entry.get("n")
        if kind == "fixed":
            raw_commit = str(entry.get("raw_commit") or "")
            resolved_fix = _resolve_commit(main, raw_commit) if raw_commit else ""
            if not resolved_fix:
                entry["in_target_history"] = False
                entry["verified"] = False
                incomplete_reasons.append(
                    f"finding #{n} fix commit {raw_commit[:10] or '(none)'} does not exist in git"
                )
            elif not git_ok(main, "merge-base", "--is-ancestor", resolved_fix, target_sha):
                entry["commit"] = resolved_fix
                entry["in_target_history"] = False
                entry["verified"] = False
                incomplete_reasons.append(
                    f"finding #{n} fix commit {resolved_fix[:10]} is not in target history {target_sha[:10]}"
                )
            elif landed_prev and git_ok(main, "merge-base", "--is-ancestor", resolved_fix, landed_prev):
                entry["commit"] = resolved_fix
                entry["in_target_history"] = True
                entry["verified"] = False
                incomplete_reasons.append(
                    f"scope mismatch: finding #{n} fix commit {resolved_fix[:10]} predates lane's landed base "
                    f"{landed_prev[:10]} (possible lane ID reuse)"
                )
            else:
                entry["commit"] = resolved_fix
                entry["in_target_history"] = True
                entry["verified"] = True
        elif kind == "rejected":
            reason = str(entry.get("reason") or "").strip()
            entry["verified"] = bool(reason)
            if not reason:
                incomplete_reasons.append(f"finding #{n} rejection has no reason")
        else:
            entry["verified"] = False
            incomplete_reasons.append(f"finding #{n} has no verified disposition")

        verified_dispositions.append(entry)

        mark: dict[str, Any] | None = None
        if kind == "fixed" and entry.get("verified") is True:
            mark = {
                "kind": "fixed",
                "merge": entry["commit"],
                "at": now,
                "recovered": True,
                "source": entry.get("source"),
            }
        elif kind == "rejected" and entry.get("verified") is True:
            mark = {
                "kind": "rejected",
                "reason": entry.get("reason", ""),
                "at": now,
                "recovered": True,
                "source": entry.get("source"),
            }
        findings.append({
            "n": n,
            "severity": entry.get("severity"),
            "title": entry.get("title", ""),
            "file": entry.get("file"),
            "line": entry.get("line"),
            "mark": mark,
        })

    if not verified_dispositions and not parsed.clean_statement and fetched:
        incomplete_reasons.append("no review dispositions or clean-review statement found in source(s)")

    # Require verified scope binding to the lane (reviewed_scope.verified or verified merge matching landed.merge).
    scope_tied = (reviewed_scope is not None and reviewed_scope.get("verified") is True) or verified_merge_match
    if not scope_tied and (verified_dispositions or parsed.clean_statement):
        incomplete_reasons.append("reviewed scope unverified")

    incomplete_reasons = list(dict.fromkeys(incomplete_reasons))
    is_complete = len(incomplete_reasons) == 0 and (bool(verified_dispositions) or parsed.clean_statement)

    # Truthfully record missing_facts — NEVER fabricate missing metadata!
    missing_facts: list[str] = []
    if not parsed.model:
        missing_facts.append("model")
    if not parsed.reviewer:
        missing_facts.append("reviewer")
    if any(d.get("severity") is None for d in verified_dispositions):
        missing_facts.append("severity")
    missing_facts.append("tree")
    if reviewed_scope is None:
        missing_facts.append("reviewed_scope")

    # Guard against downgrading an already-complete recovered receipt without --force-reason.
    existing_was_complete_recovered = (
        existing_is_recovered and isinstance(existing.verdict, review_record.Complete) and existing_raw is not None
    )
    prev_sources = (
        [str(s) for s in cast(list[Any], existing_raw.get("sources")) if isinstance(s, str) and s]
        if existing_was_complete_recovered and existing_raw and isinstance(existing_raw.get("sources"), list)
        else []
    )
    fetched_urls = [c.url for c in fetched]
    downgrading_recovered = existing_was_complete_recovered and (
        not is_complete or len(fetched_urls) < len(prev_sources)
    )
    if downgrading_recovered and not force_reason:
        why = (
            "would replace complete recovered evidence with partial evidence"
            if not is_complete
            else "would reduce verified sources on complete recovered evidence"
        )
        raise LaneError(
            f"review --recover refused: lane {lane_id} already has complete recovered evidence and this run "
            f"{why} (pass --force-reason to replace and preserve the previous record)"
        )

    # Preserve prior interrupted/partial/replaced artifacts; keep existing preserved_artifacts on idempotent rerun.
    preserved_artifacts: list[dict[str, Any]] = []
    if existing_raw is not None:
        raw_preserved: Any = existing_raw.get("preserved_artifacts")
        if isinstance(raw_preserved, list):
            preserved_artifacts = [
                dict(cast(dict[str, Any], item))
                for item in cast(list[Any], raw_preserved)
                if isinstance(item, dict)
            ]
        if (not existing_is_recovered and existing.phase is not review_record.ReceiptPhase.ABSENT) or (
            downgrading_recovered and force_reason
        ):
            preserved_artifacts.append(_snapshot_existing_artifact(existing, preserved_at=now))

    actor = _actor_label()
    created = state.lane_created_of(record)
    payload: dict[str, Any] = {
        "kind": "lane",
        "lane": lane_id,
        "ticket": record["ticket"],
        **({"lane_created": created} if created else {}),
        "recovered": True,
        "evidence_source": "historical",
        "complete": is_complete,
        "incomplete_reasons": incomplete_reasons,
        "sources": source_urls,
        "unavailable_sources": unavailable,
        "target": target_sha,
        "reviewed_scope": reviewed_scope,
        "landed_scope": landed_scope or None,
        "dispositions": verified_dispositions,
        "findings": findings,
        "missing_facts": missing_facts,
        "model": parsed.model,
        "reviewer": parsed.reviewer,
        "recovered_by": actor,
        "recovered_at": now,
        "finished": now,
        "preserved_artifacts": preserved_artifacts,
    }

    # Idempotency: if already recovered with identical semantic content, keep original timestamps & avoid churn.
    if (
        existing_is_recovered
        and existing_raw is not None
        and _semantic_recovery_key(existing_raw) == _semantic_recovery_key(payload)
    ):
        return existing, is_complete

    if force_reason and audit_override is not None and (
        (not existing_is_recovered and existing.phase is not review_record.ReceiptPhase.ABSENT)
        or downgrading_recovered
    ):
        audit_override(force_reason)

    completed = review_record.ReviewRecord.recover(
        store,
        lane_id,
        payload,
        force=bool(force_reason),
        lane_created=created,
        record=record,
    )
    journal.ledger(
        f"review-recover {lane_id} {'complete' if is_complete else 'partial'} "
        f"sources={len(source_urls)} dispositions={len(verified_dispositions)} "
        f"missing={','.join(missing_facts) or '-'}",
        kind="review.recovered",
        level="info" if is_complete else "warn",
        lane=record,
        details={
            "complete": is_complete,
            "sources": source_urls,
            "unavailable_sources": unavailable,
            "dispositions": len(verified_dispositions),
            "incomplete_reasons": incomplete_reasons,
            "missing_facts": missing_facts,
            "recovered_by": actor,
            "preserved_artifacts": len(preserved_artifacts),
        },
        store=store,
    )
    return completed, is_complete
