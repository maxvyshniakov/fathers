"""A background Bash command owned on its own (fathers#166): the PreToolUse hook rewrites a `run_in_background` call
so that its shell adopts itself before the agent's command runs, a subagent's stop or the TTL reaps it, and a
foreground call is left alone. End to end through the hook fixture with real processes, the shell started the way
Claude Code starts a Bash call: a group of its own running `eval '<command>' && pwd -P`."""
from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

from support import ROOT, Case, plain

from junkfather import background, defaults, hook, procs


def call(command: str, background_: Optional[bool] = True, agent: Optional[str] = "agent-a",
         tool_use: str = "toolu_01", **extra: Any) -> Dict[str, Any]:
    tool_input: Dict[str, Any] = {"command": command, "description": "wait for it", "timeout": 600000, **extra}
    if background_ is not None:
        tool_input["run_in_background"] = background_
    data: Dict[str, Any] = {"hook_event_name": "PreToolUse", "session_id": "s1", "tool_use_id": tool_use,
                            "tool_name": "Bash", "tool_input": tool_input}
    if agent:
        data.update(agent_id=agent, agent_type="general-purpose")
    return data


class RewriteTests(Case):
    """What the PreToolUse hook answers, decided without running the command."""

    def answer(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        said: List[Dict[str, Any]] = []
        hook.handle(data, self.store, spawn=lambda argv: self.fail(f"spawned {argv}"), emit=said.append)
        return said

    def test_a_background_call_runs_the_adopting_line_first_and_keeps_the_rest_of_its_input(self):
        (said,) = self.answer(call("until grep -q done out; do sleep 15; done", dangerouslyDisableSandbox=True))
        output = said["hookSpecificOutput"]
        self.assertEqual("PreToolUse", output["hookEventName"])
        self.assertNotIn("permissionDecision", output, "the permission flow stays Claude Code's")
        updated = output["updatedInput"]
        first, rest = updated["command"].split("\n", 1)
        self.assertEqual("until grep -q done out; do sleep 15; done", rest, "the agent's command, unchanged")
        self.assertIn(" background toolu_01 ", first)
        self.assertTrue(first.endswith(" >/dev/null 2>&1 </dev/null"), "the adopting line prints nothing")
        self.assertEqual({"description": "wait for it", "timeout": 600000, "run_in_background": True,
                          "dangerouslyDisableSandbox": True},
                         {k: v for k, v in updated.items() if k != "command"})
        claim = json.loads((self.state / "claims" / "s1" / "toolu_01.json").read_text())
        self.assertEqual(("agent-a", "general-purpose", True, "wait for it"),
                         (claim["agent"], claim["agent_type"], claim["background"], claim["description"]))

    def test_a_foreground_call_is_left_alone(self):
        for flag in (False, None):
            self.assertEqual([], self.answer(call("sleep 600", background_=flag)))
        self.assertFalse((self.state / "claims").exists())

    def test_a_call_that_runs_junkfather_is_left_to_junkfather(self):
        self.assertEqual([], self.answer(call("junkfather own --ttl 1h -- vite")))
        self.assertTrue((self.state / "claims" / "s1" / "toolu_01.json").exists(), "the claim for own, as before")

    def test_only_one_explicit_ownership_command_is_left_to_junkfather(self):
        alone = ("junkfather own -- vite", "~/.local/bin/junkfather own --detach -- npm start",
                 "PORT=1 junkfather own -- vite", "nohup junkfather own -- x", "env -i A=1 junkfather adopt 42",
                 "python3 -m junkfather own -- x", "junkfather own-resource --kind docker --id db",
                 "junkfather own --ttl 1h -- sh -c 'a; b'", "junkfather own -- x\n")
        for command in alone:
            self.assertTrue(background.owns_itself(command), command)
        owned = ("junkfather list; until grep -q exit= out; do sleep 15; done", "cd app && junkfather own -- x",
                 "junkfather list", "junkfather own -- x | tee log", "(junkfather own -- x)", "x=$(junkfather list)",
                 "cd ~/fathers/junkfather && make", "timeout 600 junkfather own -- x", "bash -c 'junkfather own -- x'",
                 "junkfather own -- x > log", 'echo "unterminated junkfather own', "grep junkfather docs", "sleep 600")
        for command in owned:
            self.assertFalse(background.owns_itself(command), command)

    def test_a_command_that_only_names_junkfather_is_taken_over(self):
        (said,) = self.answer(call("cd ~/fathers/junkfather && until test -f done; do sleep 15; done"))
        rest = said["hookSpecificOutput"]["updatedInput"]["command"].split("\n", 1)[1]
        self.assertEqual("cd ~/fathers/junkfather && until test -f done; do sleep 15; done", rest)

    def test_nothing_without_what_identifies_the_call(self):
        for data in (call("sleep 600", tool_use="../x"), call("sleep 600", tool_use=""), call("   "),
                     {**call("sleep 600"), "session_id": None}, {**call("sleep 600"), "tool_name": "exec_command"}):
            self.assertEqual([], self.answer(data), data)


class BackgroundTests(Case):
    """The hook's answer run as Claude Code runs it, then the subagent's stop, the TTL and other entries."""

    def setUp(self) -> None:
        super().setUp()
        self.claude = self.fake_claude("s1")
        self.session_env = self.claude_env("s1", self.claude)
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.addCleanup(self.end_watchers)

    def end_watchers(self) -> None:
        """A watcher ends with its shell; one still there (a test that failed midway) is stopped by its identity."""
        for entry in self.store.entries():
            supervisor = entry.get("supervisor") or {}
            if supervisor.get("pid") and procs.identities([supervisor["pid"]]).get(supervisor["pid"]) == \
                    procs.norm(supervisor.get("start") or ""):
                os.kill(supervisor["pid"], signal.SIGKILL)

    def rewritten(self, data: Dict[str, Any]) -> str:
        result = self.hook("PreToolUse", data.pop("session_id"), env=self.session_env, **data)
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads(result.stdout)["hookSpecificOutput"]["updatedInput"]["command"]

    def run_as_claude(self, command: str, env: Optional[Dict[str, str]] = None) -> subprocess.Popen[Any]:
        """The Bash tool's shell: a process group of its own, the command evaluated, then the working directory
        recorded, so the shell outlives the command's last step as Claude Code's does."""
        shell = self.spawn("/bin/sh", "-c", 'eval "$1" < /dev/null && pwd -P > /dev/null', "sh", command,
                           start_new_session=True, cwd=self.work, env=env or self.session_env,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for pipe in (shell.stdout, shell.stderr):
            if pipe is not None:
                self.addCleanup(pipe.close)
        self.addCleanup(self.end_group, shell)
        return shell

    def end_group(self, shell: subprocess.Popen[Any]) -> None:
        """The shell's whole group, what the agent's command left in it too; only while the shell is not waited for,
        so its pid, the group's id, cannot have been taken again."""
        if shell.returncode is None:
            try:
                os.killpg(shell.pid, signal.SIGKILL)
            except OSError:
                pass

    def adopted(self) -> Dict[str, Any]:
        self.wait_for(lambda: any(e.get("state") == "running" for e in self.store.entries()), what="the entry",
                      seen=lambda: (self.state / "hook.out").read_text() if (self.state / "hook.out").exists()
                      else "no entry")
        (entry,) = map(plain, self.store.entries())
        return entry

    def test_a_subagents_background_command_is_listed_as_its_own_and_ends_with_its_stop(self):
        shell = self.run_as_claude(self.rewritten(call("echo started; sleep 600")))
        entry = self.adopted()
        sleeper = self.runs_600(shell, below=True)
        self.assertEqual((shell.pid, shell.pid, True, "adopted", "background", "owner"),
                         (entry["pid"], entry["pgid"], entry["group"], entry["mode"], entry["auto"], entry["keep"]))
        self.assertEqual(("claude", "s1", "agent-a", "general-purpose", self.claude.pid),
                         tuple(entry["owner"][k] for k in ("kind", "session", "agent", "agent_type", "pid")))
        self.assertEqual((7200, ["echo started; sleep 600"], "wait for it"),
                         (entry["ttl"], entry["argv"], entry["label"]))
        listed = json.loads(self.cli("list", "--json").stdout)["entries"]
        self.assertEqual([("agent-a", "general-purpose", "background", 2)],
                         [(r["agent"], r["agent_type"], r["auto"], r["processes"]) for r in listed])
        self.assertIn("/agent-a", self.cli("status").stdout)

        self.hook("SubagentStop", "s1", env=self.session_env, agent_id="agent-b")
        self.assertEqual("running", self.state_of(entry["id"]), "another subagent's stop")
        self.hook("SubagentStop", "s1", env=self.session_env, agent_id="agent-a")
        self.wait_for(lambda: self.state_of(entry["id"]) == "reaped", what="the subagent's stop to reap it")
        self.assertIsNotNone(shell.wait(10))
        self.assertFalse(self.runs(sleeper))
        out, _ = shell.communicate()
        self.assertEqual(b"started\n", out, "the agent sees its command's output and nothing of junkfather's")

    def test_the_command_runs_and_exits_as_it_would_without_junkfather(self):
        shell = self.run_as_claude(self.rewritten(call("echo out; echo err >&2; (exit 3)")))
        out, err = shell.communicate(timeout=30)
        self.assertEqual((b"out\n", b"err\n", 3), (out, err, shell.returncode))
        (entry,) = map(plain, self.store.entries())
        self.assertEqual(("agent-a", shell.pid), (entry["owner"]["agent"], entry["pid"]))

    def test_its_ttl_reaps_it_when_the_subagent_never_stops(self):
        self.cli("settings", "set", "own.background_ttl", "30m")
        shell = self.run_as_claude(self.rewritten(call("sleep 600")))
        entry = self.adopted()
        self.assertEqual(1800, entry["ttl"], "the own.background_ttl setting")
        self.store.update(entry["id"], lambda e: e.update(expires=time.time() - 1))  # its TTL passed
        swept = json.loads(self.cli("sweep", "--json").stdout)
        self.assertEqual([(entry["id"], "ttl expired")], [(r["id"], r["reason"]) for r in swept["reaped"]])
        self.assertIsNotNone(shell.wait(10))

    def test_a_main_session_call_belongs_to_the_session_and_has_no_ttl(self):
        shell = self.run_as_claude(self.rewritten(call("sleep 600", agent=None)))
        entry = self.adopted()
        self.assertEqual(("s1", None, True), (entry["owner"]["session"], entry["owner"]["agent"], entry["no_ttl"]))
        self.assertEqual((defaults.NO_TTL, entry["created"] + defaults.NO_TTL), (entry["ttl"], entry["expires"]),
                         "a hundred years on: older readers see it far from expiry")
        self.store.update(entry["id"], lambda e: e.update(created=time.time() - 3 * 86400))  # three days on
        self.hook("Stop", "s1", env=self.session_env)
        self.assertEqual([], json.loads(self.cli("sweep", "--json").stdout)["reaped"], "no TTL to expire")
        self.assertEqual("running", self.state_of(entry["id"]))
        (row,) = json.loads(self.cli("list", "--json").stdout)["entries"]
        self.assertTrue(row["no_ttl"])
        self.hook("SessionEnd", "s1", env=self.session_env, reason="other")
        self.wait_for(lambda: self.state_of(entry["id"]) == "reaped", what="the session end")
        self.assertIsNotNone(shell.wait(10))

    def test_its_reap_leaves_what_another_entry_owns_inside_it(self):
        """A script in the background shell runs an attached `junkfather own --keep-for session`: that entry
        survives the subagent's stop, though its junkfather runs in the shell's group."""
        env = {**self.session_env, "JF": f"{sys.executable} -m junkfather", "PYTHONPATH": str(ROOT)}
        shell = self.run_as_claude(self.rewritten(call("$JF own --keep-for session --ttl 1h -- sleep 601")), env)
        self.wait_for(lambda: len([e for e in self.store.entries() if e.get("state") == "running"]) == 2,
                      what="both entries")
        auto, kept = sorted(map(plain, self.store.entries()), key=lambda e: e.get("auto") != "background")
        self.assertEqual(("background", "session"), (auto["auto"], kept["keep"]))
        self.assertEqual(shell.pid, procs.Table.read().get(kept["supervisor"]["pid"]).pgid,  # type: ignore[union-attr]
                         "the attached junkfather is in the shell's group")
        self.hook("SubagentStop", "s1", env=self.session_env, agent_id="agent-a")
        self.wait_for(lambda: self.state_of(auto["id"]) == "reaped", what="the background entry reaped")
        self.assertEqual("running", self.state_of(kept["id"]))
        self.assertTrue(self.runs(kept["pid"]))
        self.assertTrue(self.runs(kept["supervisor"]["pid"]))
        self.assertEqual(0, self.cli("reap", "--id", kept["id"], env=self.session_env).returncode)

    def test_the_adopting_line_owns_only_its_own_shell_once(self):
        claim = self.rewritten(call("sleep 600")).split("\n", 1)[0]
        stranger = self.spawn("sleep", "602", start_new_session=True)
        replay = self.run_as_claude(claim.replace(" >/dev/null 2>&1 </dev/null", "") + " " + str(stranger.pid))
        replay.communicate(timeout=30)
        self.assertNotEqual(0, replay.returncode, "background takes no pid: it adopts the shell that runs it")
        first = self.run_as_claude(claim + "\nsleep 600")
        self.adopted()
        second = self.run_as_claude(claim + "\nsleep 600")
        self.runs_600(second, below=True)
        self.assertEqual([first.pid], [e["pid"] for e in self.store.entries()], "a claim adopts one shell")

    def test_an_unknown_claim_owns_nothing(self):
        line = background.adopting_line("toolu_unknown", background.launcher())
        shell = self.run_as_claude(line + "\nexit 0")
        shell.communicate(timeout=30)
        self.assertEqual(([], 0), (self.store.entries(), shell.returncode))

    def test_a_shell_that_ended_is_gone_at_once(self):
        done = self.run_as_claude(self.rewritten(call("true")))
        done.communicate(timeout=30)
        (ended,) = map(plain, self.store.entries())
        self.wait_for(lambda: self.state_of(ended["id"]) == "gone", what="the watcher to mark it gone",
                      seen=lambda: str(self.loaded(ended["id"])))

    def test_what_the_command_leaves_or_execs_is_still_its_own(self):
        """`exec` turns the shell into another program; `&` leaves a child once the shell exits. The watcher records
        both, by the kernel's word (kqueue) or by looking every few seconds (poll), so the stop still ends them."""
        for mode in ("kqueue", "poll") if hasattr(select, "kqueue") else ("poll",):
            for n, command in enumerate(("exec sleep 600", "sleep 600 &")):
                with self.subTest(mode=mode, command=command):
                    self.stopped_with_its_agent(mode, f"agent-{mode}-{n}", command)

    def stopped_with_its_agent(self, mode: str, agent: str, command: str) -> None:
        env = {**self.session_env, "JUNKFATHER_WATCH": mode}
        shell = self.run_as_claude(self.rewritten(call(command, agent=agent, tool_use=f"toolu_{agent}")), env)
        self.wait_for(lambda: any(e.get("owner", {}).get("agent") == agent and e.get("supervisor")
                                  for e in self.store.entries()), what="the watched entry")
        (entry,) = [plain(e) for e in self.store.entries() if e["owner"]["agent"] == agent]
        if command.endswith("&"):
            self.assertIsNotNone(shell.wait(30), "the shell exits and leaves its child")

        def sleepers() -> List[int]:
            seen: List[List[Any]] = list(self.loaded(entry["id"]).get("known_seen") or [])
            return [int(item[0]) for item in seen if str(item[2]).endswith("sleep 600")]
        self.wait_for(sleepers, what="the watcher to record the sleep", seen=lambda: str(self.loaded(entry["id"])))
        (sleeper,) = sleepers()
        self.assertTrue(self.runs(sleeper))
        self.hook("SubagentStop", "s1", env=self.session_env, agent_id=agent)
        self.wait_for(lambda: self.state_of(entry["id"]) == "reaped", what="the stop to reap it",
                      seen=lambda: str(self.loaded(entry["id"])))
        self.wait_for(lambda: not self.runs(sleeper), what="the sleep to end")
