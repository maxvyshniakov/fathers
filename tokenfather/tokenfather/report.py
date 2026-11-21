"""Usage reporting over the incremental daily rollup."""
from __future__ import annotations

import datetime
from typing import Any

from .attribution import LaneIndex
from .pricing import PricingTable
from .schema import RequestRecord, parse_timestamp


def _percentile(values: list[int], pct: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    idx = (len(ordered) - 1) * pct
    lo = int(idx)
    hi = min(lo + 1, len(ordered) - 1)
    frac = idx - lo
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * frac)


def _pct(part: float, total: float) -> float:
    return round((part / total) * 100.0, 2) if total > 0 else 0.0


def _iso_week(day_str: str) -> str:
    try:
        d = datetime.date.fromisoformat(day_str)
    except ValueError:
        return day_str
    iso = d.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def build_report(
    records: list[RequestRecord],
    pricing: PricingTable,
    *,
    lane_index: LaneIndex | None = None,
    since: str | None = None,
    until: str | None = None,
    project: str | None = None,
) -> dict[str, Any]:
    """Compute the full tokenfather report dictionary from normalized RequestRecords."""
    total_requests = len(records)
    total_input = 0
    total_output = 0
    total_cread = 0
    total_cwrite = 0
    w_input = 0.0
    w_output = 0.0
    w_cread = 0.0
    w_cwrite = 0.0
    w_total = 0.0
    contexts: list[int] = []

    by_provider: dict[str, dict[str, Any]] = {}
    by_model_effort: dict[str, dict[str, Any]] = {}
    by_role: dict[str, dict[str, Any]] = {}
    role_contexts: dict[str, list[int]] = {}
    by_day: dict[str, dict[str, Any]] = {}
    by_week: dict[str, dict[str, Any]] = {}
    by_lane: dict[tuple[str, str], dict[str, Any]] = {}

    miss_count = 0
    miss_weighted = 0.0
    miss_by_reason: dict[str, int] = {}
    miss_by_role: dict[str, int] = {}

    wait_turns = 0
    wait_by_tool: dict[str, int] = {}
    small_read_turns = 0

    for rec in records:
        comp = pricing.weighted_components(rec)
        ctx = rec.context_tokens
        contexts.append(ctx)

        total_input += rec.input_tokens
        total_output += rec.output_tokens
        total_cread += rec.cache_read_tokens
        total_cwrite += rec.cache_write_tokens
        w_input += comp["input"]
        w_output += comp["output"]
        w_cread += comp["cache_read"]
        w_cwrite += comp["cache_write"]
        w_total += comp["total"]

        # By provider
        prov = by_provider.setdefault(rec.provider, {
            "requests": 0, "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_write_tokens": 0, "weighted_units": 0.0,
        })
        prov["requests"] += 1
        prov["input_tokens"] += rec.input_tokens
        prov["output_tokens"] += rec.output_tokens
        prov["cache_read_tokens"] += rec.cache_read_tokens
        prov["cache_write_tokens"] += rec.cache_write_tokens
        prov["weighted_units"] += comp["total"]

        # By model + effort
        me_key = f"{rec.model or 'unknown'}:{rec.effort or 'default'}"
        me = by_model_effort.setdefault(me_key, {
            "provider": rec.provider, "model": rec.model or "unknown", "effort": rec.effort or "default",
            "requests": 0, "weighted_units": 0.0, "output_tokens": 0,
        })
        me["requests"] += 1
        me["weighted_units"] += comp["total"]
        me["output_tokens"] += rec.output_tokens

        # By role (main vs subagent)
        role_bucket = by_role.setdefault(rec.agent_role, {"requests": 0, "weighted_units": 0.0})
        role_bucket["requests"] += 1
        role_bucket["weighted_units"] += comp["total"]
        role_contexts.setdefault(rec.agent_role, []).append(ctx)

        # By day and week
        day_bucket = by_day.setdefault(rec.day, {"requests": 0, "weighted_units": 0.0, "lanes": set()})
        day_bucket["requests"] += 1
        day_bucket["weighted_units"] += comp["total"]
        if rec.lane_id:
            day_bucket["lanes"].add((rec.lane_project, rec.lane_id))

        week = _iso_week(rec.day)
        week_bucket = by_week.setdefault(week, {"requests": 0, "weighted_units": 0.0, "lanes": set()})
        week_bucket["requests"] += 1
        week_bucket["weighted_units"] += comp["total"]
        if rec.lane_id:
            week_bucket["lanes"].add((rec.lane_project, rec.lane_id))

        # By lane (disambiguated by (project, lane_id) across projects)
        if rec.lane_id:
            lane_meta = lane_index.get_lane(rec.lane_id, rec.lane_project) if lane_index else None
            lane_proj = rec.lane_project or (lane_meta.project if lane_meta else "")
            lane_key = (lane_proj, rec.lane_id)
            lane_row = by_lane.setdefault(lane_key, {
                "lane_id": rec.lane_id,
                "project": lane_proj,
                "status": lane_meta.status if lane_meta else "unknown",
                "requests": 0,
                "weighted_units": 0.0,
                "turns_by_role": {},
                "providers": set(),
            })
            lane_row["requests"] += 1
            lane_row["weighted_units"] += comp["total"]
            tbr: dict[str, int] = lane_row["turns_by_role"]
            tbr[rec.agent_role] = tbr.get(rec.agent_role, 0) + 1
            lane_row["providers"].add(rec.provider)

        # Cache misses
        if rec.cache_miss:
            miss_count += 1
            miss_weighted += comp["total"]
            reason = rec.cache_miss_reason or "unknown"
            miss_by_reason[reason] = miss_by_reason.get(reason, 0) + 1
            miss_by_role[rec.agent_role] = miss_by_role.get(rec.agent_role, 0) + 1

        # Turn patterns
        if rec.is_wait_turn:
            wait_turns += 1
            for tool in rec.tools:
                wait_by_tool[tool] = wait_by_tool.get(tool, 0) + 1
        elif rec.is_small_read_turn:
            small_read_turns += 1

    # Finalize shares, percentiles, and JSON-serializable sets.
    for prov in by_provider.values():
        prov["share_pct"] = _pct(prov["weighted_units"], w_total)
        prov["weighted_units"] = round(prov["weighted_units"], 2)

    for me in by_model_effort.values():
        me["share_pct"] = _pct(me["weighted_units"], w_total)
        me["weighted_units"] = round(me["weighted_units"], 2)

    for role_name, rb in by_role.items():
        ctxs = role_contexts.get(role_name, [])
        rb["share_pct"] = _pct(rb["weighted_units"], w_total)
        rb["weighted_units"] = round(rb["weighted_units"], 2)
        rb["median_context"] = _percentile(ctxs, 0.5)
        rb["p90_context"] = _percentile(ctxs, 0.9)

    days_out: dict[str, dict[str, Any]] = {}
    for day in sorted(by_day):
        d = by_day[day]
        days_out[day] = {
            "requests": d["requests"],
            "weighted_units": round(d["weighted_units"], 2),
            "lanes": len(d["lanes"]),
        }

    weeks_out: dict[str, dict[str, Any]] = {}
    for week in sorted(by_week):
        w = by_week[week]
        weeks_out[week] = {
            "requests": w["requests"],
            "weighted_units": round(w["weighted_units"], 2),
            "lanes": len(w["lanes"]),
        }

    lanes_out: dict[str, dict[str, Any]] = {}
    landed_weights: list[float] = []
    landed_turns: list[int] = []
    lane_id_counts: dict[str, int] = {}
    for _proj, lid in by_lane:
        lane_id_counts[lid] = lane_id_counts.get(lid, 0) + 1
    for (lane_proj, lane_id), row in sorted(by_lane.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        out_key = f"{lane_proj}:{lane_id}" if lane_id_counts[lane_id] > 1 and lane_proj else lane_id
        row_out = {
            "lane_id": row["lane_id"],
            "project": row["project"],
            "status": row["status"],
            "requests": row["requests"],
            "weighted_units": round(row["weighted_units"], 2),
            "turns_by_role": dict(sorted(row["turns_by_role"].items())),
            "providers": sorted(row["providers"]),
        }
        lanes_out[out_key] = row_out
        if row["status"] in ("landed", "released", "done"):
            landed_weights.append(row["weighted_units"])
            landed_turns.append(row["requests"])

    # Validate timestamp ordering for time bounds metadata.
    times = [t for r in records if (t := parse_timestamp(r.time)) is not None]
    return {
        "v": 1,
        "filters": {"since": since, "until": until, "project": project},
        "pricing_source": pricing.source,
        "window": {
            "first": min(times).strftime("%Y-%m-%dT%H:%M:%SZ") if times else None,
            "last": max(times).strftime("%Y-%m-%dT%H:%M:%SZ") if times else None,
        },
        "totals": {
            "requests": total_requests,
            "input_tokens": total_input,
            "output_tokens": total_output,
            "cache_read_tokens": total_cread,
            "cache_write_tokens": total_cwrite,
            "weighted_units": round(w_total, 2),
            "weighted_breakdown": {
                "input_pct": _pct(w_input, w_total),
                "output_pct": _pct(w_output, w_total),
                "cache_read_pct": _pct(w_cread, w_total),
                "cache_write_pct": _pct(w_cwrite, w_total),
            },
            "median_context": _percentile(contexts, 0.5),
            "p90_context": _percentile(contexts, 0.9),
        },
        "by_provider": dict(sorted(by_provider.items())),
        "by_model_effort": dict(sorted(by_model_effort.items())),
        "by_role": dict(sorted(by_role.items())),
        "by_day": days_out,
        "by_week": weeks_out,
        "by_lane": lanes_out,
        "per_landed_lane": {
            "count": len(landed_weights),
            "avg_weighted_units": round(sum(landed_weights) / len(landed_weights), 2) if landed_weights else 0.0,
            "avg_turns": round(sum(landed_turns) / len(landed_turns), 2) if landed_turns else 0.0,
        },
        "cache_misses": {
            "count": miss_count,
            "weighted_units": round(miss_weighted, 2),
            "share_pct": _pct(miss_weighted, w_total),
            "by_reason": dict(sorted(miss_by_reason.items())),
            "by_role": dict(sorted(miss_by_role.items())),
        },
        "turn_patterns": {
            "waiting_turns": wait_turns,
            "waiting_pct": _pct(wait_turns, total_requests),
            "waiting_by_tool": dict(sorted(wait_by_tool.items())),
            "small_read_turns": small_read_turns,
            "small_read_pct": _pct(small_read_turns, total_requests),
        },
    }


def format_report(report: dict[str, Any]) -> str:
    """Render a concise human-readable report."""
    totals = report["totals"]
    wb = totals["weighted_breakdown"]
    lines = [
        f"Requests: {totals['requests']}  Weighted: {totals['weighted_units']:,.1f} "
        f"(cache_read {wb['cache_read_pct']}%, cache_write {wb['cache_write_pct']}%, "
        f"output {wb['output_pct']}%, input {wb['input_pct']}%)",
        f"Context: median {totals['median_context']:,}  p90 {totals['p90_context']:,}  "
        f"(pricing: {report['pricing_source']})",
    ]
    if report["by_provider"]:
        lines.append("Providers:")
        for name, row in report["by_provider"].items():
            lines.append(f"  {name}: {row['requests']} reqs, {row['weighted_units']:,.1f} ({row['share_pct']}%)")
    if report["by_role"]:
        lines.append("Roles:")
        for role, row in report["by_role"].items():
            lines.append(
                f"  {role}: {row['requests']} reqs, {row['share_pct']}%, "
                f"median {row['median_context']:,} / p90 {row['p90_context']:,}"
            )
    misses = report["cache_misses"]
    lines.append(
        f"Cache misses: {misses['count']} ({misses['share_pct']}% of usage) "
        f"reasons={misses['by_reason'] or '{}'}"
    )
    tp = report["turn_patterns"]
    lines.append(
        f"Turn patterns: waiting {tp['waiting_turns']} ({tp['waiting_pct']}%), "
        f"small_read {tp['small_read_turns']} ({tp['small_read_pct']}%)"
    )
    landed = report["per_landed_lane"]
    if landed["count"]:
        lines.append(
            f"Per landed lane ({landed['count']}): avg {landed['avg_weighted_units']:,.1f} units, "
            f"avg {landed['avg_turns']} turns"
        )
    return "\n".join(lines)
