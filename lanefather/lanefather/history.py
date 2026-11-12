"""The run history (fathers#92): each check's start, end, status and lease, kept after the lane is done.

  <state>/history/<YYYY-MM>.jsonl   one JSON object per line, newest last; KEEP_MONTHS files are kept

A line is {"v": 1, "kind", "at", "lane", "phase", "tree", ...}:
- kind "check": one check of one verify run: check, suite, status, seconds, started, finished, run (the run id
  Lanefather gave the run, also in its slot lease's meta as `run`), lease (the lease id when verify's report names
  one), report;
- kind "flake": a check that failed and then passed on the same tree under the same environment: check, failed_at,
  passed_at, how ("rerun-alone" or "later-run"), tests (the failed JUnit test names when the check reported them).

`done` and `gc` leave the history alone; lanefather stats reads it.
"""
from __future__ import annotations

import datetime
import json
import os
from pathlib import Path
import statistics
from typing import Any
from xml.etree import ElementTree

from . import state
from .journal import now, parse_time

VERSION = 1
KEEP_MONTHS = 12


def directory() -> Path:
    path = state.state_dir() / "history"
    path.mkdir(exist_ok=True)
    return path


def append(kind: str, **fields: Any) -> None:
    """Append one line to this month's file; never fails the caller."""
    line = {"v": VERSION, "kind": kind, "at": now(), **{k: v for k, v in fields.items() if v is not None}}
    try:
        folder = directory()
        month = datetime.datetime.now(datetime.UTC).strftime("%Y-%m")
        with open(folder / f"{month}.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, sort_keys=True) + "\n")
        for old in sorted(folder.glob("*.jsonl"))[:-KEEP_MONTHS]:
            old.unlink(missing_ok=True)
    except OSError:
        return


def read(days: float | None = None) -> list[dict[str, Any]]:
    """Every line of the window (the last `days`, else everything kept), oldest first."""
    cutoff = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=days)) if days else None
    found: list[dict[str, Any]] = []
    try:
        files = sorted(directory().glob("*.jsonl"))
    except OSError:
        return []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for raw in text.splitlines():
            try:
                parsed: Any = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(parsed, dict):
                continue
            line: dict[str, Any] = parsed  # pyright: ignore[reportUnknownVariableType]
            if line.get("v") != VERSION:
                continue
            if cutoff is not None:
                try:
                    if parse_time(str(line.get("at"))) < cutoff:
                        continue
                except ValueError:
                    continue
            found.append(line)
    return found


def has_flake(check: str, tree: str, days: float = 60) -> bool:
    """Whether the history already marks the check a flake on the tree: one red-to-green is one flake."""
    return any(line["kind"] == "flake" and line.get("check") == check and line.get("tree") == tree
               for line in read(days))


def junit_failures(path: Any) -> list[str]:
    """The failed or errored test cases a JUnit XML report names, as classname.name; [] without a readable one."""
    if not path:
        return []
    try:
        root = ElementTree.parse(os.fspath(str(path))).getroot()
    except (OSError, ElementTree.ParseError):
        return []
    names: list[str] = []
    for case in root.iter("testcase"):
        if case.find("failure") is not None or case.find("error") is not None:
            names.append(".".join(part for part in (case.get("classname"), case.get("name")) if part))
    return names


def record_run(report: dict[str, Any] | None, *, lane: str | None, phase: str, tree: str | None, run: str,
               started: str, finished: str, seconds: float, checks: list[str] | None = None) -> None:
    """One "check" line per row of a verify report; a run without a report gets one line for the run."""
    given: list[Any] = (report or {}).get("checks", [])
    rows: list[dict[str, Any]] = [r for r in given if isinstance(r, dict) and r.get("id")]  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    lease = (report or {}).get("lease")
    if not rows:
        append("check", lane=lane, phase=phase, tree=tree, check=",".join(checks or []) or None, status="no report",
               seconds=seconds, started=started, finished=finished, run=run, lease=lease)
        return
    for row in rows:
        append("check", lane=lane, phase=phase, tree=tree, check=row["id"], status=row.get("status"),
               seconds=row.get("seconds"), started=row.get("started") or started,
               finished=row.get("finished") or finished, run=run, lease=row.get("lease") or lease,
               junit=row.get("junit"))


def flake(check: str, *, lane: str | None, tree: str, failed_at: str | None, how: str, junit: Any = None,
          phase: str | None = None) -> None:
    append("flake", lane=lane, phase=phase, tree=tree, check=check, failed_at=failed_at, passed_at=now(), how=how,
           tests=junit_failures(junit) or None)


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    return statistics.quantiles(ordered, n=100, method="inclusive")[round(fraction * 100) - 1]


def stats(days: float | None = 7, lane: str | None = None) -> dict[str, Any]:
    """Per check over the window: runs, failures, flakes, p50 and p90 seconds of its runs with a duration."""
    lines = [line for line in read(days) if lane is None or line.get("lane") == lane]
    table: dict[str, dict[str, Any]] = {}
    for line in lines:
        check = line.get("check")
        if not isinstance(check, str) or not check:
            continue
        if line["kind"] == "check" and (line.get("status") not in ("passed", "failed") or check.startswith(
                "environment:")):
            continue  # not run, blocked, a run without a report, or the environment's pseudo-check
        row = table.setdefault(check, {"check": check, "runs": 0, "failed": 0, "flakes": 0, "seconds": []})
        if line["kind"] == "check":
            row["runs"] += 1
            row["failed"] += line.get("status") == "failed"
            if isinstance(line.get("seconds"), (int, float)):
                row["seconds"].append(float(line["seconds"]))
        elif line["kind"] == "flake":
            row["flakes"] += 1
    checks: list[dict[str, Any]] = []
    for row in table.values():
        seconds: list[float] = row.pop("seconds")
        checks.append(dict(row, p50=round(percentile(seconds, 0.5), 3) if seconds else None,
                           p90=round(percentile(seconds, 0.9), 3) if seconds else None,
                           total_seconds=round(sum(seconds), 3)))
    checks.sort(key=lambda r: (-(r["total_seconds"] or 0), r["check"]))
    return {"v": VERSION, "days": days, "lane": lane, "lines": len(lines), "checks": checks}


def command(args: Any) -> int:
    report = stats(None if args.days == 0 else args.days, args.lane)
    if args.json:
        print(json.dumps(report))
        return 0
    window = "everything kept" if not report["days"] else f"the last {report['days']:g} days"
    print(f"Checks over {window}" + (f", lane {args.lane}" if args.lane else "") + ":")
    if not report["checks"]:
        print("  no recorded runs")
    for row in report["checks"]:
        p50 = "-" if row["p50"] is None else f"{row['p50']:.1f}s"
        p90 = "-" if row["p90"] is None else f"{row['p90']:.1f}s"
        print(f"  {row['check']}: {row['runs']} runs, p50 {p50}, p90 {p90}, {row['failed']} red, "
              f"{row['flakes']} flakes")
    return 0
