"""Provider-agnostic normalized request schema, timestamp helpers, turn classification, and cache miss detection."""
from __future__ import annotations

from dataclasses import dataclass, field
import datetime
import re
import socket
from typing import Any, cast

CACHE_MISS_MIN_CONTEXT = 30_000
CACHE_MISS_WRITE_RATIO = 0.5
SHORT_TTL_SECONDS = 300.0
LONG_TTL_SECONDS = 3600.0

WAIT_TOOLS = frozenset({
    "Monitor",
    "TaskStop",
    "TaskOutput",
    "schedule",
    "t3_thread_wait",
    "mcp_t3-code_t3_thread_wait",
    "mcp_t3-code_t3_background_wait",
})
SHELL_TOOLS = frozenset({"Bash", "run_command", "exec", "shell", "shell_command"})
SMALL_READ_TOOLS = frozenset({"Read", "Grep", "Glob", "read_file", "grep_search", "find_by_name"})
WAIT_SHELL_RE = re.compile(r"(?:^|[;&|]\s*)(?:sleep\b|until\b|while\s+.*sleep\b)", re.MULTILINE)
SMALL_READ_SHELL_RE = re.compile(
    r"(?:^|[;&|]\s*)(?:sed\s+-n\b|grep\b|rg\b|head\b|tail\b|wc\b|ls\b|cat\b|awk\b)", re.MULTILINE
)


def as_dict(value: Any) -> dict[str, Any] | None:
    """Narrow a JSON-decoded object to dict[str, Any] for strict type checking."""
    return cast(dict[str, Any], value) if isinstance(value, dict) else None


def as_list(value: Any) -> list[Any] | None:
    """Narrow a JSON-decoded array to list[Any] for strict type checking."""
    return cast(list[Any], value) if isinstance(value, list) else None


def host_name() -> str:
    """Short lowercase host name, matching the shared fathers event schema."""
    name = re.sub(r"[^a-z0-9-]", "-", socket.gethostname().split(".", 1)[0].lower()).strip("-")
    return name[:63].strip("-") or "localhost"


def parse_timestamp(value: Any) -> datetime.datetime | None:
    """Parse an ISO-8601 / RFC-3339 timestamp into an aware UTC datetime, or None."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=datetime.UTC)
    return parsed.astimezone(datetime.UTC)


def format_timestamp(moment: datetime.datetime) -> str:
    """Format an aware datetime as RFC 3339 UTC with millisecond precision."""
    utc = moment.astimezone(datetime.UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def classify_tools(tools: list[str], shell_commands: list[str]) -> tuple[bool, bool]:
    """Return (is_wait_turn, is_small_read_turn) from the tools and shell commands invoked in one request."""
    if not tools:
        return False, False
    wait_hits = 0
    small_read_hits = 0
    shell_idx = 0
    for name in tools:
        if name in WAIT_TOOLS:
            wait_hits += 1
        elif name in SHELL_TOOLS:
            cmd = shell_commands[shell_idx] if shell_idx < len(shell_commands) else ""
            shell_idx += 1
            if cmd and WAIT_SHELL_RE.search(cmd):
                wait_hits += 1
            elif cmd and SMALL_READ_SHELL_RE.search(cmd):
                small_read_hits += 1
        elif name in SMALL_READ_TOOLS:
            small_read_hits += 1
    total = len(tools)
    is_wait = wait_hits > 0 and wait_hits == total
    is_small_read = not is_wait and small_read_hits > 0 and small_read_hits == total
    return is_wait, is_small_read


def detect_cache_miss(
    *,
    is_first: bool,
    context_tokens: int,
    cache_write_tokens: int,
    cache_write_ttl_buckets: dict[str, int],
    agent_role: str,
    gap_seconds: float | None,
    prev_model: str | None,
    model: str,
    compacted: bool,
    input_tokens: int = 0,
) -> tuple[bool, str]:
    """Classify whether a request suffered a cache miss and why (fathers#199).

    Rule: not the agent's first request, context > 30K, and uncached context (input + cache write) > 50% of context.
    """
    if is_first or context_tokens <= CACHE_MISS_MIN_CONTEXT:
        return False, ""
    if (input_tokens + cache_write_tokens) <= context_tokens * CACHE_MISS_WRITE_RATIO:
        return False, ""
    if prev_model and model and prev_model != model:
        return True, "model_switch"
    if compacted:
        return True, "compaction"
    short_tokens = cache_write_ttl_buckets.get("short", 0)
    long_tokens = cache_write_ttl_buckets.get("long", 0)
    ttl = SHORT_TTL_SECONDS if (short_tokens > long_tokens or agent_role == "subagent") else LONG_TTL_SECONDS
    if gap_seconds is not None and gap_seconds > ttl:
        if agent_role == "main" and gap_seconds > LONG_TTL_SECONDS:
            return True, "resume"
        return True, "wake_after_ttl"
    return True, "unknown"


def _empty_int_dict() -> dict[str, int]:
    return {}


def _empty_str_list() -> list[str]:
    return []


@dataclass
class RequestRecord:
    """One normalized model request across any supported provider."""

    provider: str
    request_id: str
    time: str
    day: str
    host: str
    project: str
    session_id: str
    agent_id: str
    agent_role: str  # "main" | "subagent"
    agent_type: str
    transcript_path: str
    model: str
    effort: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cache_write_ttl_buckets: dict[str, int] = field(default_factory=_empty_int_dict)
    reasoning_tokens: int = 0
    tools: list[str] = field(default_factory=_empty_str_list)
    is_wait_turn: bool = False
    is_small_read_turn: bool = False
    gap_seconds: float | None = None
    cache_miss: bool = False
    cache_miss_reason: str = ""
    lane_id: str = ""
    lane_project: str = ""

    @property
    def context_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": 1,
            "provider": self.provider,
            "request_id": self.request_id,
            "time": self.time,
            "day": self.day,
            "host": self.host,
            "project": self.project,
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "agent_role": self.agent_role,
            "agent_type": self.agent_type,
            "transcript_path": self.transcript_path,
            "model": self.model,
            "effort": self.effort,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cache_write_ttl_buckets": dict(self.cache_write_ttl_buckets),
            "context_tokens": self.context_tokens,
            "tools": list(self.tools),
            "is_wait_turn": self.is_wait_turn,
            "is_small_read_turn": self.is_small_read_turn,
            "gap_seconds": self.gap_seconds,
            "cache_miss": self.cache_miss,
            "cache_miss_reason": self.cache_miss_reason,
            "lane_id": self.lane_id,
            "lane_project": self.lane_project,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RequestRecord:
        raw_buckets = as_dict(data.get("cache_write_ttl_buckets"))
        buckets: dict[str, int] = {}
        if raw_buckets is not None:
            for k, v in raw_buckets.items():
                if isinstance(v, int) and not isinstance(v, bool):
                    buckets[str(k)] = v
        raw_tools = as_list(data.get("tools"))
        tools = [str(t) for t in raw_tools] if raw_tools is not None else []
        raw_gap = data.get("gap_seconds")
        gap = float(raw_gap) if isinstance(raw_gap, (int, float)) and not isinstance(raw_gap, bool) else None
        return cls(
            provider=str(data.get("provider", "")),
            request_id=str(data.get("request_id", "")),
            time=str(data.get("time", "")),
            day=str(data.get("day", "")),
            host=str(data.get("host", "")),
            project=str(data.get("project", "")),
            session_id=str(data.get("session_id", "")),
            agent_id=str(data.get("agent_id", "")),
            agent_role=str(data.get("agent_role", "main")),
            agent_type=str(data.get("agent_type", "")),
            transcript_path=str(data.get("transcript_path", "")),
            model=str(data.get("model", "")),
            effort=str(data.get("effort", "")),
            input_tokens=int(data.get("input_tokens", 0)),
            output_tokens=int(data.get("output_tokens", 0)),
            reasoning_tokens=int(data.get("reasoning_tokens", 0)),
            cache_read_tokens=int(data.get("cache_read_tokens", 0)),
            cache_write_tokens=int(data.get("cache_write_tokens", 0)),
            cache_write_ttl_buckets=buckets,
            tools=tools,
            is_wait_turn=bool(data.get("is_wait_turn", False)),
            is_small_read_turn=bool(data.get("is_small_read_turn", False)),
            gap_seconds=gap,
            cache_miss=bool(data.get("cache_miss", False)),
            cache_miss_reason=str(data.get("cache_miss_reason", "")),
            lane_id=str(data.get("lane_id", "")),
            lane_project=str(data.get("lane_project", "")),
        )
