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
from typing import Any, Dict, List, Optional
import unittest
from unittest import mock

from support import ROOT, Case, proc, table

from junkfather import changelog, events, launch, procs, reap, resources, sweep

GOLDEN = Path(__file__).resolve().parent / "golden"
VOLATILE = {"age", "ttl_left", "at", "time", "created", "expires", "ended"}
START = re.compile(r"\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) +\d+ "
                   r"\d\d:\d\d:\d\d \d{4}\b")
ISO = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z")
EPOCH = re.compile(r"\b1\d{9}(\.\d+)?\b")
ENTRY = re.compile(r"\bj[0-9a-f]{6}\b")
TTY = re.compile(r"\bttys?\d+\b")


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

    def run(self, *args: str, env: Dict[str, str], input: Optional[str] = None) -> subprocess.CompletedProcess:
        result = self.case.cli(*args, env=env, input=input)
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
        text = TTY.sub("<tty>", text)
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


    # Added in the review round (fathers#77 to #80): the safety paths, a supervisor revive and a claim-named
    # subagent. Generated on 0d14d9e's code like the rest.

    def test_the_sweep_report_skips_what_the_policy_spares(self):
        """reportable, below_session and untouchable_tree inside a real sweep, and the unowned resources report."""
        me = os.getpid()
        root = "/golden/scratch"
        A, B = "Thu Nov 12 09:00:00 2026", "Thu Nov 12 08:00:00 2026"
        here = table(
            proc(me, ppid=1, start=A, cmd="python3"),
            proc(500, start=B, cmd="/x/.local/share/claude/versions/2.1.0"),
            proc(510, ppid=500, start=B, cmd="node mcp-server"),  # below a live session
            proc(600, start=B, cmd="node server.js"), proc(601, ppid=600, start=B, cmd="node worker"),
            proc(700, start=B, cmd="sh -c tmux"), proc(701, ppid=700, tty="ttys009", start=B, cmd="zsh"),
            proc(710, start=B, cmd="sh -c agent"), proc(711, ppid=710, start=B, cmd="/usr/local/bin/codex"),
            proc(800, start=B, cmd=f"/Applications/Browser --profile={root}/s1/profile"),
            proc(900, uid=os.getuid() + 1, start=B, cmd="node other"),
            proc(910, tty="ttys010", start=B, cmd="vim"),
            proc(920, start=B, cmd="sleep 600"),
            proc(930, start=B, cmd="python3 -m http.server"),
            moment=1_900_000_000.0)
        cwds = {510: f"{root}/s1", 600: f"{root}/s1", 601: f"{root}/s1", 700: f"{root}/s1", 701: f"{root}/s1",
                710: f"{root}/s1", 711: f"{root}/s1", 900: f"{root}/s1", 910: f"{root}/s1", 920: f"{root}/s1",
                930: "/repo/.claude/worktrees/w1"}
        owned = {"v": 1, "id": "j0000a1", "state": "running", "pid": 920, "pgid": 920, "start": B, "seen": "sleep 600",
                 "group": True, "known": [[920, B]], "argv": ["sleep", "600"], "expires": 1_900_003_600.0,
                 "keep": "owner", "owner": {}, "created": 1_899_999_000.0, "mode": "detached", "ttl": 3600.0}
        held = {**owned, "id": "j0000a2", "pid": None, "pgid": None, "known": [], "mode": "resource", "group": False,
                "resource": {"kind": "simulator", "id": "UDID-HELD"}}
        self.store.save(owned)
        self.store.save(held)
        listings = {"simulator": {"UDID-HELD": {"name": "iPhone"}, "UDID-FREE": {"name": "iPad"}},
                    "docker": None, "tailscale-serve": {"https:443": {"target": "http://127.0.0.1:6006"}},
                    "safari-tp": {"4242": {"pid": 4242}}}
        with mock.patch.dict(os.environ, {"JUNKFATHER_SCRATCH_ROOTS": root}), \
                mock.patch.object(resources, "tool", lambda name: "/usr/bin/true"), \
                mock.patch.object(resources.Snapshot, "listing", lambda snap, kind: listings.get(kind)):
            report = sweep.sweep(self.store, table=here, cwds=cwds, sessions=[], now=1_900_000_000.0)
        self.record.steps.append({"sweep": report, "text": sweep.text(report),
                                  "unowned.json": json.loads((self.state / "unowned.json").read_text())})
        self.record.snapshot("swept")
        self.compare("sweep_policy")

    def test_members_leave_out_what_no_reap_may_touch(self):
        me = os.getpid()
        A = "Thu Nov 12 09:00:00 2026"
        here = table(
            proc(1, ppid=0, start=A, cmd="launchd"), proc(me, ppid=1, start=A, cmd="python3"),
            proc(300, start=A, cmd="node server.js"),
            proc(301, ppid=300, pgid=300, start=A, cmd="node worker"),
            proc(302, ppid=300, pgid=300, tty="ttys003", start=A, cmd="zsh"),
            proc(303, ppid=302, pgid=300, start=A, cmd="vim"),
            proc(304, ppid=300, start=A, cmd="/usr/local/bin/codex exec"),
            proc(305, ppid=304, start=A, cmd="node codex-helper"),
            proc(306, ppid=300, pgid=300, uid=os.getuid() + 1, start=A, cmd="node other-user"),
            proc(307, ppid=301, start=A, cmd="chrome --headless"),
            proc(400, start=A, cmd="node gone-leader"))
        cases = {
            "group": {"pid": 300, "pgid": 300, "start": A, "seen": "node server.js", "group": True,
                      "known": [[300, A]]},
            "known only": {"pid": 399, "pgid": 399, "start": A, "seen": "x", "group": True,
                           "known": [[301, A], [302, A], [400, A]],
                           "known_seen": [[301, A, "node worker"], [302, A, "zsh"], [400, A, "node renamed"]]},
            "reused": {"pid": 300, "pgid": 300, "start": "Mon Jan  1 00:00:00 2024", "seen": "node server.js",
                       "group": True, "known": [[300, "Mon Jan  1 00:00:00 2024"], [301, A]]},
            "changed": {"pid": 300, "pgid": 300, "start": A, "seen": "python3 other.py", "group": True,
                        "known": [[300, A], [307, A]], "known_seen": [[300, A, "python3 other.py"],
                                                                      [307, A, "chrome --headless"]]},
        }
        for name, entry in cases.items():
            for force in (False, True):
                found, how = reap.members(entry, here, force=force)
                self.record.steps.append({"members": name, "force": force, "how": how,
                                          "pids": [p.pid for p in found]})
        self.compare("members")

    def test_refusals_of_agents_terminals_and_held_pids(self):
        r = self.record
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "claude"
        fake.symlink_to("/bin/sleep")
        agent = self.spawn(str(fake), "600", start_new_session=True)
        r.pid(agent.pid)
        shell = self.spawn("sh", "-c", '"${0}ude" 600; true', str(fake)[:-3], start_new_session=True)
        r.pid(shell.pid)
        script = self.spawn("script", "-q", "/dev/null", "sleep", "600", stdin=subprocess.PIPE,
                            start_new_session=True)
        r.pid(script.pid)
        below = self.below(shell.pid)
        sleeper = self.below(script.pid)
        held = self.child()
        self.wait_for(lambda: (procs.Table.read().get(agent.pid) or proc(0, cmd="")).cmd.endswith(" 600"),
                      what="the agent stand-in")
        r.run("adopt", str(agent.pid), env=self.as_claude)
        r.run("adopt", str(sleeper), env=self.as_claude)
        r.run("adopt", str(shell.pid), env=self.as_claude)
        r.run("adopt", str(below), env=self.as_claude)
        r.run("adopt", str(held.pid), "--ttl", "1h", env=self.as_claude)
        (entry,) = self.store.entries()
        self.store.update(entry["id"], lambda e: e.update(state="reaping"))
        here = procs.Table.read()
        self.state.mkdir(parents=True, exist_ok=True)
        for pid in (agent.pid, shell.pid, sleeper, held.pid):
            current = here.get(pid) or proc(0)
            (self.state / "unowned.json").write_text(json.dumps(
                {"v": 1, "items": [{"pid": pid, "start": current.start, "cmd": current.cmd}]}))
            r.run("reap", "--pid", str(pid), "--force", env=self.as_claude)
        r.snapshot("refused")
        self.store.update(entry["id"], lambda e: e.update(state="running"))
        r.run("reap", "--id", entry["id"], "--grace", "2", env=self.as_claude)
        r.snapshot("reaped")
        self.compare("refusals_live")

    def below(self, parent: int) -> int:
        """The child of `parent` once it runs its `600` command, registered with the recorder and killed at the end
        if it is still that process."""
        found: List[int] = []

        def ready() -> bool:
            found[:] = [p.pid for p in procs.Table.read().descendants(parent) if p.cmd.endswith(" 600")]
            return bool(found)
        self.wait_for(ready, what="the child")
        pid = self.record.pid(found[0])
        start = procs.identities([pid]).get(pid)

        def kill() -> None:
            if start is not None and procs.identities([pid]).get(pid) == start:
                try:
                    os.kill(pid, 9)
                except OSError:
                    pass
        self.addCleanup(kill)
        return pid

    def test_a_supervisor_revives_a_gone_entry(self):
        class Quiet(launch.Catcher):
            def __init__(self) -> None:
                self.signum = None

        child = self.child()
        record = launch.new_entry(self.store, ["sleep", "600"], 3600.0, "owner", "revived", {"kind": "process"},
                                  "attached", str(self.tmp))
        self.store.save(record)
        launch.record_child(self.store, record["id"], child, os.getpid())
        self.store.update(record["id"], lambda e: e.update(state="gone", ended=1.0, known=[]))
        self.record.snapshot("gone")
        seen: List[Any] = []

        def pause(_seconds: float) -> None:
            seen.append(self.store.load(record["id"]))
            child.kill()
            child.wait()

        launch.Supervisor(self.store, record["id"], child, quiet=True, catcher=Quiet(), clock=lambda: 0.0,
                          pause=pause).run()
        self.record.steps.append({"while running": seen[0]})
        self.record.snapshot("exited")
        self.compare("revive")

    def test_a_claim_names_the_subagent_that_owns_and_reaps(self):
        r = self.record
        command = "junkfather own --detach --ttl 1h -- sleep 600"
        r.run("hook", input=json.dumps({"hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": "Bash",
                                        "tool_use_id": "toolu_golden", "agent_id": "agent-a",
                                        "agent_type": "Explore", "tool_input": {"command": command}}),
              env=self.as_claude)
        owned = r.run("own", "--detach", "--ttl", "1h", "--", "sleep", "600", env=self.as_claude)
        entry = self.store.load(owned.stdout.split()[1]) or {}
        r.pid(entry["pid"])
        r.pid(entry["supervisor"]["pid"])
        r.run("own", "--detach", "--ttl", "1h", "--", "sleep", "600", env=self.as_claude)
        second = [e for e in self.store.entries() if e["id"] != entry["id"]][0]
        r.pid(second["pid"])
        r.pid(second["supervisor"]["pid"])
        r.snapshot("owned")
        r.run("reap", "--owner", "s1", "--agent", "agent-a", "--grace", "2", env=self.as_claude)
        r.run("reap", "--owner", "s1", "--grace", "2", env=self.as_claude)
        supervisors = [entry["supervisor"]["pid"], second["supervisor"]["pid"]]
        self.wait_for(lambda: not any(self.runs(pid) for pid in supervisors), what="the supervisors to end")
        r.snapshot("reaped")
        self.compare("claim")


if __name__ == "__main__":
    unittest.main()
