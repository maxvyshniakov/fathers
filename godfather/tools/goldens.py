#!/usr/bin/env python3
"""The screens every later change is compared against: Slotfather's recorded scene drawn by Godfather.

    python3 tools/goldens.py             write tests/fixtures/scene/ and tests/goldens/ again
    python3 tools/goldens.py --check     exit 1 when a golden differs (tests/test_goldens.py does the same)

tests/fixtures/scene/ is a GODFATHER_FIXTURES directory: the scene's `status --json --full` reply as the Slotfather
adapter reads it, capabilities and a `settings --json` reply, with scene.json holding the frozen moment and the lane
summaries the scene recorded, and CHANGELOG.md the changelog the changelog screen shows, so a new entry in the real
one changes no golden. tests/goldens/ holds, for each screen, the plain text (`.txt`) and the exact escape
sequences in the owner's two themes (`.seti.ansi`, Seti dark; `.prolight.ansi`, Pro Light): the overview wide
(156×43), at 100 and at 76 columns (one column), short (156×20 and 100×30), the help and Settings overlays, and what's new
(fathers#102); and, from tests/fixtures/expected/ (the scene with lease ids, expected runs and an event log of an
outlier and slower trends, fathers#108), the overview at 100 columns with an overrun lease and with a waiter
selected; and the overview with Tab's focus on EVENTS, and at 76 columns on DETAILS (fathers#139).
tests/fixtures/news/ is what the tools answer for what's new (`changelog --json`, or only their
version); the overview screens have everything read (the footer muted), the `unread-` ones and what's new have
Slotfather's newest release unread.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Iterator, cast
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
SCENE = ROOT / "tests" / "fixtures" / "scene"
EXPECTED = ROOT / "tests" / "fixtures" / "expected"  # the scene with expected runs, outliers and trends (fathers#108)
LOCKS = ROOT / "tests" / "fixtures" / "slot-locks"  # Slotfather's lock and head reservation log (fathers#162)
MIXED = ROOT / "tests" / "fixtures" / "mixed"  # the scene with Lanefather's and junkfather's event logs (fathers#84)
LANES = ROOT / "tests" / "fixtures" / "lanes"  # mixed, with Lanefather's status snapshot (fathers#85)
PROCS = ROOT / "tests" / "fixtures" / "procs"  # lanes, with Junkfather's status snapshot (fathers#86)
NEWS = ROOT / "tests" / "fixtures" / "news"
GOLDENS = ROOT / "tests" / "goldens"
COMMIT = "9f600e1c0ffee"  # Godfather's own install commit in what's new, so a commit changes no golden
THEMES = {"seti": ("#111213", "#cacecd"), "prolight": ("#ffffff", "#191919")}  # tools/raster.py THEMES
SCREENS = {  # name: (width, height, overlay); what's new's filter after a colon
    "slot-lock-log-120x7": (120, 7, "slot-locks"),
    "overview-156x43": (156, 43, None), "overview-100x40": (100, 40, None), "overview-76x40": (76, 40, None),
    "overview-156x20": (156, 20, None),
    "overview-100x30": (100, 30, None),  # fathers#169: one column short of rows, the bottom row given way first
    "help-156x43": (156, 43, "help"), "settings-156x43": (156, 43, "settings"),
    "settings-mixed-156x43": (156, 43, "settings:mixed"),  # fathers#87: every tool's section
    "unread-156x43": (156, 43, None), "unread-100x40": (100, 40, None), "unread-76x40": (76, 40, None),
    "whatsnew-156x43": (156, 43, "changelog"), "whatsnew-76x40": (76, 40, "changelog"),
    "whatsnew-godfather-156x43": (156, 43, "changelog:godfather"),
    # fathers#108: an overrun lease selected, then a waiter with an expected run; EVENTS with an outlier and trends
    "expected-100x40": (100, 40, "expected:0"), "expected-waiting-100x40": (100, 40, "expected:3"),
    # fathers#139: Tab's focus on EVENTS, and at 76 columns (no EVENTS there) on DETAILS
    "focus-events-156x43": (156, 43, "focus:events"), "focus-details-76x40": (76, 40, "focus:details"),
    # fathers#152: EVENTS focused, its cursor on a row and DETAILS showing it: the newest run, a run with an outlier
    # and a slower trend, and (mixed) a Lanefather event
    "focus-events-100x40": (100, 40, "focus:events"), "focus-run-156x43": (156, 43, "focus:events:expected:1"),
    "focus-run-100x40": (100, 40, "focus:events:expected:0"),
    "focus-lane-156x43": (156, 43, "focus:events:mixed-all:2"),
    # fathers#84: EVENTS with every source, wide, at 100 and at 76 columns; filtered to Lanefather while "710" is typed
    "mixed-156x43": (156, 43, "mixed"), "mixed-100x40": (100, 40, "mixed"), "mixed-76x40": (76, 40, "mixed"),
    "mixed-filter-156x43": (156, 43, "mixed:lanefather:710"),
    # fathers#85: Lanefather's lanes: the Lanes page wide and in one column, the overview with the lanes summary and
    # DETAILS' lane link, wide and at 76 columns (the summary narrowed)
    "lanes-156x43": (156, 43, "lanes:page"), "lanes-100x40": (100, 40, "lanes:page"),
    "lanes-overview-156x43": (156, 43, "lanes:overview"), "lanes-overview-76x40": (76, 40, "lanes:overview"),
    # fathers#86: Junkfather's processes: the Processes page wide and in one column, the overview with both summaries
    # and DETAILS' processes, and the Lanes page with a lane's processes
    "procs-156x43": (156, 43, "procs:processes"), "procs-100x40": (100, 40, "procs:processes"),
    "procs-overview-156x43": (156, 43, "procs:overview"), "procs-lanes-156x43": (156, 43, "procs:lanes"),
    # fathers#152: each log the title switches to, wide and at 100 columns (76 draws no EVENTS)
    **{f"mixed-{name}-{width}x{height}": (width, height, f"mixed:{tool}")
       for name, tool in (("lanes", "lanefather"), ("procs", "junkfather"), ("all", "all"))
       for width, height in ((156, 43), (100, 40))},
}


def scene_news() -> tuple[Any, dict[str, str], dict[str, str]]:
    """What's new as the fixture tools answer it, with everything read, and with Slotfather's newest unread."""
    from godfather import news

    with mock.patch.dict(os.environ, {"GODFATHER_FIXTURES": str(NEWS)}):
        found = news.load((SCENE / "CHANGELOG.md").read_text(), stale=False, commit=COMMIT)
    read = news.mark_read(found, {})
    return found, read, dict(read, slotfather="0.1.0")


def load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stamp(value: Any) -> Any:
    if isinstance(value, dt.datetime):
        return value.astimezone(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    if isinstance(value, dict):
        return {str(key): stamp(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [stamp(item) for item in value]
    return value


def write_scene() -> None:
    """The demo scene, frozen, as the fixture replies Slotfather would give (docs/contracts/slotfather.md)."""
    demo = load(ROOT / "tools" / "dashboard-demo.py", "godfather_demo")
    fake = load(ROOT / "tests" / "fake_slotfather.py", "fake_slotfather")
    _, model = demo.load_scene(advance=False, recorded=True)
    block = stamp({key: value for key, value in model.items() if key not in ("installed", "history_window")})
    lanes = {}
    for item in block["holders"] + block["waiters"]:
        lanes[lane_key(item["key"])] = item.pop("lane", None)
        item.pop("style", None)
    snapshot = {"v": 2, "host": "mbp", "time": block["updated"], "slots": block["slots"], "free": block["free"],
                "held": block["used"], "draining": 0, "dashboard": block}
    state = fake_state({"cores": 12, "memory_gib": 64, "capacity": {"slots": block["slots"]}, "order": {},
                        "writable": True, "environment": ["SLOTFATHER_CORES", "SLOTFATHER_MEMORY_GIB"]})
    block.update(v=1, draining=0, basis=fake.basis(state))
    capabilities = {"v": 1, "tool": "slotfather", "version": "0.2.0", "dashboard": {
        "v": 1, "tag": "slot", "snapshot": {"argv": ["status", "--json", "--full"], "versions": [2], "poll": 1},
        "events": {"path": "/fixture/events.jsonl", "versions": [1]},
        "settings": {"versions": [1]}, "actions": ["reprioritize"]}}
    (SCENE / "slotfather").mkdir(parents=True, exist_ok=True)
    # EVENTS reads the event log (fathers#84): the scene's grants and releases as Slotfather logs them
    live = {item["pid"]: (item["since"], item["granted"]) for item in block["holders"]}
    log = replay().lines(block["events"], live)
    (SCENE / "slotfather" / "events.jsonl").write_text("\n".join(log) + "\n")
    (SCENE / "host.json").write_text(json.dumps({"host": "mbp"}) + "\n")  # the host its event log is read for
    (SCENE / "slotfather" / "replies.json").write_text(json.dumps({
        "capabilities --json": {"code": 0, "json": capabilities},
        "status --json --full": {"code": 0, "json": snapshot},
        "settings --json": {"code": 0, "json": fake.settings(state)}}, indent=1) + "\n")
    (SCENE / "CHANGELOG.md").write_text((ROOT / "CHANGELOG.md").read_text())
    (SCENE / "scene.json").write_text(json.dumps({"moment": block["updated"], "history_window": 60,
                                                  "lanes": lanes}, indent=1) + "\n")


def replay() -> Any:
    """tests/replay.py: a recorded scene's grants and releases as Slotfather's event-log lines."""
    return load(ROOT / "tests" / "replay.py", "godfather_replay")


def lane_key(key: Any) -> str:
    """scene.json's key of a row's lane: its key as JSON, a [pid, since] pair as a list."""
    return json.dumps(list(cast(list[Any], key)) if isinstance(key, (list, tuple)) else key)


def write_expected() -> None:
    """tests/fixtures/expected/: the scene as a Slotfather with fathers#101 and fathers#106 answers it: `entries` with
    lease ids and expected runs (one lease past its p90, one waiter with an expected run), and its event log with
    lease ids, an outlier and two slower trends."""
    replies = json.loads((SCENE / "slotfather" / "replies.json").read_text())
    snapshot = replies["status --json --full"]["json"]
    block = snapshot["dashboard"]
    gate = {"key": "straylight · gate", "p50": 1080.0, "p90": 1860.0, "runs": 23, "fallback": False}
    expected = {10000: (gate, True), 10001: (gate, False),
                10003: ({"key": "prof51 · bash", "p50": 2400.0, "p90": 3300.0, "runs": 8, "fallback": True}, None)}
    snapshot["entries"] = []
    for item in block["holders"] + block["waiters"]:
        pid, since = item["key"]
        entry: dict[str, Any] = {"pid": pid, "since": since, "lease_id": f"lease-{pid}", "expected": None}
        if pid in expected:
            entry["expected"], overrun = expected[pid]
            if overrun is not None:
                entry["overrun"] = overrun
        snapshot["entries"].append(entry)
    def line(kind: str, time: str, key: str, details: dict[str, Any], level: str) -> str:
        return json.dumps({"v": 1, "time": time, "host": "mbp", "source": "slotfather", "kind": kind,
                           "subject": {"type": "run_type", "id": key}, "actor": {"type": "tool"},
                           "details": details, "level": level})

    log = [line("run.trend", "2026-11-04T09:52:30.400Z", "fathers · check · plan 3f2a",
                {"verdict": "slower", "p50_recent": 7.0, "p50_before": 5.0, "ratio": 1.4, "recent": 10,
                 "before": 30, "plan_change": {"checks": 2}, "why": "plan grew: +2 checks"}, "warn"),
           line("run.outlier", "2026-11-04T09:59:35.300Z", "straylight · gate",
                {"lease_id": "lease-21201", "ran": 73.0, "p50": 41.0, "p90": 52.0,
                 "ended": "2026-11-04T09:58:33Z", "later": [40.0, 44.0, 39.0]}, "warn"),
           line("run.trend", "2026-11-04T09:59:35.500Z", "straylight · gate",
                {"verdict": "slower", "p50_recent": 341.0, "p50_before": 258.0, "ratio": 1.32, "recent": 10,
                 "before": 30, "plan_change": None, "why": "same plan"}, "warn")]
    live = {item["pid"]: (item["since"], item["granted"]) for item in block["holders"]}
    def lease(pid: int) -> str:
        return f"lease-{pid}"

    queue = replay().lines(block["events"], live, lease=lease)
    log = sorted(queue + log, key=lambda text: json.loads(text)["time"])  # stable: a run event after its release
    EXPECTED.mkdir(parents=True, exist_ok=True)
    (EXPECTED / "slotfather").mkdir(exist_ok=True)
    (EXPECTED / "slotfather" / "replies.json").write_text(json.dumps(replies, indent=1) + "\n")
    (EXPECTED / "slotfather" / "events.jsonl").write_text("\n".join(log) + "\n")
    (EXPECTED / "host.json").write_text(json.dumps({"host": "mbp"}) + "\n")  # the host its event log is read for
    scene = json.loads((SCENE / "scene.json").read_text())
    scene["lanes"] = {lane_key(f"lease-{json.loads(key)[0]}"): lane for key, lane in scene["lanes"].items()}
    (EXPECTED / "scene.json").write_text(json.dumps(scene, indent=1) + "\n")


def write_mixed() -> None:
    """tests/fixtures/mixed/: the scene with Lanefather and junkfather present, each answering capabilities with its
    event log, and logs of a lane's start, check, land, smoke and review, a red batch gate, and owned, reported and
    reaped processes in the scene's last ten minutes (fathers#84)."""
    import shutil

    shutil.rmtree(MIXED, ignore_errors=True)
    shutil.copytree(SCENE, MIXED, ignore=shutil.ignore_patterns("CHANGELOG.md"))

    def line(source: str, kind: str, time: str, subject: dict[str, Any], details: dict[str, Any],
             level: str | None = None) -> str:
        event = {"v": 1, "time": f"2026-11-04T{time}.000Z", "host": "mbp", "source": source, "kind": kind,
                 "subject": subject, "actor": {"type": "agent", "session": "4bceaaaf"}, "details": details}
        if level:
            event["level"] = level
        return json.dumps(event, sort_keys=True)

    lane = {"type": "lane", "id": "710-speech-paste-phase", "ticket": "710", "project": "Straylight"}
    review = {"type": "lane", "id": "644-build-once", "ticket": "644", "project": "Straylight"}
    commit = {"type": "commit", "id": "cedf8b972dd8ed11d8af0725f0a9bcb5b2d0ba35", "project": "Straylight"}
    lanes = [
        line("lanefather", "lane.started", "09:50:12", lane, {"status": "active", "base": "integrate/queue-347",
                                                              "claims": ["desktop/src/**", "docs/**"]}),
        line("lanefather", "lane.checked", "09:53:40", lane, {"status": "green", "suites": ["tooling", "native"],
                                                              "checks": 14, "reused": 3, "jobs": 2}),
        line("lanefather", "lane.landed", "09:56:05", lane, {"onto": "integrate/queue-347", "merge": "cedf8b97"}),
        line("lanefather", "lane.post_merge", "09:57:48", lane, {"status": "green", "scope": "narrowed", "jobs": 2}),
        line("lanefather", "gate.finished", "09:58:10", commit, {"status": "red", "jobs": 1, "seconds": 1177},
             "error"),
        line("lanefather", "review.finished", "09:59:02", review, {"status": "finished", "findings": 2,
                                                                   "seconds": 540})]
    entry = {"type": "entry", "id": "j3f9a1c", "label": "vite dev", "pid": 90412}
    junk = [
        line("junkfather", "entry.owned", "09:51:30", entry, {"argv": ["bun", "run", "dev"], "ttl": 7200,
                                                              "keep": "owner", "mode": "detached"}),
        line("junkfather", "unowned.reported", "09:54:20", {"type": "process", "id": "88123", "pid": 88123},
             {"cmd": "node storybook dev", "why": "orphan", "age": 5400}, "warn"),
        line("junkfather", "entry.reaped", "09:57:00", {"type": "entry", "id": "j1c2d3e", "label": "simulator",
                                                         "pid": 87001}, {"reason": "ttl expired", "result": "stopped"}),
        line("junkfather", "entry.extended", "09:59:20", entry, {"ttl": 14400})]
    tools = load(ROOT / "tests" / "fake_tool.py", "fake_tool")  # each tool's settings as the real one prints them
    environments = {"lanefather": {"LANEFATHER_PRIORITY_CHECK": "batch"}, "junkfather": {}}
    for tool, tag, log in (("lanefather", "lane", lanes), ("junkfather", "junk", junk)):
        (MIXED / tool).mkdir()
        capabilities = {"v": 1, "tool": tool, "version": "0.9.0", "dashboard": {
            "v": 1, "tag": tag, "snapshot": {"argv": ["status", "--json"], "versions": [1], "poll": 5},
            "events": {"path": f"/fixture/{tool}/events.jsonl", "versions": [1]}, "settings": {"versions": [1]}}}
        settings = tools.document({"tool": tool, "values": {}, "environment": environments[tool], "writable": True})
        (MIXED / tool / "replies.json").write_text(json.dumps({"capabilities --json": {"code": 0, "json": capabilities},
                                                               "settings --json": {"code": 0, "json": settings}},
                                                              indent=1) + "\n")
        (MIXED / tool / "events.jsonl").write_text("\n".join(log) + "\n")


def write_lanes() -> None:
    """tests/fixtures/lanes/: the mixed scene with Lanefather's `status --json` (fathers#85): Straylight with four
    lanes and a red batch gate, data-grid with a lane of the same id as one of Straylight's, sid-kit whose config is
    gone. The first lease names its lane and Straylight's state directory; the last waiter only its lane, from inside
    that lane's worktree; the second names a lane of no project here. Lanefather advertises no `show`."""
    import shutil

    shutil.rmtree(LANES, ignore_errors=True)
    shutil.copytree(MIXED, LANES)
    straylight, grid = "/fixture/straylight/lanes", "/fixture/data-grid/lanes"
    replies = json.loads((LANES / "slotfather" / "replies.json").read_text())
    block = replies["status --json --full"]["json"]["dashboard"]
    for item in block["holders"] + block["waiters"]:
        if item["pid"] == 10000:
            item["context"] = dict(item["context"], **{"lanefather.state": straylight})
        if item["pid"] == 10004:
            item["workdir"] = "/fixture/straylight/worktrees/555-host-rename-names/desktop"
        if item["pid"] == 10001:
            item["context"] = dict(item["context"], lane="471-missing-lane")
    (LANES / "slotfather" / "replies.json").write_text(json.dumps(replies, indent=1) + "\n")

    def lane(ident: str, status: str, receipt: str, match: Any, review: str, open_: int, findings: int,
             created: str, claims: int, post: Any = None, ticket: Any = None) -> dict[str, Any]:
        number = ticket if ticket is not None else ident.split("-", 1)[0]
        return {"id": ident, "status": status,
                "ticket": {"number": number, "url": f"https://github.com/maxvyshniakov/straylight/issues/{number}"},
                "branch": f"lane/{ident}", "base": "integrate/queue-347",
                "worktree": f"/fixture/straylight/worktrees/{ident}",
                "claims": [f"src/part{n}/**" for n in range(claims)], "after": [], "created": created,
                "receipt": {"status": receipt, "tree_match": match, "path": None}, "post_merge": post,
                "review": {"status": review, "open": open_, "findings": findings},
                "logs": {"check": f"/fixture/straylight/lanes/logs/lane-{ident}-20261104T0950{claims}0.log"},
                "outcome": None}

    status = {"v": 1, "host": "mbp", "time": "2026-11-04T09:59:39Z", "registry": "/fixture/projects.json",
              "projects": [
                  {"project": "Straylight", "state_dir": straylight, "config": "/fixture/straylight/.lanefather.toml",
                   "repository": "maxvyshniakov/straylight", "cap": 4, "cap_source": "derived", "active": 3,
                   "planned": 1, "gate": {"sha": "cedf8b972dd8", "status": "red", "at": "2026-11-04T09:58:10Z"},
                   "lanes": [lane("644-build-once", "active", "in progress", None, "not started", 0, 0,
                                  "2026-11-04T09:04:00Z", 11),
                             lane("710-speech-paste-phase", "active", "green", True, "running", 2, 3,
                                  "2026-11-04T08:12:00Z", 2, post="green"),
                             lane("612-overlay-keys", "active", "red", False, "finished", 0, 1,
                                  "2026-11-03T16:40:00Z", 4),
                             lane("555-host-rename-names", "planned", "not run", None, "not started", 0, 0,
                                  "2026-11-04T09:50:00Z", 1)], "damaged": []},
                  {"project": "data-grid", "state_dir": grid, "config": "/fixture/data-grid/.lanefather.toml",
                   "repository": "maxvyshniakov/data-grid", "cap": 2, "cap_source": "derived", "active": 1,
                   "planned": 0, "gate": {"sha": "0a1b2c3d", "status": "green", "at": "2026-11-04T07:00:00Z"},
                   "lanes": [dict(lane("644-build-once", "active", "red", False, "not started", 0, 0,
                                       "2026-11-04T09:30:00Z", 3, ticket="88"),
                                  worktree="/fixture/data-grid/worktrees/644-build-once")], "damaged": []},
                  {"project": "sid-kit", "state_dir": "/fixture/sid-kit/lanes",
                   "error": "config not found at /fixture/sid-kit/.lanefather.toml"}]}
    lanefather = json.loads((LANES / "lanefather" / "replies.json").read_text())
    lanefather["status --json"] = {"code": 0, "json": status}
    (LANES / "lanefather" / "replies.json").write_text(json.dumps(lanefather, indent=1) + "\n")
    scene = json.loads((LANES / "scene.json").read_text())
    scene["lanes"] = {key: {"error": "lanefather too old"} for key in scene["lanes"]}  # no `show` to ask
    (LANES / "scene.json").write_text(json.dumps(scene, indent=1) + "\n")


def write_procs() -> None:
    """tests/fixtures/procs/: the lanes scene with Junkfather's `status --json` (fathers#86): a dev server owned in
    lane 710's worktree, a storybook in the first lease's process group, an orphaned Playwright server, an expired
    Docker resource and a running simulator; the last sweep's unowned process, worktree and simulator."""
    import shutil

    shutil.rmtree(PROCS, ignore_errors=True)
    shutil.copytree(LANES, PROCS)

    def entry(ident: str, label: str, pid: Any, ttl_left: float, alive: bool = True, lane: Any = None,
              cwd: str = "/fixture/straylight", pgid: Any = None, resource: Any = None, age: float = 1800,
              argv: Any = None) -> dict[str, Any]:
        return {"id": ident, "state": "running", "owner": "claude 4bceaaaf/a71c", "session": "4bceaaaf",
                "agent": "a71c", "lane": lane, "owner_alive": alive, "age": age, "ttl": 7200, "ttl_left": ttl_left,
                "keep": "owner", "pid": pid, "pgid": pgid or pid, "processes": None if resource else 3,
                "label": label, "mode": "resource" if resource else "detached", "cwd": cwd,
                "argv": argv if argv is not None else (None if resource else ["bun", "run", label.split()[0]]),
                "log": f"/fixture/junkfather/logs/{ident}.log", "end": None, "exit": None, "resource": resource}

    status: dict[str, Any] = {"v": 2, "host": "mbp", "time": "2026-11-04T09:59:39Z", "entries": [
        entry("j3f9a1c", "vite dev", 90412, 4000, lane="710-speech-paste-phase",
              cwd="/fixture/straylight/worktrees/710-speech-paste-phase", argv=["bun", "run", "dev"]),
        entry("j5e6f7a", "storybook", 55001, 6400, pgid=10000, argv=["bun", "run", "storybook"]),
        entry("j7a8b9c", "playwright server", 61200, 3000, alive=False, age=5400,
              argv=["npx", "playwright", "run-server"]),
        entry("j2b3c4d", "pg-test", None, -300, age=7500,
              resource={"kind": "docker", "id": "1a2b3c4d5e6f", "name": "pg-test"}),
        entry("j8d9e0f", "iPhone 17 Pro", None, 5000,
              resource={"kind": "simulator", "id": "1A2B3C4D-5E6F-7081-92A3-B4C5D6E7F809", "name": "iPhone 17 Pro"})],
        "unowned": [{"pid": 88123, "start": "Tue Nov  4 08:29:00 2026", "cmd": "node storybook dev -p 6006",
                     "cwd": "/fixture/straylight/web", "root": "/fixture/straylight", "why": "orphan", "age": 5400,
                     "alive": True}],
        "unowned_resources": [
            {"kind": "worktree", "id": "/fixture/straylight/worktrees/old-spike", "name": "old-spike",
             "why": "merged, idle 9d", "repo": "/fixture/straylight", "branch": "lane/old-spike", "age": 777600,
             "size": 2.3e9},
            {"kind": "simulator", "id": "9F8E7D6C-0000-4000-8000-0123456789AB", "name": "iPad Air",
             "why": "booted, no owner"}],
        "sweep": {"time": "2026-11-04T09:30:00Z", "result": {"reaped": [], "failed": []}}, "cpu": None, "errors": []}
    junk = json.loads((PROCS / "junkfather" / "replies.json").read_text())
    junk["status --json"] = {"code": 0, "json": status}
    junk["capabilities --json"]["json"]["dashboard"]["snapshot"]["versions"] = [1, 2]
    junk["capabilities --json"]["json"]["dashboard"]["actions"] = ["reap", "extend"]
    (PROCS / "junkfather" / "replies.json").write_text(json.dumps(junk, indent=1) + "\n")
    # fathers#88: every row's actions advertised; the requests carry their holders' start, the second lease yields
    # for the interactive waiter
    slot = json.loads((PROCS / "slotfather" / "replies.json").read_text())
    slot["capabilities --json"]["json"]["dashboard"]["actions"] = ["reprioritize", "ask-yield"]
    block = slot["status --json --full"]["json"]["dashboard"]
    for item in block["holders"] + block["waiters"]:
        item["start"] = f"Tue Nov  4 08:{item['pid'] % 60:02d}:00 2026"
        if item["pid"] == 10001:
            item.update(yieldable=True, priority="batch")
        if item["pid"] == 10003:
            item["priority"] = "interactive"
    (PROCS / "slotfather" / "replies.json").write_text(json.dumps(slot, indent=1) + "\n")
    lanes = json.loads((PROCS / "lanefather" / "replies.json").read_text())
    lanes["capabilities --json"]["json"]["dashboard"]["actions"] = ["park"]
    (PROCS / "lanefather" / "replies.json").write_text(json.dumps(lanes, indent=1) + "\n")


def fake_state(state: dict[str, Any]) -> dict[str, Any]:
    os.environ.pop("SLOTFATHER_QUEUE", None)  # the fake reads it as the environment's queue order
    return dict(state, version="0.2.0")


@contextlib.contextmanager
def environment(theme: str | None, scene: Path = SCENE) -> Iterator[None]:
    keep = {key: value for key, value in os.environ.items()
            if not key.startswith(("SLOTFATHER_", "GODFATHER_", "COLORFGBG", "NO_COLOR"))}
    keep.update(GODFATHER_FIXTURES=str(scene), TZ="Etc/GMT-2", HOME="/nonexistent")
    if theme:
        keep.update(GODFATHER_TERMINAL_BG=THEMES[theme][0], GODFATHER_TERMINAL_FG=THEMES[theme][1])
    with mock.patch.dict(os.environ, keep, clear=True):
        time.tzset()
        try:
            yield
        finally:
            pass
    time.tzset()


def render(name: str, theme: str | None) -> str:
    """One screen in one theme (None: plain text), as the live view would draw it at the scene's moment: the moment,
    the lanes and the chart's window go in as data, the theme and marks with the canvas, the commit what's new shows as
    an argument. No module is patched (fathers#138); the only thing set from outside is the process environment
    (environment(): the fixture folder, the time zone and the terminal's colours), which is what the tools read."""
    from godfather import app, entries, events, overlay_text, pages, state, theme as themes, tui
    from godfather.adapters import slotfather as adapter
    from godfather.views import common, overview

    width, height, overlay = SCREENS[name]
    selected, focus = 0, "requests"
    event = None
    if overlay and overlay.startswith("focus:"):
        focus, _, rest = overlay.partition(":")[2].partition(":")
        overlay = None
        if focus == "events":  # DETAILS follows EVENTS' cursor (fathers#152): on the newest row, or the one named
            folder_name, _, row = rest.partition(":")
            event = int(row or 0)
            if folder_name:
                overlay = folder_name
    if overlay == "expected" or overlay == "mixed-all":
        overlay = {"expected": "expected:0", "mixed-all": "mixed:all"}[overlay]
    folder, found_filter = SCENE, None
    if overlay and overlay.startswith("expected"):
        folder, selected, overlay = EXPECTED, int(overlay.partition(":")[2]) if event is None else 0, None
    if overlay == "settings:mixed":
        folder, overlay = MIXED, "settings"
    lock_log = overlay == "slot-locks"
    if lock_log:
        folder, overlay = LOCKS, None
    page = "overview"
    if overlay and overlay.startswith("lanes:"):  # fathers#85
        folder, page, overlay = LANES, "lanes" if overlay == "lanes:page" else "overview", None
    if overlay and overlay.startswith("procs:"):  # fathers#86
        folder, page, overlay = PROCS, overlay.partition(":")[2], None
    if overlay and overlay.startswith("mixed"):
        _, source, text = (overlay.split(":") + ["", ""])[:3]
        found_filter = events.Filter(source or None, text, editing=bool(text))
        folder, overlay = MIXED, None
    found, everything, unread = scene_news()
    read = everything if name.startswith(("overview-", "help-", "settings-", "expected", "focus-")) else unread
    scene = json.loads((folder / "scene.json").read_text())
    with environment(theme, folder), app.Screen() as screen:
        palette = themes.from_environment()
        model, error = screen.load()
        assert model is not None and error is None, error
        entries.dashboard_colours(model, {}, palette.holders())
        for item in model["holders"] + model["waiters"]:
            lane = scene["lanes"].get(lane_key(item["key"]))
            if lane:
                item["lane"] = lane
        model["history_window"] = scene["history_window"]
        moment = adapter.parse_time(scene["moment"])
        assert moment is not None
        if lock_log:
            from godfather.layout import Rect
            from godfather.views.events import draw_events

            canvas = tui.Canvas(width, height, palette, common.marks_for(False))
            draw_events(canvas, Rect(0, 0, height, width), model, moment)
            return canvas.render(theme is not None)
        shown: object = None
        if overlay == "settings":
            from godfather import settings_model

            shown = settings_model.opened(screen.settings_sections(), settings_model.Context(width=width,
                                                                                             height=height), 0.0)
        elif overlay and overlay.startswith("changelog"):
            tool = overlay.partition(":")[2] or "all"
            shown = state.Changelog(tab=overlay_text.filters(found).index(tool))
        elif overlay:
            shown = state.Help()
        tools = {name: screen.load_tool(name) for name in app.TOOL_ADAPTERS}  # the other tools as sources
        opened = pages.open_page(state.settle(state.AppState(model=model, tools=tools), 0.0)[0], page)
        canvas = tui.Canvas(width, height, palette, common.marks_for(False))
        canvas.focus = opened.focus if page != "overview" else focus
        overview.dashboard_draw(canvas, model, selected, shown, cwd="~/straylight", news=found, read=read,
                                moment=moment, events_filter=found_filter, event=event, tools=tools,
                                page=opened.page,
                                page_row=pages.page_index(opened, opened.page) if opened.page != "overview" else None)
        return canvas.render(theme is not None)


# fathers#156: MACHINE alone, for each reason the queue admits nothing (and one where it admits): its border says why
ADMISSION: dict[str, dict[str, Any]] = {
    "full": {"holders": [{"kind": "held", "label": "build", "count": 4}, {"kind": "held", "label": "gate", "count": 4}]},
    "whole-machine": {"holders": [{"kind": "held", "label": "dg-striped perf JS parity vs CSS parity", "count": 8}]},
    "queue-ahead": {"used": 6, "free": 2, "waiters": [{"kind": "waiting", "count": 4}]},
    "draining": {"slots": 4, "used": 6, "free": 0, "draining": 2},
    "memory": {"used": 2, "free": 6, "memory_block": "memory pressure: critical"},
    "memory-full": {"memory_block": "memory pressure: warning"},
    "open": {"admission": 2, "used": 6, "free": 2},
}


def admission_screen(name: str, theme: str | None) -> str:
    """MACHINE at 72x13 over a model whose queue admits nothing for one reason (ADMISSION)."""
    import datetime as dt

    from godfather import theme as themes, tui
    from godfather.layout import Rect
    from godfather.model import Dashboard
    from godfather.views import common, machine

    with environment(theme):
        palette = themes.from_environment()
        moment = dt.datetime(2026, 11, 16, 1, 30, tzinfo=dt.timezone.utc)
        model: dict[str, Any] = dict(
            slots=8, cores=12, averages=(6.3, 6.0, 6.0), admission=0, advice={"unleased_load": 0.2, "jobs": 1},
            memory_block=None, used=8, free=0, holders=[], waiters=[], draining=0,
            memory={"level": 1, "used": 768, "total": 1024, "swap_total": 1024, "swap_free": 800, "low": False,
                    "swap_low": False},
            history=[(moment - dt.timedelta(minutes=1), moment, 8)])
        model.update(ADMISSION[name])
        canvas = tui.Canvas(72, 13, palette, common.marks_for(False))
        machine.draw_machine(canvas, Rect(0, 0, 13, 72), cast(Dashboard, model))
        return canvas.render(theme is not None)


def goldens() -> dict[str, str]:
    found = {}
    for name in SCREENS:
        found[f"{name}.txt"] = render(name, None) + "\n"
        for theme in THEMES:
            found[f"{name}.{theme}.ansi"] = render(name, theme) + "\n"
    for name in ADMISSION:
        found[f"admission-{name}-72x13.txt"] = admission_screen(name, None) + "\n"
        for theme in THEMES:
            found[f"admission-{name}-72x13.{theme}.ansi"] = admission_screen(name, theme) + "\n"
    return found


# fathers#114: box drawing and blocks as tools/raster.py draws them, no text, so the pixels are the same everywhere
RASTER = ["╭─┬──╮ ▀▂▄", "│ │  ┃ ▇█🮅", "├─┼──┤ ▌▐░", "│ │  ╹ ▖▟▒", "╰─┴──╯ ╱╲┄"]


def raster_goldens() -> dict[str, Any]:
    """tests/goldens/raster/: the geometry frame in each theme, its lines in the theme's border grey."""
    tool = load(ROOT / "tools" / "raster.py", "godfather_raster")
    found: dict[str, Any] = {}
    for name in THEMES:
        palette = tool.THEMES[name]
        text = "\n".join("\x1b[0;38;2;90;92;93m" + line + "\x1b[0m" for line in RASTER)
        found[f"geometry.{name}.png"] = tool.Raster(10, len(RASTER), palette).frame(text)
    return found


def main(argv: list[str]) -> int:
    if argv == ["--check"]:
        bad = [name for name, text in goldens().items()
               if not (GOLDENS / name).is_file() or (GOLDENS / name).read_text() != text]
        for name in bad:
            print(f"differs: tests/goldens/{name}", file=sys.stderr)
        return 1 if bad else 0
    write_scene()
    write_expected()
    write_mixed()
    write_lanes()
    write_procs()
    GOLDENS.mkdir(parents=True, exist_ok=True)
    for name, text in goldens().items():
        (GOLDENS / name).write_text(text)
    try:
        pictures = raster_goldens()
    except ImportError:
        print("Pillow is missing: tests/goldens/raster/ left as it is (requirements-dev.txt)", file=sys.stderr)
        pictures = {}
    for name, image in pictures.items():
        (GOLDENS / "raster").mkdir(exist_ok=True)
        image.save(GOLDENS / "raster" / name, format="PNG")
    print(f"wrote {SCENE.relative_to(ROOT)} and {len(SCREENS) * 3 + len(pictures)} goldens in "
          f"{GOLDENS.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
