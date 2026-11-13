"""The process-safety policy (safety.py) through the CLI: every refusal of `adopt` and `reap --pid --force` keeps its
message and exit code, with real child processes (a terminal's, a stand-in agent session, a tree holding one), and
no module but safety.py matches the agent patterns."""
from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
from typing import Any, List
import unittest

from support import ROOT, Case, proc, table

from junkfather import procs, safety


class RefusalTests(Case):
    def setUp(self) -> None:
        super().setUp()
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        self.claude = bin_dir / "claude"  # its command line reads `.../claude 600`: an agent session by its command
        self.claude.symlink_to("/bin/sleep")

    def started(self, child: subprocess.Popen[Any], below: bool = False) -> int:
        """The pid of `child`, or with `below` of its child, once ps shows it running `600`."""
        found: List[int] = []

        def ready() -> bool:
            here = procs.Table.read()
            candidates = here.descendants(child.pid) if below else [here.get(child.pid)]
            found[:] = [p.pid for p in candidates if p is not None and p.cmd.endswith(" 600")]
            return bool(found)
        self.wait_for(ready, what="the child")
        return found[0]

    def reap_later(self, pid: int) -> None:
        """Kill a grandchild this test started when the test ends, if it is still that process."""
        start = procs.identities([pid]).get(pid)

        def kill() -> None:
            if start is not None and procs.identities([pid]).get(pid) == start:
                with contextlib.suppress(OSError):
                    os.kill(pid, 9)
        self.addCleanup(kill)

    def list_unowned(self, pid: int) -> None:
        current = procs.Table.read().get(pid)
        assert current is not None
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "unowned.json").write_text(json.dumps(
            {"v": 1, "items": [{"pid": pid, "start": current.start, "cmd": current.cmd}]}))

    def refused(self, *args: str) -> str:
        result = self.cli(*args)
        self.assertEqual(1, result.returncode, result.stderr)
        self.assertEqual("", result.stdout)
        return result.stderr

    def test_another_users_process_is_never_adopted(self):
        """Any process of another user (none under a single-user container: then there is nothing to test)."""
        other = next((p for p in procs.Table.read().procs.values() if p.uid != os.getuid() and p.pid > 1), None)
        if other is None:
            self.skipTest("no process of another user runs here")
        self.assertEqual(f"junkfather adopt: refusing pid {other.pid}: another user's process\n",
                         self.refused("adopt", str(other.pid)))

    def test_an_agent_session_is_never_adopted(self):
        agent = self.spawn(str(self.claude), "600", start_new_session=True)
        self.started(agent)
        self.assertEqual(f"junkfather adopt: refusing pid {agent.pid}: an agent session\n",
                         self.refused("adopt", str(agent.pid)))

    def test_a_process_with_a_terminal_is_never_adopted(self):
        script = self.spawn("script", "-q", "/dev/null", "sleep", "600", stdin=subprocess.PIPE,
                            start_new_session=True)
        sleeper = self.started(script, below=True)
        self.reap_later(sleeper)
        tty = (procs.Table.read().get(sleeper) or proc(0)).tty
        self.assertTrue(tty, "script gives its command a terminal")
        self.assertEqual(f"junkfather adopt: refusing pid {sleeper}: a process with a terminal ({tty})\n",
                         self.refused("adopt", str(sleeper)))

    def test_a_tree_holding_an_agent_is_neither_adopted_nor_force_stopped(self):
        # The shell's own command line must not read as an agent's: it names the path in two halves.
        shell = self.spawn("sh", "-c", '"${0}ude" 600; true', str(self.claude)[:-3], start_new_session=True)
        below = self.started(shell, below=True)
        self.reap_later(below)
        why = "its tree holds a terminal's process or an agent session"
        self.assertEqual(f"junkfather adopt: refusing pid {shell.pid}: {why}\n", self.refused("adopt", str(shell.pid)))
        self.list_unowned(shell.pid)
        self.assertEqual(f"junkfather reap: refusing pid {shell.pid}: {why}\n",
                         self.refused("reap", "--pid", str(shell.pid), "--force"))
        self.assertTrue(self.runs(shell.pid) and self.runs(below))

    def test_an_agent_listed_as_unowned_is_not_force_stopped(self):
        agent = self.spawn(str(self.claude), "600", start_new_session=True)
        self.started(agent)
        self.list_unowned(agent.pid)
        self.assertEqual(f"junkfather reap: refusing pid {agent.pid}: not a process junkfather may stop\n",
                         self.refused("reap", "--pid", str(agent.pid), "--force"))
        self.assertTrue(self.runs(agent.pid))

    def test_a_pid_an_entry_holds_while_it_reaps_is_not_force_stopped(self):
        child = self.spawn("sleep", "600", start_new_session=True)
        self.started(child)
        self.assertEqual(0, self.cli("adopt", str(child.pid), "--ttl", "1h").returncode)
        (entry,) = self.store.entries()
        self.store.update(entry["id"], lambda e: e.update(state="reaping"))  # another reaper holds it
        self.list_unowned(child.pid)
        self.assertEqual(f"junkfather reap: refusing pid {child.pid}: an entry owns it now (reap that entry)\n",
                         self.refused("reap", "--pid", str(child.pid), "--force"))
        self.assertTrue(self.runs(child.pid))


class PolicyTests(unittest.TestCase):
    def test_what_is_never_signalled(self):
        me = os.getpid()
        here = table(proc(1, ppid=0), proc(me, ppid=1), proc(100), proc(101, ppid=100, tty="ttys001"),
                     proc(102, ppid=101), proc(103, ppid=100, cmd="/usr/local/bin/codex"),
                     proc(104, uid=os.getuid() + 1), proc(105, ppid=100))
        chosen = safety.signallable([here.procs[p] for p in (100, 101, 102, 103, 104, 105, me, 1)], here)
        self.assertEqual([100, 105], [p.pid for p in chosen])
        self.assertTrue(safety.untouchable_tree(100, here))
        self.assertFalse(safety.untouchable_tree(105, here))
        self.assertEqual([100, 105], [p.pid for p in safety.forced_targets(100, here)])

    def test_an_unowned_resource_is_reported_never_more(self):
        self.assertIsNone(safety.unowned_resource({"kind": "docker", "id": "a"}, "docker:a", {"docker:a": "j1"}))
        self.assertEqual("a running docker no entry owns",
                         (safety.unowned_resource({"kind": "docker", "id": "b"}, "docker:b", {}) or {})["why"])

    def test_no_module_but_safety_matches_the_agent_patterns(self):
        """No module but safety.py names the patterns or compiles one of its own for an agent's command."""
        pattern = re.compile(r"\b(CLAUDE|CODEX)\b(?!_)|re\.compile\(.*(?i:claude|codex)")
        found = [f"{path.name}:{number}: {line.strip()}" for path in sorted((ROOT / "junkfather").glob("*.py"))
                 if path.name != "safety.py"
                 for number, line in enumerate(path.read_text().splitlines(), 1) if pattern.search(line)]
        self.assertEqual([], found)


if __name__ == "__main__":
    unittest.main()
