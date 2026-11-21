"""Antigravity / T3 Code transcript adapter.

Discovers ~/.t3/userdata/providers/antigravity/**/brain/*/.system_generated/logs/transcript.jsonl
(covering both antigravity/*/brain/... and antigravity/*/antigravity-acp/brain/...).
"""
from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from ..schema import (
    RequestRecord,
    as_dict,
    as_list,
    classify_tools,
    detect_cache_miss,
    format_timestamp,
    parse_timestamp,
)

EFFORT_SUFFIX_RE = re.compile(r"-(minimal|low|medium|high|xhigh|max)$")
WORKSPACE_RE = re.compile(r"(?:Command Working Directory:\s*|Active Workspaces:\s*\n-\s*)(/\S+)")


def _as_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _conversation_id_from_path(path: Path) -> str:
    parts = path.parts
    for i, part in enumerate(parts):
        if part == "brain" and i + 1 < len(parts):
            return parts[i + 1]
    return path.stem


def _read_sibling_metadata(path: Path) -> dict[str, Any]:
    """Read optional T3 / Antigravity session metadata beside the transcript if present."""
    brain_dir = path.parent.parent.parent  # brain/<conversation-id>
    for candidate in (brain_dir / "metadata.json", path.with_name("metadata.json")):
        try:
            data = as_dict(json.loads(candidate.read_text(encoding="utf-8")))
            if data is not None:
                return data
        except (OSError, ValueError):
            continue
    return {}


class AntigravityAdapter:
    name = "antigravity"

    def discover(self, home: Path) -> list[Path]:
        root = home / ".t3" / "userdata" / "providers" / "antigravity"
        if not root.is_dir():
            return []
        return sorted(root.rglob("brain/*/.system_generated/logs/transcript.jsonl"))

    def parse_lines(
        self,
        path: Path,
        lines: list[str],
        cursor: dict[str, Any],
        host: str,
    ) -> list[RequestRecord]:
        conv_id = str(cursor.get("session_id") or _conversation_id_from_path(path))
        meta = _read_sibling_metadata(path)
        seen_ids: set[str] = set(cursor.get("seen_ids") or [])
        prev_time_str: str | None = cursor.get("prev_time")
        prev_time = parse_timestamp(prev_time_str) if prev_time_str else None
        prev_model: str | None = cursor.get("prev_model")
        cwd = str(cursor.get("cwd") or meta.get("cwd") or meta.get("workspace") or "")
        agent_role = str(cursor.get("agent_role") or meta.get("agent_role") or "main")
        agent_type = str(cursor.get("agent_type") or meta.get("agent_type") or "antigravity")
        compacted = bool(cursor.get("compacted", False))

        records: list[RequestRecord] = []
        for idx, raw in enumerate(lines):
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                obj = as_dict(json.loads(stripped))
            except ValueError:
                continue
            if obj is None:
                continue

            step_type = obj.get("type")
            if step_type == "USER_INPUT" and not cwd:
                content = obj.get("content")
                if isinstance(content, str):
                    match = WORKSPACE_RE.search(content)
                    if match:
                        cwd = match.group(1).strip()
                    if "Act as the implementation sub-agent" in content or "subagent" in str(meta.get("role", "")):
                        agent_role = "subagent"
                continue

            if step_type in ("COMPACT", "COMPACTED"):
                compacted = True
                continue

            if step_type != "PLANNER_RESPONSE":
                continue

            step_index = obj.get("step_index", idx)
            req_id = f"{conv_id}:step:{step_index}"
            if req_id in seen_ids:
                continue

            moment = parse_timestamp(obj.get("created_at") or obj.get("timestamp"))
            if moment is None:
                continue

            # In Antigravity PLANNER_RESPONSE, input_tokens is non-cached fresh input and cache_read_tokens is cached.
            input_tokens = _as_int(obj.get("input_tokens"))
            cache_read = _as_int(obj.get("cache_read_tokens"))
            cache_write = _as_int(obj.get("cache_write_tokens"))
            output_tokens = _as_int(obj.get("output_tokens"))
            reasoning_tokens = _as_int(obj.get("thinking_tokens") or obj.get("reasoning_tokens"))
            if input_tokens == 0 and cache_read == 0 and output_tokens == 0:
                continue

            seen_ids.add(req_id)
            ts_str = format_timestamp(moment)
            day = ts_str[:10]

            model = str(obj.get("model") or meta.get("model") or "")
            effort = str(obj.get("thought_level") or obj.get("effort") or meta.get("thought_level") or "")
            if not effort and model:
                suffix = EFFORT_SUFFIX_RE.search(model)
                if suffix:
                    effort = suffix.group(1)

            tools: list[str] = []
            shell_cmds: list[str] = []
            raw_calls = as_list(obj.get("tool_calls"))
            if raw_calls is not None:
                for raw_call in raw_calls:
                    call = as_dict(raw_call)
                    if call is not None:
                        name = str(call.get("name") or "")
                        if name:
                            tools.append(name)
                            args = as_dict(call.get("args"))
                            if args is not None:
                                cmd = args.get("CommandLine") or args.get("command") or ""
                                if isinstance(cmd, str) and cmd:
                                    shell_cmds.append(cmd.strip('"'))

            buckets = {"default": cache_write} if cache_write > 0 else {}
            gap_seconds: float | None = None
            is_first = prev_time is None
            if prev_time is not None:
                gap_seconds = max(0.0, (moment - prev_time).total_seconds())

            context_tokens = input_tokens + cache_read + cache_write
            miss, reason = detect_cache_miss(
                is_first=is_first,
                context_tokens=context_tokens,
                cache_write_tokens=cache_write,
                cache_write_ttl_buckets=buckets,
                agent_role=agent_role,
                gap_seconds=gap_seconds,
                prev_model=prev_model,
                model=model,
                compacted=compacted,
            )
            compacted = False
            is_wait, is_small_read = classify_tools(tools, shell_cmds)

            records.append(RequestRecord(
                provider=self.name,
                request_id=req_id,
                time=ts_str,
                day=day,
                host=host,
                project=cwd,
                session_id=conv_id,
                agent_id=conv_id,
                agent_role=agent_role,
                agent_type=agent_type,
                transcript_path=str(path),
                model=model,
                effort=effort,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                reasoning_tokens=reasoning_tokens,
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
                cache_write_ttl_buckets=buckets,
                tools=tools,
                is_wait_turn=is_wait,
                is_small_read_turn=is_small_read,
                gap_seconds=gap_seconds,
                cache_miss=miss,
                cache_miss_reason=reason,
            ))

            prev_time = moment
            prev_time_str = ts_str
            if model:
                prev_model = model

        cursor["seen_ids"] = sorted(seen_ids)[-5000:]
        cursor["prev_time"] = prev_time_str
        cursor["prev_model"] = prev_model
        cursor["compacted"] = compacted
        cursor["cwd"] = cwd
        cursor["session_id"] = conv_id
        cursor["agent_role"] = agent_role
        cursor["agent_type"] = agent_type
        return records
