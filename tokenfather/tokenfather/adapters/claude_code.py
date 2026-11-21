"""Claude Code transcript adapter (~/.claude/projects/*/*.jsonl and */subagents/*.jsonl + .meta.json)."""
from __future__ import annotations

import json
from pathlib import Path
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


def _as_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _read_subagent_meta(path: Path) -> dict[str, Any]:
    meta_path = path.with_suffix(".meta.json")
    try:
        data = as_dict(json.loads(meta_path.read_text(encoding="utf-8")))
        return data if data is not None else {}
    except (OSError, ValueError):
        return {}


class ClaudeCodeAdapter:
    name = "claude-code"

    def discover(self, home: Path) -> list[Path]:
        projects_dir = home / ".claude" / "projects"
        if not projects_dir.is_dir():
            return []
        found: list[Path] = []
        found.extend(sorted(projects_dir.glob("*/*.jsonl")))
        found.extend(sorted(projects_dir.glob("*/*/subagents/*.jsonl")))
        return sorted(set(found))

    def parse_lines(
        self,
        path: Path,
        lines: list[str],
        cursor: dict[str, Any],
        host: str,
    ) -> list[RequestRecord]:
        is_subagent = path.parent.name == "subagents"
        meta = _read_subagent_meta(path) if is_subagent else {}
        agent_role = "subagent" if is_subagent else "main"
        agent_type = str(meta.get("agentType") or ("subagent" if is_subagent else "main"))
        agent_id = path.stem

        raw_seen = as_list(cursor.get("seen_ids")) or []
        seen_order: list[str] = [str(s) for s in raw_seen]
        seen_ids: set[str] = set(seen_order)
        last_entry = as_dict(cursor.get("last_entry"))
        prev_time_str: str | None = cursor.get("prev_time")
        prev_model: str | None = cursor.get("prev_model")
        compacted = bool(cursor.get("compacted", False))
        cwd = str(cursor.get("cwd") or "")
        session_id = str(cursor.get("session_id") or (path.parent.parent.name if is_subagent else path.stem))

        # Claude Code writes one line per content block with the same message.id and streaming usage updates.
        # Group lines in this batch by message.id so each model request is counted once with its latest/max
        # output_tokens and all its tools, including streaming blocks that arrive in a later incremental batch.
        grouped: dict[str, dict[str, Any]] = {}
        order: list[str] = []

        for raw in lines:
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                obj = as_dict(json.loads(stripped))
            except ValueError:
                continue
            if obj is None:
                continue

            if isinstance(obj.get("cwd"), str) and obj["cwd"]:
                cwd = str(obj["cwd"])
            if isinstance(obj.get("sessionId"), str) and obj["sessionId"]:
                session_id = str(obj["sessionId"])

            rec_type = obj.get("type")
            if rec_type == "system" and obj.get("subtype") in ("compact_boundary", "microcompact_boundary"):
                compacted = True
                continue
            if rec_type == "compacted":
                compacted = True
                continue

            if rec_type != "assistant":
                continue
            message = as_dict(obj.get("message"))
            if message is None:
                continue
            usage = as_dict(message.get("usage"))
            if usage is None:
                continue

            msg_id = str(message.get("id") or obj.get("requestId") or obj.get("uuid") or "")
            if not msg_id:
                continue
            if msg_id in seen_ids:
                if last_entry is None or msg_id != last_entry.get("msg_id"):
                    continue
                if msg_id not in grouped:
                    restored_usage = as_dict(last_entry.get("usage")) or {}
                    grouped[msg_id] = {
                        "msg_id": msg_id,
                        "timestamp": last_entry.get("timestamp"),
                        "cwd": str(last_entry.get("cwd") or cwd),
                        "session_id": str(last_entry.get("session_id") or session_id),
                        "model": str(last_entry.get("model") or ""),
                        "effort": str(last_entry.get("effort") or ""),
                        "usage": dict(restored_usage),
                        "tools": list(last_entry.get("tools") or []),
                        "shell_commands": list(last_entry.get("shell_commands") or []),
                        "compacted": bool(last_entry.get("compacted", False)),
                        "_from_prev": True,
                        "_updated": False,
                        "_prev_time": last_entry.get("prev_time"),
                        "_prev_model": last_entry.get("prev_model"),
                    }
                    order.append(msg_id)

            entry = grouped.get(msg_id)
            if entry is None:
                tools_list: list[str] = []
                cmds_list: list[str] = []
                new_entry: dict[str, Any] = {
                    "msg_id": msg_id,
                    "timestamp": obj.get("timestamp"),
                    "cwd": str(obj.get("cwd") or cwd),
                    "session_id": str(obj.get("sessionId") or session_id),
                    "model": str(message.get("model") or meta.get("pickedEffortModel") or ""),
                    "effort": str(obj.get("effort") or obj.get("perTurnEffort") or ""),
                    "usage": usage,
                    "tools": tools_list,
                    "shell_commands": cmds_list,
                    "compacted": compacted,
                }
                entry = new_entry
                compacted = False
                grouped[msg_id] = entry
                order.append(msg_id)
            else:
                if not entry["effort"]:
                    entry["effort"] = str(obj.get("effort") or obj.get("perTurnEffort") or "")
                new_out = _as_int(usage.get("output_tokens"))
                cur_out = _as_int(entry["usage"].get("output_tokens"))
                if new_out >= cur_out:
                    if new_out > cur_out:
                        entry["_updated"] = True
                    entry["usage"] = usage

            content = as_list(message.get("content"))
            if content is not None:
                entry_tools: list[str] = entry["tools"]
                entry_cmds: list[str] = entry["shell_commands"]
                for raw_block in content:
                    block = as_dict(raw_block)
                    if block is not None and block.get("type") == "tool_use":
                        tool_name = str(block.get("name") or "")
                        if tool_name:
                            entry_tools.append(tool_name)
                            entry["_updated"] = True
                            inp = as_dict(block.get("input"))
                            if inp is not None and isinstance(inp.get("command"), str):
                                entry_cmds.append(str(inp["command"]))

        records: list[RequestRecord] = []
        for msg_id in order:
            entry = grouped[msg_id]
            from_prev = bool(entry.get("_from_prev"))
            if from_prev and not entry.get("_updated"):
                continue
            if msg_id not in seen_ids:
                seen_ids.add(msg_id)
                seen_order.append(msg_id)
            usage = entry["usage"]

            moment = parse_timestamp(entry["timestamp"])
            if moment is None:
                continue
            ts_str = format_timestamp(moment)
            day = ts_str[:10]

            input_tokens = _as_int(usage.get("input_tokens"))
            output_tokens = _as_int(usage.get("output_tokens"))
            cache_read = _as_int(usage.get("cache_read_input_tokens"))
            cache_write_total = _as_int(usage.get("cache_creation_input_tokens"))

            # Map Anthropic TTL details into provider-agnostic buckets ("short" = 5m, "long" = 1h).
            buckets: dict[str, int] = {}
            cc = as_dict(usage.get("cache_creation"))
            if cc is not None:
                short_tok = _as_int(cc.get("ephemeral_5m_input_tokens"))
                long_tok = _as_int(cc.get("ephemeral_1h_input_tokens"))
                if short_tok:
                    buckets["short"] = short_tok
                if long_tok:
                    buckets["long"] = long_tok
                if cache_write_total == 0:
                    cache_write_total = short_tok + long_tok
            elif cache_write_total > 0:
                buckets["short" if is_subagent else "long"] = cache_write_total

            out_details = as_dict(usage.get("output_tokens_details"))
            reasoning_tokens = _as_int(out_details.get("thinking_tokens")) if out_details is not None else 0

            ref_prev_time_str: str | None = entry.get("_prev_time") if from_prev else prev_time_str
            ref_prev_time = parse_timestamp(ref_prev_time_str) if ref_prev_time_str else None
            ref_prev_model: str | None = entry.get("_prev_model") if from_prev else prev_model

            gap_seconds: float | None = None
            is_first = ref_prev_time is None
            if ref_prev_time is not None:
                gap_seconds = max(0.0, (moment - ref_prev_time).total_seconds())

            context_tokens = input_tokens + cache_read + cache_write_total
            miss, reason = detect_cache_miss(
                is_first=is_first,
                context_tokens=context_tokens,
                cache_write_tokens=cache_write_total,
                cache_write_ttl_buckets=buckets,
                agent_role=agent_role,
                gap_seconds=gap_seconds,
                prev_model=ref_prev_model,
                model=str(entry["model"]),
                compacted=bool(entry["compacted"]),
                input_tokens=input_tokens,
            )
            is_wait, is_small_read = classify_tools(entry["tools"], entry["shell_commands"])

            records.append(RequestRecord(
                provider=self.name,
                request_id=msg_id,
                time=ts_str,
                day=day,
                host=host,
                project=str(entry["cwd"]),
                session_id=str(entry["session_id"]),
                agent_id=agent_id,
                agent_role=agent_role,
                agent_type=agent_type,
                transcript_path=str(path),
                model=str(entry["model"]),
                effort=str(entry["effort"]),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                reasoning_tokens=reasoning_tokens,
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write_total,
                cache_write_ttl_buckets=buckets,
                tools=list(entry["tools"]),
                is_wait_turn=is_wait,
                is_small_read_turn=is_small_read,
                gap_seconds=gap_seconds,
                cache_miss=miss,
                cache_miss_reason=reason,
            ))

            last_entry = {
                "msg_id": msg_id,
                "timestamp": entry["timestamp"],
                "cwd": str(entry["cwd"]),
                "session_id": str(entry["session_id"]),
                "model": str(entry["model"]),
                "effort": str(entry["effort"]),
                "usage": dict(usage),
                "tools": list(entry["tools"]),
                "shell_commands": list(entry["shell_commands"]),
                "compacted": bool(entry["compacted"]),
                "prev_time": ref_prev_time_str,
                "prev_model": ref_prev_model,
            }
            prev_time_str = ts_str
            if entry["model"]:
                prev_model = str(entry["model"])

        cursor["seen_ids"] = seen_order[-5000:]
        cursor["last_entry"] = last_entry
        cursor["prev_time"] = prev_time_str
        cursor["prev_model"] = prev_model
        cursor["compacted"] = compacted
        cursor["cwd"] = cwd
        cursor["session_id"] = session_id
        return records
