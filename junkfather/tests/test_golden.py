"""The entries, events and output the commands write for fixed scenarios, compared with tests/golden/*.json.

The golden files were written by the release before the architecture tidy-up (fathers#76, at 0d14d9e), so a
refactor that changes a byte of an entry, an event, a refusal or a dashboard snapshot fails here. Volatile values are
masked: times, pids, start times, entry ids, the host and the scratch directory. A deliberate change regenerates a
file with JUNKFATHER_GOLDEN_WRITE=1 and says why in its commit.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Dict, List
import unittest
from unittest import mock

from support import ROOT, Case, proc, table

from junkfather import changelog, events, procs, sweep

GOLDEN = Path(__file__).resolve().parent / "golden"
VOLATILE = {"age", "ttl_left", "at", "time", "created", "expires", "ended"}
START = re.compile(r"\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) +\d+ "
                   r"\d\d:\d\d:\d\d \d{4}\b")
ISO = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z")
EPOCH = re.compile(r"\b1\d{9}(\.\d+)?\b")
ENTRY = re.compile(r"\bj[0-9a-f]{6}\b")


class Recorder:
    """Collects a scenario's steps and masks what differs between runs."""

    def __init__(self, case: "GoldenTests"):
        self.case = case
        self.steps: List[Dict[str, Any]] = []
        self.pids: List[int] = []

    def pid(self, pid: int) -> int:
        if pid not in self.pids:
            self.pids.append(pid)
        return pid

    def run(self, *args: str, env: Dict[str, str]) -> subprocess.CompletedProcess:
        result = self.case.cli(*args, env=env)
        self.steps.append({"args": list(args), "code": result.returncode, "out": result.stdout,
                           "err": result.stderr})
        return result

    def snapshot(self, name: str) -> None:
        entries = sorted(self.case.store.entries(), key=lambda e: (e.get("created") or 0, e["id"]))
        for entry in entries:
            if entry.get("label") == "short-lived" and entry.get("seen"):
                entry["seen"] = "<what ps showed of a command that may have exited>"
            for key in ("pid", "pgid"):
                if isinstance(entry.get(key), int):
                    self.pid(entry[key])
            for key in ("supervisor", "owner"):
                if isinstance((entry.get(key) or {}).get("pid"), int):
                    self.pid(entry[key]["pid"])
        path = self.case.state / "events.jsonl"
        lines = path.read_text().splitlines() if path.exists() else []
        self.steps.append({"snapshot": name, "entries": entries, "events": [json.loads(line) for line in lines]})

    def masked(self) -> str:
        data = mask(self.steps)
        text = json.dumps(data, indent=1, sort_keys=True)
        text = text.replace(str(self.case.tmp), "<tmp>").replace(str(ROOT), "<root>").replace(events.host(), "<host>")
        text = START.sub("<start>", text)
        text = ISO.sub("<time>", text)
        text = EPOCH.sub("<t>", text)
        ids: Dict[str, str] = {}
        text = ENTRY.sub(lambda m: ids.setdefault(m[0], f"<id{len(ids) + 1}>"), text)
        for index, pid in enumerate([os.getpid(), *self.pids]):
            text = re.sub(rf"(?<![\w.]){pid}(?![\w.])", f"<pid{index}>", text)
        return text + "\n"


def mask(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("<v>" if k in VOLATILE and v is not None else mask(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [mask(v) for v in value]
    if isinstance(value, str) and value.startswith(("{", "[")):
        try:
            return {"json": mask(json.loads(value))}
        except ValueError:
            return value
    return value


class GoldenTests(Case):
    def setUp(self) -> None:
        super().setUp()
        self.env.pop("GODFATHER_ACTOR", None)
        self.record = Recorder(self)
        self.claude = self.fake_claude("s1")
        self.record.pid(self.claude.pid)
        self.as_claude = self.claude_env("s1", self.claude)

    def compare(self, name: str) -> None:
        text = self.record.masked()
        path = GOLDEN / f"{name}.json"
        if os.environ.get("JUNKFATHER_GOLDEN_WRITE"):
            path.parent.mkdir(exist_ok=True)
            path.write_text(text)
        self.assertEqual(path.read_text(), text, f"{name}: entries, events or output changed")

    def child(self) -> subprocess.Popen:
        child = self.spawn("sleep", "600", start_new_session=True)
        self.record.pid(child.pid)
        return child

    def test_own_attached_and_detached_then_reap(self):
        r = self.record
        r.run("own", "--ttl", "1m", "--label", "short-lived", "--", "sh", "-c", "exit 3", env=self.as_claude)
        r.snapshot("attached exited")
        owned = r.run("own", "--detach", "--ttl", "1h", "--", "sleep", "600", env=self.as_claude)
        entry = self.store.load(owned.stdout.split()[1]) or {}
        r.pid(entry["pid"])
        r.pid(entry["supervisor"]["pid"])
        r.snapshot("detached running")
        r.run("reap", "--id", entry["id"], "--grace", "2", env=self.as_claude)
        self.wait_for(lambda: not self.runs(entry["supervisor"]["pid"]), what="the supervisor to end")
        r.snapshot("detached reaped")
        r.run("reap", "--id", entry["id"], "--json", env=self.as_claude)
        r.run("reap", "--id", "jffffff", env=self.as_claude)
        self.compare("own")

    def test_adopt_extend_hand_and_reap(self):
        r = self.record
        child = self.child()
        r.run("adopt", str(child.pid), "--ttl", "1h", "--label", "adopted", env=self.as_claude)
        (entry,) = self.store.entries()
        r.run("adopt", str(child.pid), env=self.as_claude)
        r.run("extend", entry["id"], "--ttl", "2h", env=self.as_claude)
        r.snapshot("adopted and extended")
        other = self.fake_claude("s2")
        r.pid(other.pid)
        r.run("hand", entry["id"], "--to", "s2", env=self.as_claude)
        r.run("hand", entry["id"], "--to", "nobody", env=self.as_claude)
        r.run("hand", entry["id"], "--to", "self", env=self.as_claude)
        r.run("hand", entry["id"], "--to", "self", env=self.env)
        r.run("hand", "jffffff", "--to", "s2", env=self.as_claude)
        r.run("extend", "jffffff", "--ttl", "1h", env=self.as_claude)
        r.snapshot("handed")
        r.run("list", "--json", env=self.as_claude)
        r.run("status", "--json", env=self.as_claude)
        r.run("reap", "--pid", str(child.pid), "--dry-run", "--json", env=self.as_claude)
        r.run("reap", "--lane", "none", env=self.as_claude)
        r.run("reap", "--owner", "s1", env=self.as_claude)
        r.run("reap", "--owner", "s1", "--agent", "a1", "--json", env=self.as_claude)
        r.run("reap", "--id", entry["id"], "--grace", "2", "--reason", "golden", env=self.as_claude)
        r.snapshot("reaped")
        r.run("extend", entry["id"], "--ttl", "1h", env=self.as_claude)
        r.run("hand", entry["id"], "--to", "s2", env=self.as_claude)
        self.compare("adopt")

    def test_refusals(self):
        r = self.record
        child = self.child()
        r.run("adopt", "1", env=self.as_claude)
        r.run("adopt", str(os.getpid()), env=self.as_claude)
        r.run("adopt", "999999", env=self.as_claude)
        r.run("reap", "--pid", str(child.pid), env=self.as_claude)
        r.run("reap", "--pid", str(child.pid), "--force", env=self.as_claude)
        self.state.mkdir(parents=True, exist_ok=True)
        unowned = self.state / "unowned.json"
        listed = (procs.Table.read().get(child.pid) or proc(0)).start
        for item in ({"pid": child.pid, "start": "Mon Jan  1 00:00:00 2024"},
                     {"pid": child.pid, "start": listed, "cmd": "something else"},
                     {"pid": os.getpid(), "start": (procs.Table.read().get(os.getpid()) or proc(0)).start},
                     {"pid": 999999, "start": listed}):
            unowned.write_text(json.dumps({"v": 1, "items": [item]}))
            r.run("reap", "--pid", str(item["pid"]), "--force", env=self.as_claude)
        unowned.write_text(json.dumps({"v": 1, "items": [{"pid": child.pid, "start": listed, "cmd": "sleep 600"}]}))
        r.run("reap", "--pid", str(child.pid), "--force", "--dry-run", "--json", env=self.as_claude)
        r.run("reap", "--pid", str(child.pid), "--force", "--grace", "2", env=self.as_claude)
        r.snapshot("forced")
        r.run("reap", env=self.as_claude)
        r.run("reap", "--agent", "a", "--lane", "x", env=self.as_claude)
        r.run("own", "--", env=self.as_claude)
        r.run("own", "--log", "x", "--", "true", env=self.as_claude)
        r.run("own", "--remove", "--", "true", env=self.as_claude)
        r.run("own", "--kind", "docker", "--detach", "--", "true", env=self.as_claude)
        r.run("own-resource", "--kind", "simulator", "--id", "x", "--remove", env=self.as_claude)
        r.run("own-resource", "--kind", "docker", "--id", "x", "--alive", "true", env=self.as_claude)
        r.run("own-resource", "--kind", "command", "--id", "x", env=self.as_claude)
        r.run("own-resource", "--kind", "safari-tp", "--id", "1", env=self.as_claude)
        r.run("sweep", "--plist", "--interval", "10", env=self.as_claude)
        r.run("changelog", "--json", "--since", "nope", env=self.as_claude)
        self.compare("refusals")

    def test_a_command_resource_owned_and_reaped(self):
        r = self.record
        marker = self.tmp / "marker"
        marker.write_text("up")
        alive, stop = f"test -e {marker}", f"rm {marker}"
        r.run("own-resource", "--kind", "command", "--id", "web", "--alive", alive, "--stop", stop, "--ttl", "1h",
              "--label", "web", env=self.as_claude)
        r.run("own-resource", "--kind", "command", "--id", "web", "--alive", alive, "--stop", stop,
              env=self.as_claude)
        r.snapshot("owned")
        r.run("list", "--json", env=self.as_claude)
        (entry,) = self.store.entries()
        r.run("reap", "--id", entry["id"], "--dry-run", env=self.as_claude)
        r.run("reap", "--id", entry["id"], "--grace", "1", env=self.as_claude)
        r.snapshot("reaped")
        r.run("own-resource", "--kind", "command", "--id", "web", "--alive", alive, "--stop", stop,
              env=self.as_claude)
        self.compare("resource")

    def test_sweep_transitions(self):
        """starting too long -> failed, running without members -> gone, stuck reaping -> released, old -> pruned;
        a resource whose kind says it is gone -> gone."""
        r = self.record
        me = os.getpid()
        now = 1_900_000_000.0
        here = table(proc(me, ppid=1, start="Thu Nov 12 09:00:00 2026", cmd="python3"))
        base = {"v": 1, "argv": ["sleep", "600"], "keep": "owner", "owner": {}, "group": True, "ttl": 3600.0,
                "expires": now + 3600, "known": [], "mode": "detached"}
        entries = [
            {**base, "id": "j000001", "state": "starting", "created": now - 61, "pid": None},
            {**base, "id": "j000002", "state": "starting", "created": now - 30, "pid": None},
            {**base, "id": "j000003", "state": "running", "created": now - 10, "pid": 4243, "pgid": 4243,
             "start": "Thu Nov 12 08:00:00 2026", "known": [[4243, "Thu Nov 12 08:00:00 2026"]]},
            {**base, "id": "j000004", "state": "reaping", "created": now - 5, "pid": 4244, "pgid": 4244,
             "start": "Thu Nov 12 08:00:00 2026",
             "reaping": {"at": now - sweep.REAPING_STUCK - 1, "by": 4242, "by_start": "Thu Nov 12 08:00:00 2026"}},
            {**base, "id": "j000005", "state": "reaped", "created": now - 9e6, "ended": now - 8e6},
            {**base, "id": "j000006", "state": "running", "created": now - 4, "mode": "resource", "group": False,
             "resource": {"kind": "command", "id": "gone", "alive": ["false"], "stop": ["true"]}},
        ]
        for entry in entries:
            self.store.save(entry)
        with mock.patch.object(sweep._resources, "unowned", return_value=([], [])):
            dry = sweep.sweep(self.store, dry_run=True, table=here, cwds={}, sessions=[], now=now)
            self.record.steps.append({"sweep": "dry", "report": dry})
            done = sweep.sweep(self.store, table=here, cwds={}, sessions=[], now=now)
        self.record.steps.append({"sweep": "done", "report": done, "text": sweep.text(done)})
        r.snapshot("swept")
        r.run("status", "--json", env=self.as_claude)
        self.compare("sweep")

    def test_dashboard_contracts(self):
        r = self.record
        r.run("capabilities", "--json", env=self.as_claude)
        r.run("list", "--json", env=self.as_claude)
        r.run("list", env=self.as_claude)
        r.run("list", "--all", env=self.as_claude)
        r.run("status", "--json", env=self.as_claude)
        r.run("version", env=self.as_claude)
        result = r.run("changelog", "--json", env=self.as_claude)
        data = json.loads(result.stdout)
        text = (ROOT / "CHANGELOG.md").read_text()
        self.assertEqual(changelog.entries(text, None, None), data["entries"])
        self.record.steps[-1]["out"] = json.dumps({k: v for k, v in data.items() if k != "entries"}, sort_keys=True)
        self.compare("dashboard")


if __name__ == "__main__":
    unittest.main()
