"""Resources that outlive their process, against fake xcrun, docker, tailscale, open and osascript on PATH
(tests/fakes): own and own-resource, the stop through each kind's command, the hooks, the sweep's gone, TTL and
orphan handling, and the report-only path for unowned resources."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sys
import time
from typing import Any, Dict, List

from support import ROOT, Case

import eventcheck
from junkfather import procs, resources

FAKES = ROOT / "tests" / "fakes"
SIM = "F29DD884-3B1F-4EC8-9750-546BA1CB647D"
OTHER_SIM = "4F716ED7-1987-4A6D-AEC8-84D91B680928"


class ResourceCase(Case):
    def setUp(self) -> None:
        super().setUp()
        self.fake = self.tmp / "fake.json"
        app = self.tmp / "Safari Technology Preview.app" / "Contents" / "MacOS" / "Safari Technology Preview"
        app.parent.mkdir(parents=True)
        app.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(600)\n")
        app.chmod(0o755)
        self.app = app
        self.write_fake(sims={SIM: {"name": "iPhone 18 Pro", "state": "Shutdown"},
                              OTHER_SIM: {"name": "iPhone 18 Pro Max", "state": "Shutdown"}},
                        containers={}, routes={"https:443": "http://127.0.0.1:5186"}, stp=[], fail=[],
                        stp_app=str(app))
        self.env.update(PATH=f"{FAKES}{os.pathsep}{self.env.get('PATH', '')}", JUNKFATHER_FAKE_STATE=str(self.fake))
        self.addCleanup(self.end_fake_apps)

    def write_fake(self, **state: Any) -> None:
        self.fake.write_text(json.dumps(state))

    def fake_state(self) -> Dict[str, Any]:
        return json.loads(self.fake.read_text())

    def change_fake(self, **changes: Any) -> None:
        state = self.fake_state()
        state.update(changes)
        self.fake.write_text(json.dumps(state))

    def end_fake_apps(self) -> None:
        for proc in procs.Table.read().procs.values():
            if resources.STP_MAIN.search(proc.cmd) and str(self.tmp) in proc.cmd:
                try:
                    os.kill(proc.pid, signal.SIGKILL)
                except OSError:
                    pass

    def events(self) -> List[Dict[str, Any]]:
        path = self.state / "events.jsonl"
        lines = path.read_text().splitlines() if path.exists() else []
        for line in lines:
            self.assertEqual([], eventcheck.validate_line(line, strict=True), line)
        return [json.loads(line) for line in lines]

    def own(self, *args: str, env: "Dict[str, str] | None" = None) -> Dict[str, Any]:
        result = self.cli("own", *args, env=env or self.env)
        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        entry = self.store.entries()[-1]
        self.assertEqual(("resource", "running"), (entry["mode"], entry["state"]))
        return entry


class SimulatorTests(ResourceCase):
    def test_own_kind_simulator_boots_owns_and_reap_shuts_it_down(self):
        entry = self.own("--kind", "simulator", "--ttl", "1h", "--label", "ios", "--", "xcrun", "simctl", "boot", SIM)
        self.assertEqual({"kind": "simulator", "id": SIM}, {k: entry["resource"][k] for k in ("kind", "id")})
        self.assertEqual(("iPhone 18 Pro", ["xcrun", "simctl", "boot", SIM]), (entry["resource"]["name"],
                                                                               entry["argv"]))
        listed = json.loads(self.cli("list", "--json", env=self.env).stdout)["entries"]
        self.assertEqual(("simulator", SIM, None), (listed[0]["resource"]["kind"], listed[0]["resource"]["id"],
                                                    listed[0]["processes"]))
        self.assertIn(f"[ios] simulator {SIM}", self.cli("list", env=self.env).stdout)
        result = self.cli("reap", "--id", entry["id"], "--json", env=self.env)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("stopped", json.loads(result.stdout)["results"][0]["result"])
        self.assertEqual("Shutdown", self.fake_state()["sims"][SIM]["state"])
        self.assertIn(["xcrun", "simctl", "shutdown", SIM], self.fake_state()["calls"])
        self.assertEqual("reaped", self.state_of(entry["id"]))
        kinds = [(e["kind"], e["subject"]["type"], e["subject"]["id"]) for e in self.events()]
        self.assertIn(("resource.owned", "resource", f"simulator:{SIM}"), kinds)
        self.assertIn(("resource.stopped", "resource", f"simulator:{SIM}"), kinds)

    def test_subagent_stop_shuts_down_that_subagents_simulator(self):
        claude = self.fake_claude("s1")
        env = {**self.claude_env("s1", claude), "PATH": self.env["PATH"], "JUNKFATHER_FAKE_STATE": str(self.fake)}
        command = f"junkfather own --kind simulator -- xcrun simctl boot {SIM}"
        self.hook("PreToolUse", "s1", env=env, tool_name="Bash", tool_input={"command": command},
                  tool_use_id="toolu_1", agent_id="agent-a")
        entry = self.own("--kind", "simulator", "--", "xcrun", "simctl", "boot", SIM, env=env)
        self.assertEqual(("s1", "agent-a"), (entry["owner"]["session"], entry["owner"]["agent"]))
        self.hook("SubagentStop", "s1", env=env, agent_id="agent-a")
        self.wait_for(lambda: self.state_of(entry["id"]) == "reaped", what="the subagent's simulator reaped")
        self.assertEqual("Shutdown", self.fake_state()["sims"][SIM]["state"])

    def test_a_failed_stop_keeps_the_entry_and_is_reported(self):
        entry = self.own("--kind", "simulator", "--", "xcrun", "simctl", "boot", SIM)
        self.change_fake(fail=[["xcrun", "simctl", "shutdown"]])
        result = self.cli("reap", "--id", entry["id"], "--json", "--grace", "1", env=self.env)
        self.assertEqual(1, result.returncode)
        self.assertEqual("failed", json.loads(result.stdout)["results"][0]["result"])
        kept = self.store.load(entry["id"]) or {}
        self.assertEqual("running", kept["state"])
        self.assertIn("failing on purpose", kept["last_error"]["error"])
        failed = [e for e in self.events() if e["kind"] == "resource.stop_failed"]
        self.assertEqual(["error"], [e["level"] for e in failed])

    def test_a_failed_boot_or_nothing_new_owns_nothing(self):
        self.change_fake(fail=[["xcrun", "simctl", "boot"]])
        self.assertEqual(3, self.cli("own", "--kind", "simulator", "--", "xcrun", "simctl", "boot", SIM,
                                     env=self.env).returncode)
        self.change_fake(fail=[])
        resources_before = self.fake_state()
        resources_before["sims"][SIM]["state"] = "Booted"
        self.fake.write_text(json.dumps(resources_before))
        result = self.cli("own", "--kind", "simulator", "--", "true", env=self.env)
        self.assertEqual(1, result.returncode)
        self.assertIn("no new simulator", result.stderr)
        self.assertEqual([], self.store.entries())

    def test_own_resource_refuses_what_does_not_run_or_is_owned(self):
        refused = self.cli("own-resource", "--kind", "simulator", "--id", SIM, env=self.env)
        self.assertEqual(1, refused.returncode)
        self.assertIn("no running simulator", refused.stderr)
        self.change_fake(sims={SIM: {"name": "iPhone 18 Pro", "state": "Booted"}})
        self.assertEqual(0, self.cli("own-resource", "--kind", "simulator", "--id", SIM.lower(), "--ttl", "1h",
                                     env=self.env).returncode)
        again = self.cli("own-resource", "--kind", "simulator", "--id", SIM, env=self.env)
        self.assertEqual(1, again.returncode)
        self.assertIn("already owned", again.stderr)
        self.assertEqual(1, len(self.store.entries()))

    def test_sweep_marks_gone_reaps_expired_and_leaves_unknown_alone(self):
        gone = self.own("--kind", "simulator", "--", "xcrun", "simctl", "boot", SIM)
        expired = self.own("--kind", "simulator", "--ttl", "1s", "--", "xcrun", "simctl", "boot", OTHER_SIM)
        state = self.fake_state()
        state["sims"][SIM]["state"] = "Shutdown"  # shut down by hand
        state["fail"] = [["xcrun", "simctl", "list"]]
        self.fake.write_text(json.dumps(state))
        time.sleep(1.2)
        unknown = json.loads(self.cli("sweep", "--json", env=self.env).stdout)
        self.assertEqual(([], []), (unknown["reaped"], unknown["gone"]), "unknown liveness changes nothing")
        self.assertTrue(any("cannot be told" in note for note in unknown["notes"]))
        self.change_fake(fail=[])
        swept = json.loads(self.cli("sweep", "--json", env=self.env).stdout)
        self.assertEqual([gone["id"]], [g["id"] for g in swept["gone"]])
        self.assertEqual([(expired["id"], "ttl expired", "stopped")],
                         [(r["id"], r["reason"], r["result"]) for r in swept["reaped"]])
        self.assertEqual("Shutdown", self.fake_state()["sims"][OTHER_SIM]["state"])

    def test_sweep_reaps_the_simulator_of_a_session_that_ended(self):
        claude = self.fake_claude("s1")
        env = {**self.claude_env("s1", claude), "PATH": self.env["PATH"], "JUNKFATHER_FAKE_STATE": str(self.fake)}
        entry = self.own("--kind", "simulator", "--ttl", "1h", "--", "xcrun", "simctl", "boot", SIM, env=env)
        claude.kill()
        claude.wait()
        swept = json.loads(self.cli("sweep", "--json", env=self.env).stdout)
        self.assertEqual([entry["id"]], [r["id"] for r in swept["reaped"]])
        self.assertIn("owner gone", swept["reaped"][0]["reason"])


class DockerTests(ResourceCase):
    def test_docker_run_d_is_owned_by_the_id_it_prints_and_removed_when_asked(self):
        entry = self.own("--kind", "docker", "--remove", "--", "docker", "run", "-d", "nginx")
        (ident,) = self.fake_state()["containers"]
        self.assertEqual((ident, "box0", True), (entry["resource"]["id"], entry["resource"]["name"],
                                                 entry["resource"]["remove"]))
        self.assertEqual(0, self.cli("reap", "--id", entry["id"], env=self.env).returncode)
        self.assertEqual({}, self.fake_state()["containers"])
        calls = [c for c in self.fake_state()["calls"] if c[:2] in (["docker", "stop"], ["docker", "rm"])]
        self.assertEqual([["docker", "stop", "-t", "10", ident], ["docker", "rm", ident]], calls)

    def test_own_resource_takes_a_name_and_records_the_full_id(self):
        self.cli("own", "--kind", "docker", "--", "docker", "run", "-d", "redis", env=self.env)
        (ident,) = self.fake_state()["containers"]
        self.store.update(self.store.entries()[0]["id"], lambda e: e.update(state="reaped"))
        result = self.cli("own-resource", "--kind", "docker", "--id", "box0", env=self.env)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(ident, self.store.entries()[-1]["resource"]["id"])
        self.assertEqual(1, self.cli("own-resource", "--kind", "docker", "--id", "nope", env=self.env).returncode)

    def test_unowned_running_containers_are_reported_never_stopped(self):
        self.change_fake(containers={"a" * 64: {"name": "theirs", "image": "postgres", "running": True},
                                     "b" * 64: {"name": "exited", "image": "x", "running": False}})
        swept = json.loads(self.cli("sweep", "--json", env=self.env).stdout)
        self.assertIn(f"docker:{'a' * 64}", [i["key"] for i in swept["unowned_resources"]])
        self.assertNotIn(f"docker:{'b' * 64}", [i["key"] for i in swept["unowned_resources"]])
        self.assertTrue(self.fake_state()["containers"]["a" * 64]["running"])
        self.assertFalse([c for c in self.fake_state()["calls"] if c[:2] == ["docker", "stop"]])


class TailscaleTests(ResourceCase):
    def test_serve_bg_owns_the_new_route_and_reap_removes_only_that_route(self):
        entry = self.own("--kind", "tailscale-serve", "--", "tailscale", "serve", "--bg", "--https=6475", "6475")
        self.assertEqual("https:6475", entry["resource"]["id"])
        swept = json.loads(self.cli("sweep", "--json", env=self.env).stdout)
        self.assertEqual(["tailscale-serve:https:443"], [i["key"] for i in swept["unowned_resources"]])
        reported = [e for e in self.events() if e["kind"] == "unowned.reported" and e["subject"]["type"] == "resource"]
        self.assertEqual([("resource", "tailscale-serve:https:443", "warn")],
                         [(e["subject"]["type"], e["subject"]["id"], e["level"]) for e in reported])
        self.assertEqual(0, self.cli("sweep", "--quiet", env=self.env).returncode)
        self.assertEqual(1, len([e for e in self.events() if e["kind"] == "unowned.reported"
                                 and e["subject"]["type"] == "resource"]), "reported once")
        self.assertEqual(0, self.cli("reap", "--id", entry["id"], env=self.env).returncode)
        self.assertEqual({"https:443": "http://127.0.0.1:5186"}, self.fake_state()["routes"])
        self.assertIn(["tailscale", "serve", "--https=6475", "off", "--yes"], self.fake_state()["calls"])
        status = json.loads(self.cli("status", "--json", env=self.env).stdout)
        self.assertEqual(["https:443"], [i["id"] for i in status["unowned_resources"]])

    def test_serve_bg_that_repoints_a_served_port_owns_that_route(self):
        entry = self.own("--kind", "tailscale-serve", "--", "tailscale", "serve", "--bg", "6475")
        self.assertEqual(("https:443", "http://127.0.0.1:6475"), (entry["resource"]["id"],
                                                                  entry["resource"]["target"]))

    def test_routes_parse_paths_and_tcp_forwards(self):
        found = resources.routes({"TCP": {"443": {"HTTPS": True}, "5432": {"TCPForward": "127.0.0.1:5432"}},
                                  "Web": {"h.ts.net:443": {"Handlers": {"/": {"Proxy": "x"},
                                                                        "/docs": {"Path": "/srv"}}}}})
        self.assertEqual({"https:443", "https:443/docs", "tcp:5432"}, set(found))
        self.assertEqual([["tailscale", "serve", "--https=443", "--set-path=/docs", "off", "--yes"]],
                         resources.KINDS["tailscale-serve"].stop({"id": "https:443/docs"}, 10))


class SafariTests(ResourceCase):
    def test_the_instance_an_open_starts_is_owned_and_quit(self):
        entry = self.own("--kind", "safari-tp", "--", "open", "-g", "-a", "Safari Technology Preview")
        pid = entry["resource"]["pid"]
        self.assertEqual(str(pid), entry["resource"]["id"])
        self.assertTrue(self.runs(pid))
        self.assertEqual(0, self.cli("reap", "--id", entry["id"], env=self.env).returncode)
        self.wait_for(lambda: not self.runs(pid), what="the quit")
        self.assertEqual("reaped", self.state_of(entry["id"]))

    def test_an_instance_that_already_ran_is_not_taken_and_a_second_one_is_never_quit(self):
        theirs = self.spawn(str(self.app), start_new_session=True)
        self.wait_for(lambda: self.runs(theirs.pid), what="the person's instance")
        state = self.fake_state()
        state["stp_app"] = "/nonexistent"  # `open` reaches the running instance and starts nothing
        self.fake.write_text(json.dumps(state))
        self.assertNotEqual(0, self.cli("own", "--kind", "safari-tp", "--", "open", "-g", "-a", "x",
                                        env=self.env).returncode)
        self.change_fake(stp_app=str(self.app))
        entry = self.own("--kind", "safari-tp", "--", "open", "-n", "-g", "-a", "Safari Technology Preview")
        result = self.cli("reap", "--id", entry["id"], "--json", "--grace", "1", env=self.env)
        self.assertEqual("failed", json.loads(result.stdout)["results"][0]["result"])
        self.assertIn("instances run", (self.store.load(entry["id"]) or {})["last_error"]["error"])
        self.assertTrue(self.runs(theirs.pid) and self.runs(entry["resource"]["pid"]), "both keep running")
        self.assertFalse([c for c in self.fake_state()["calls"] if c[0] == "osascript"])


class CommandKindTests(ResourceCase):
    def test_a_command_pair_checks_and_stops_it_without_a_shell(self):
        marker = self.tmp / "it runs; touch pwned"
        marker.write_text("")
        result = self.cli("own-resource", "--kind", "command", "--id", "marker", "--alive",
                          f"test -e '{marker}'", "--stop", f"rm '{marker}'", env=self.env)
        self.assertEqual(0, result.returncode, result.stderr)
        (entry,) = self.store.entries()
        self.assertEqual(["rm", str(marker)], entry["resource"]["stop"])
        self.assertEqual(0, self.cli("reap", "--id", entry["id"], env=self.env).returncode)
        self.assertFalse(marker.exists())
        self.assertFalse(Path(ROOT / "pwned").exists() or (self.tmp / "pwned").exists())
        gone = self.cli("own-resource", "--kind", "command", "--id", "m", "--alive", f"test -e '{marker}'",
                        "--stop", "true", env=self.env)
        self.assertEqual(1, gone.returncode)


class EventKindsTests(ResourceCase):
    def test_capabilities_list_the_resource_kinds(self):
        kinds = json.loads(self.cli("capabilities", "--json", env=self.env).stdout)["dashboard"]["events"]["kinds"]
        self.assertTrue({"resource.owned", "resource.stopped", "resource.stop_failed"} <= set(kinds))

    def test_a_stop_command_that_hangs_times_out(self):
        original = resources.STOP_TIMEOUT
        resources.STOP_TIMEOUT = 0.5
        try:
            started = time.monotonic()
            outcome = resources.stop({"kind": "command", "id": "x", "stop": ["sleep", "30"]}, 0.5, lambda: True,
                                     pause=lambda _s: None)
        finally:
            resources.STOP_TIMEOUT = original
        self.assertEqual("failed", outcome["result"])
        self.assertIn("did not answer", outcome["error"])
        self.assertLess(time.monotonic() - started, 10)

