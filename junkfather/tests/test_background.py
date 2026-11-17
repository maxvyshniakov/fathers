"""A background Bash command owned on its own (fathers#166): the PreToolUse hook rewrites a `run_in_background` call
so that its shell adopts itself before the agent's command runs, survives a subagent turn, and is reaped at the
TTL or session end. A foreground call is left alone. The hook fixture runs the shell as Claude Code runs a Bash
call: in a process group of its own, running `eval '<command>' && pwd -P`."""
from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from support import ROOT, Case, as_entry, plain, proc, table

from junkfather import background, defaults, hook, procs
from junkfather.entry import Entry
from junkfather.state import Store


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
        here = procs.Table.read().get(shell.pid)
        self.addCleanup(self.end_group, shell.pid, here.start if here else "")
        return shell

    def end_group(self, group: int, start: str) -> None:
        """What the agent's command left in the shell's group: the processes of that group that started no earlier
        than the shell (a group id cannot be taken again while any of its processes run)."""
        began = procs.started_at(start)
        for member in procs.Table.read().group(group):
            if began is not None and member.started is not None and member.started >= began:
                try:
                    os.kill(member.pid, signal.SIGKILL)
                except OSError:
                    pass

    def adopted(self) -> Dict[str, Any]:
        self.wait_for(lambda: any(e.get("state") == "running" for e in self.store.entries()), what="the entry",
                      seen=lambda: (self.state / "hook.out").read_text() if (self.state / "hook.out").exists()
                      else "no entry")
        (entry,) = map(plain, self.store.entries())
        return entry

    def test_a_subagents_background_call_survives_its_turn_and_ends_with_the_session(self):
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
        self.assertFalse((self.state / "hook.out").exists(), "the turn did not start a reap")
        self.assertEqual("running", self.state_of(entry["id"]))
        self.assertTrue(self.runs(sleeper))
        self.hook("SessionEnd", "s1", env=self.session_env, reason="other")
        self.wait_for(lambda: self.state_of(entry["id"]) == "reaped", what="the session end to reap it")
        self.assertIsNotNone(shell.wait(10))
        self.assertFalse(self.runs(sleeper))
        out, _ = shell.communicate()
        self.assertEqual(b"started\n", out, "the agent sees its command's output and nothing of junkfather's")

    def test_a_subagent_turn_reaps_explicit_ownership_and_spares_its_background_call(self):
        shell = self.run_as_claude(self.rewritten(call("sleep 600")))
        auto = self.adopted()
        sleeper = self.runs_600(shell, below=True)
        claim = call("junkfather own --detach --ttl 1h -- sleep 601", background_=False, tool_use="toolu_explicit")
        hook.handle(claim, self.store)
        explicit = self.own_detached(self.session_env, "--ttl", "1h", argv=("sleep", "601"))
        self.assertEqual("agent-a", explicit["owner"]["agent"])

        def run_reaper(argv: List[str]) -> None:
            result = subprocess.run(argv, cwd=ROOT, env=self.session_env, capture_output=True, text=True, timeout=60)
            self.assertEqual(0, result.returncode, result.stderr)

        hook.handle({"hook_event_name": "SubagentStop", "session_id": "s1", "agent_id": "agent-a"}, self.store,
                    spawn=run_reaper)
        self.assertEqual("reaped", self.state_of(explicit["id"]))
        self.assertFalse(self.runs(explicit["pid"]))
        self.assertEqual("running", self.state_of(auto["id"]))
        self.assertTrue(self.runs(sleeper))
        manual = self.cli("reap", "--owner", "s1", "--agent", "agent-a", "--dry-run", "--json", env=self.session_env)
        self.assertEqual(0, manual.returncode, manual.stderr)
        self.assertEqual([auto["id"]], [r["id"] for r in json.loads(manual.stdout)["results"]])

    def test_the_command_runs_and_exits_as_it_would_without_junkfather(self):
        shell = self.run_as_claude(self.rewritten(call("echo out; echo err >&2; (exit 3)")))
        out, err = shell.communicate(timeout=30)
        self.assertEqual((b"out\n", b"err\n", 3), (out, err, shell.returncode))
        (entry,) = map(plain, self.store.entries())
        self.assertEqual(("agent-a", shell.pid), (entry["owner"]["agent"], entry["pid"]))

    def test_its_ttl_reaps_it_after_the_subagent_turn(self):
        self.cli("settings", "set", "own.background_ttl", "30m")
        shell = self.run_as_claude(self.rewritten(call("sleep 600")))
        entry = self.adopted()
        self.assertEqual(1800, entry["ttl"], "the own.background_ttl setting")
        self.store.update(entry["id"], lambda e: e.update(expires=time.time() - 1))  # its TTL passed
        self.hook("SubagentStop", "s1", env=self.session_env, agent_id="agent-a")
        self.assertFalse((self.state / "hook.out").exists(), "the turn left expiry to the sweep")
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
        survives a reap of the auto-owned entry, though its junkfather runs in the shell's group."""
        env = {**self.session_env, "JF": f"{sys.executable} -m junkfather", "PYTHONPATH": str(ROOT)}
        shell = self.run_as_claude(self.rewritten(call("$JF own --keep-for session --ttl 1h -- sleep 601")), env)
        self.wait_for(lambda: len([e for e in self.store.entries() if e.get("state") == "running"]) == 2,
                      what="both entries")
        auto, kept = sorted(map(plain, self.store.entries()), key=lambda e: e.get("auto") != "background")
        self.assertEqual(("background", "session"), (auto["auto"], kept["keep"]))
        self.assertEqual(shell.pid, procs.Table.read().get(kept["supervisor"]["pid"]).pgid,  # type: ignore[union-attr]
                         "the attached junkfather is in the shell's group")
        self.assertEqual(0, self.cli("reap", "--id", auto["id"], env=self.session_env).returncode)
        self.assertEqual("reaped", self.state_of(auto["id"]))
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
        both, by the kernel's word (kqueue) or by looking every few seconds (poll), so session end still reaps them."""
        precise = [mode for mode, here in (("kqueue", hasattr(select, "kqueue")), ("pidfd", hasattr(os, "pidfd_open")))
                   if here]
        cases = [(mode, command) for mode in precise for command in ("exec sleep 600", "sleep 600 &")]
        for n, (mode, command) in enumerate([*cases, ("poll", "exec sleep 600")]):  # poll sees no exit as it happens
            with self.subTest(mode=mode, command=command):
                self.reaped_at_session_end(mode, f"agent-{mode}-{n}", command)

    def test_a_child_left_behind_that_execs_later_is_still_its_own(self):
        """The shell leaves `(sleep 1; exec sleep 600) &` and exits; a second later the child becomes `sleep 600`.
        The watcher keeps watching what it recorded, so session end still reaps it."""
        modes = [m for m, here in (("kqueue", hasattr(select, "kqueue")), ("pidfd", hasattr(os, "pidfd_open"))) if here]
        for n, mode in enumerate(modes):
            with self.subTest(mode=mode):
                self.reaped_at_session_end(mode, f"agent-late-{n}", "(sleep 1; exec sleep 600) &")

    def test_an_entry_adopted_inside_the_shell_is_reaped_by_its_own_reap(self):
        shell = self.run_as_claude(self.rewritten(call("sleep 600; :")))
        outer = self.adopted()
        child = self.runs_600(shell, below=True)
        result = self.cli("adopt", str(child), "--ttl", "1h", env=self.session_env)
        self.assertEqual(0, result.returncode, result.stderr)
        (inner,) = [plain(e) for e in self.store.entries() if e["id"] != outer["id"]]
        self.assertEqual(0, self.cli("reap", "--id", inner["id"], env=self.session_env).returncode)
        self.wait_for(lambda: not self.runs(child), what="the inner entry's own reap to end its process")
        self.assertEqual("reaped", self.state_of(inner["id"]))

    def reaped_at_session_end(self, mode: str, agent: str, command: str) -> None:
        env = {**self.session_env, "JUNKFATHER_WATCH": mode}
        shell = self.run_as_claude(self.rewritten(call(command, agent=agent, tool_use=f"toolu_{agent}")), env)
        self.wait_for(lambda: any(e.get("owner", {}).get("agent") == agent and e.get("supervisor")
                                  for e in self.store.entries()), what="the watched entry")
        (entry,) = [plain(e) for e in self.store.entries() if e["owner"]["agent"] == agent]
        if command.endswith("&"):
            self.assertIsNotNone(shell.wait(30), "the shell exits and leaves its child")

        def sleepers() -> List[int]:
            seen: List[List[Any]] = list(self.loaded(entry["id"]).get("known_seen") or [])
            return [int(item[0]) for item in seen if str(item[2]) == "sleep 600"]  # not the shell naming it
        self.wait_for(sleepers, what="the watcher to record the sleep", seen=lambda: str(self.loaded(entry["id"])))
        (sleeper,) = sleepers()
        self.assertTrue(self.runs(sleeper))
        self.hook("SessionEnd", "s1", env=self.session_env, reason="other")
        self.wait_for(lambda: self.state_of(entry["id"]) == "reaped", what="session end to reap it",
                      seen=lambda: str(self.loaded(entry["id"])))
        self.wait_for(lambda: not self.runs(sleeper), what="the sleep to end",
                      seen=lambda: f"{self.loaded(entry['id'])}\n{procs.Table.read().get(sleeper)}")


class FakeEyes:
    """The watcher's event source as a test scripts it: each wait answers the next list of events, then quiet."""

    precise = True

    def __init__(self, *rounds: List[Tuple[str, int]]):
        self.rounds = list(rounds)

    def add(self, pid: int) -> bool:
        return pid in procs.identities([pid])

    def wait(self, timeout: float) -> List[Tuple[str, int]]:
        return self.rounds.pop(0) if self.rounds else []


class WatcherTests(Case):
    """The watcher's decisions, with a scripted event source and real processes."""

    def entry(self, child: subprocess.Popen[Any], state: str = "running", seen: str = "sh -c ...", **extra: Any
              ) -> Dict[str, Any]:
        here = procs.Table.read().get(child.pid)
        assert here is not None
        self.store.save(as_entry({"id": "jw", "state": state, "mode": "adopted", "auto": "background", "keep": "owner",
                                  "pid": child.pid, "pgid": here.pgid, "group": here.pgid == child.pid,
                                  "start": here.start, "seen": seen, "known": [[child.pid, here.start]],
                                  "known_seen": [[child.pid, here.start, seen]], "created": time.time(),
                                  "expires": time.time() + 3600, "owner": {"kind": "claude", "session": "s1"},
                                  **extra}))
        return self.loaded("jw")

    def test_an_exec_the_reap_missed_is_stopped_by_the_watcher(self):
        """A reap ran between the shell's exec and the watcher's look: the entry is reaped, its leader runs as
        another program. The watcher stops it, by the pid and start time it watched."""
        child = self.spawn("sleep", "600", start_new_session=True)
        entry = self.entry(child)
        self.store.update("jw", lambda e: e.update(state="reaped"))
        background.watch(self.store, "jw", child.pid, entry["start"], eyes=FakeEyes([("exec", child.pid)]))
        self.assertIsNotNone(child.wait(15), "the exec'd leader was stopped")

    def test_an_ended_entry_whose_shell_another_entry_holds_lets_its_watcher_go(self):
        child = self.spawn("sleep", "600", start_new_session=True)
        entry = self.entry(child)
        self.store.save(as_entry({**entry, "id": "jx", "auto": None, "seen": "sleep 600", "created": time.time() + 1,
                                  "known_seen": [[child.pid, entry["start"], "sleep 600"]]}))  # holds it too
        self.store.update("jw", lambda e: e.update(state="reaped"))
        background.watch(self.store, "jw", child.pid, entry["start"], eyes=FakeEyes([]))  # a quiet wake: it returns
        self.assertTrue(self.runs(child.pid), "the other entry's process is left to it")

    def test_only_what_is_tied_to_the_shell_joins(self):
        shell = proc(500, pgid=500, start="Mon Nov 16 10:00:00 2026", cmd="sh -c x")
        entry = as_entry({"id": "jw", "pid": 500, "pgid": 500, "group": True, "start": shell.start, "known": []})
        child = proc(501, ppid=500, pgid=500, start="Mon Nov 16 10:00:01 2026")
        orphan = proc(502, ppid=1, pgid=500, start="Mon Nov 16 10:00:02 2026")
        late = proc(503, ppid=1, pgid=500, start="Mon Nov 16 10:00:09 2026")
        exited_at = procs.started_at("Mon Nov 16 10:00:05 2026") or 0.0
        with_shell = table(shell, child, orphan)
        self.assertEqual([501, 502], sorted(p.pid for p in background._joining(  # pyright: ignore[reportPrivateUsage]
            self.store, entry, with_shell, {500: (shell.start, 500)}, False, exited_at)), "the shell's group")
        after = table(orphan, late)
        self.assertEqual([502], [p.pid for p in background._joining(  # pyright: ignore[reportPrivateUsage]
            self.store, entry, after, {}, True, exited_at)], "the group as the kernel saw it end: not what came later")
        self.assertEqual([], background._joining(  # pyright: ignore[reportPrivateUsage]
            self.store, entry, after, {}, False, exited_at), "no witness and no exit seen: nothing")
        reused = table(proc(500, pgid=500, start="Mon Nov 16 11:00:00 2026", cmd="vim"), orphan)
        self.assertEqual([], background._joining(  # pyright: ignore[reportPrivateUsage]
            self.store, entry, reused, {}, True, exited_at), "the group's id leads another process: nothing")

    def test_a_reap_takes_the_members_recorded_while_it_read(self):
        """The watcher records the exec'd leader's command after the reaper read the entry and before it marked it
        reaping: the reaper goes by the entry as marked, so the exec'd leader is stopped."""
        from unittest import mock
        from junkfather import entry as entry_module, reap
        child = self.spawn("sleep", "600", start_new_session=True)
        made = self.entry(child)
        real = entry_module.begin_reap

        def watcher_wrote_first(store: Store, entry_id: str, reason: str, table: Any = None) -> Any:
            def recorded(e: Entry) -> None:
                e["known_seen"] = [[child.pid, made["start"], "sleep 600"]]
            store.update(entry_id, recorded)
            return real(store, entry_id, reason, table)
        with mock.patch.object(entry_module, "begin_reap", side_effect=watcher_wrote_first):
            result = reap.reap(self.store, "jw", "subagent-stop", grace=1)
        self.assertEqual(("stopped", [child.pid]), (result["result"], result["pids"]))
        self.assertIsNotNone(child.wait(15))
