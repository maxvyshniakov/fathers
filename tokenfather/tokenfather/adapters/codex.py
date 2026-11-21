"""Codex session adapter (~/.codex/sessions/**/rollout-*.jsonl)."""
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


class CodexAdapter:
    name = "codex"

    def discover(self, home: Path) -> list[Path]:
        sessions_dir = home / ".codex" / "sessions"
        if not sessions_dir.is_dir():
            return []
        return sorted(sessions_dir.rglob("rollout-*.jsonl"))

    def parse_lines(
        self,
        path: Path,
        lines: list[str],
        cursor: dict[str, Any],
        host: str,
    ) -> list[RequestRecord]:
        raw_seen = as_list(cursor.get("seen_ids")) or []
        seen_order: list[str] = [str(s) for s in raw_seen]
        seen_ids: set[str] = set(seen_order)
        line_offset = _as_int(cursor.get("line_offset"))
        raw_tc = as_list(cursor.get("emitted_tc")) or []
        emitted_tc: list[dict[str, Any]] = [
            d for item in raw_tc if (d := as_dict(item)) is not None
        ]
        prev_time_str: str | None = cursor.get("prev_time")
        prev_time = parse_timestamp(prev_time_str) if prev_time_str else None
        prev_model: str | None = cursor.get("prev_model")
        compacted = bool(cursor.get("compacted", False))
        cwd = str(cursor.get("cwd") or "")
        session_id = str(cursor.get("session_id") or path.stem)
        model = str(cursor.get("model") or "")
        effort = str(cursor.get("effort") or "")
        has_usage_records = bool(cursor.get("has_usage_records", False))
        last_total_tokens = _as_int(cursor.get("last_total_tokens"))
        pending_tools: list[str] = list(cursor.get("pending_tools") or [])
        pending_cmds: list[str] = list(cursor.get("pending_cmds") or [])

        # Pre-scan batch to see if token_usage_record exists (modern Codex emits both token_usage_record and
        # event_msg:token_count for the same request; prefer token_usage_record when present to avoid double counting).
        parsed_objs: list[tuple[int, dict[str, Any]]] = []
        for idx, raw in enumerate(lines):
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                obj = as_dict(json.loads(stripped))
            except ValueError:
                continue
            if obj is not None:
                if obj.get("type") == "token_usage_record":
                    has_usage_records = True
                parsed_objs.append((line_offset + idx, obj))

        records: list[RequestRecord] = []
        for line_no, obj in parsed_objs:
            rec_type = obj.get("type")
            payload = as_dict(obj.get("payload"))
            if payload is None:
                continue

            if rec_type == "session_meta":
                if isinstance(payload.get("session_id"), str) and payload["session_id"]:
                    session_id = str(payload["session_id"])
                elif isinstance(payload.get("id"), str) and payload["id"]:
                    session_id = str(payload["id"])
                if isinstance(payload.get("cwd"), str) and payload["cwd"]:
                    cwd = str(payload["cwd"])
                continue

            if rec_type == "turn_context":
                if isinstance(payload.get("cwd"), str) and payload["cwd"]:
                    cwd = str(payload["cwd"])
                if isinstance(payload.get("model"), str) and payload["model"]:
                    model = str(payload["model"])
                collab = as_dict(payload.get("collaboration_mode"))
                if collab is not None:
                    settings = as_dict(collab.get("settings"))
                    if settings is not None:
                        if isinstance(settings.get("model"), str) and settings["model"]:
                            model = str(settings["model"])
                        if isinstance(settings.get("reasoning_effort"), str) and settings["reasoning_effort"]:
                            effort = str(settings["reasoning_effort"])
                if isinstance(payload.get("effort"), str) and payload["effort"]:
                    effort = str(payload["effort"])
                continue

            if rec_type == "compacted":
                compacted = True
                continue

            if rec_type == "response_item":
                ptype = payload.get("type")
                if ptype in ("function_call", "custom_tool_call"):
                    name = str(payload.get("name") or "")
                    if name:
                        pending_tools.append(name)
                        args_raw = payload.get("arguments") or payload.get("input") or ""
                        if isinstance(args_raw, str):
                            pending_cmds.append(args_raw)
                continue

            usage_dict: dict[str, Any] | None = None
            req_id = ""
            from_token_count = False
            tc_total = 0
            if rec_type == "token_usage_record":
                usage = as_dict(payload.get("usage"))
                if usage is not None:
                    usage_dict = usage
                    req_id = str(payload.get("response_id") or f"{session_id}:ord:{obj.get('ordinal', line_no)}")
            elif rec_type == "event_msg" and payload.get("type") == "token_count" and not has_usage_records:
                info = as_dict(payload.get("info"))
                if info is not None:
                    total_info = as_dict(info.get("total_token_usage"))
                    last_info = as_dict(info.get("last_token_usage"))
                    total_tok = _as_int(total_info.get("total_tokens")) if total_info is not None else 0
                    if last_info is not None and (total_tok == 0 or total_tok != last_total_tokens):
                        if total_tok:
                            last_total_tokens = total_tok
                        usage_dict = last_info
                        from_token_count = True
                        tc_total = total_tok
                        req_id = f"{session_id}:tc:{obj.get('timestamp', '')}:{total_tok}:{line_no}"

            if usage_dict is None or not req_id or req_id in seen_ids:
                continue

            moment = parse_timestamp(obj.get("timestamp"))
            if moment is None:
                continue

            # Codex's input_tokens is inclusive of cached_input_tokens; normalize to non-cached input_tokens.
            raw_input = _as_int(usage_dict.get("input_tokens"))
            cache_read = _as_int(usage_dict.get("cached_input_tokens"))
            cache_write = _as_int(usage_dict.get("cache_write_input_tokens"))
            non_cached_input = max(0, raw_input - cache_read - cache_write)
            output_tokens = _as_int(usage_dict.get("output_tokens"))
            reasoning_tokens = _as_int(usage_dict.get("reasoning_output_tokens"))
            usage_total = _as_int(usage_dict.get("total_tokens"))

            # Deduplicate token_usage_record against a token_count already emitted in an earlier incremental batch.
            if not from_token_count and emitted_tc:
                ts_epoch = moment.timestamp()
                matched_idx: int | None = None
                for i_tc, prev_tc in enumerate(emitted_tc):
                    prev_ts = prev_tc.get("ts")
                    if not isinstance(prev_ts, (int, float)):
                        continue
                    if abs(ts_epoch - float(prev_ts)) > 5.0:
                        continue
                    same_usage = (
                        _as_int(prev_tc.get("raw_input")) == raw_input
                        and _as_int(prev_tc.get("cache_read")) == cache_read
                        and _as_int(prev_tc.get("output")) == output_tokens
                    )
                    same_total = usage_total > 0 and _as_int(prev_tc.get("total")) == usage_total
                    if same_usage or same_total:
                        matched_idx = i_tc
                        break
                if matched_idx is not None:
                    seen_ids.add(req_id)
                    seen_order.append(req_id)
                    emitted_tc.pop(matched_idx)
                    continue

            seen_ids.add(req_id)
            seen_order.append(req_id)
            if from_token_count:
                emitted_tc.append({
                    "ts": moment.timestamp(),
                    "raw_input": raw_input,
                    "cache_read": cache_read,
                    "output": output_tokens,
                    "total": tc_total,
                })
            ts_str = format_timestamp(moment)
            day = ts_str[:10]
            buckets = {"default": cache_write} if cache_write > 0 else {}

            gap_seconds: float | None = None
            is_first = prev_time is None
            if prev_time is not None:
                gap_seconds = max(0.0, (moment - prev_time).total_seconds())

            context_tokens = non_cached_input + cache_read + cache_write
            miss, reason = detect_cache_miss(
                is_first=is_first,
                context_tokens=context_tokens,
                cache_write_tokens=cache_write,
                cache_write_ttl_buckets=buckets,
                agent_role="main",
                gap_seconds=gap_seconds,
                prev_model=prev_model,
                model=model,
                compacted=compacted,
                input_tokens=non_cached_input,
            )
            compacted = False
            is_wait, is_small_read = classify_tools(pending_tools, pending_cmds)

            records.append(RequestRecord(
                provider=self.name,
                request_id=req_id,
                time=ts_str,
                day=day,
                host=host,
                project=cwd,
                session_id=session_id,
                agent_id=session_id,
                agent_role="main",
                agent_type="codex",
                transcript_path=str(path),
                model=model,
                effort=effort,
                input_tokens=non_cached_input,
                output_tokens=output_tokens,
                reasoning_tokens=reasoning_tokens,
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
                cache_write_ttl_buckets=buckets,
                tools=list(pending_tools),
                is_wait_turn=is_wait,
                is_small_read_turn=is_small_read,
                gap_seconds=gap_seconds,
                cache_miss=miss,
                cache_miss_reason=reason,
            ))

            pending_tools.clear()
            pending_cmds.clear()
            prev_time = moment
            prev_time_str = ts_str
            if model:
                prev_model = model

        cursor["seen_ids"] = seen_order[-5000:]
        cursor["line_offset"] = line_offset + len(lines)
        cursor["emitted_tc"] = emitted_tc[-50:]
        cursor["prev_time"] = prev_time_str
        cursor["prev_model"] = prev_model
        cursor["compacted"] = compacted
        cursor["cwd"] = cwd
        cursor["session_id"] = session_id
        cursor["model"] = model
        cursor["effort"] = effort
        cursor["has_usage_records"] = has_usage_records
        cursor["last_total_tokens"] = last_total_tokens
        cursor["pending_tools"] = pending_tools[-64:]
        cursor["pending_cmds"] = pending_cmds[-64:]
        return records
