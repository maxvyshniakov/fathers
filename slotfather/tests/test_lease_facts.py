#!/usr/bin/env python3
"""Facts on leases (fathers#97): the git tree and HEAD of the request's directory and `--meta cache=` in the history,
the ask-vs-use hint of `run`, `stats --by family` asks, and another host's history kept beside this one's."""
from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path
from typing import Any

from test_slotfather import TOOL, SlotCase


def git(directory: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(directory), *args], capture_output=True, text=True, check=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}).stdout.strip()


class GitFactsTests(SlotCase):
    def run_in(self, cwd: Path, *options: str,
               command: tuple[str, ...] = ("true",)) -> subprocess.CompletedProcess[str]:
        return subprocess.run([TOOL, "run", "--label", "lane-97", *options, "--", *command], cwd=cwd, env=self.env,
                              capture_output=True, text=True, timeout=20)

    def last_history(self) -> Any:
        return [json.loads(line) for line in (self.state / "history.jsonl").read_text().splitlines()][-1]

    def repository(self) -> Path:
        repo = self.tmp / "repo"
        repo.mkdir()
        git(repo, "init", "-q")
        (repo / "a.txt").write_text("one\n")
        git(repo, "add", "a.txt")
        git(repo, "commit", "-q", "-m", "one")
        return repo

    def test_a_run_in_a_repository_records_its_head_tree_and_whether_tracked_files_changed(self):
        repo = self.repository()
        result = self.run_in(repo)
        self.assertEqual(0, result.returncode, result.stderr)
        head, tree = git(repo, "rev-parse", "HEAD"), git(repo, "rev-parse", "HEAD^{tree}")
        self.assertEqual({"head": head, "tree": tree, "dirty": False}, self.last_history()["git"])

        os.utime(repo / "a.txt", (1, 1))  # touched, same content: still clean (review: diff-index said dirty)
        self.assertEqual(0, self.run_in(repo).returncode)
        self.assertFalse(self.last_history()["git"]["dirty"])
        (repo / "a.txt").write_text("two\n")
        (repo / "sub").mkdir()
        self.assertEqual(0, self.run_in(repo / "sub").returncode)
        self.assertEqual({"head": head, "tree": tree, "dirty": True}, self.last_history()["git"],
                         "a subdirectory names its repository; a changed tracked file makes it dirty")
        self.assertFalse((repo / ".git" / "index.lock").exists())

    def test_the_command_sees_the_facts_on_its_entry_while_it_runs(self):
        repo = self.repository()
        result = self.run_in(repo, command=("sh", "-c", 'cat "$SLOTFATHER_LEASE"'))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(git(repo, "rev-parse", "HEAD^{tree}"), json.loads(result.stdout)["git"]["tree"])

    def test_outside_a_repository_and_in_one_without_commits_nothing_is_recorded(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        self.assertEqual(0, self.run_in(plain).returncode)
        self.assertNotIn("git", self.last_history())
        empty = self.tmp / "empty"
        empty.mkdir()
        git(empty, "init", "-q")
        self.assertEqual(0, self.run_in(empty).returncode)
        self.assertNotIn("git", self.last_history())

    def test_git_facts_can_be_turned_off(self):
        repo = self.repository()
        self.env["SLOTFATHER_GIT_FACTS"] = "0"
        self.assertEqual(0, self.run_in(repo).returncode)
        self.assertNotIn("git", self.last_history())

    def test_cache_meta_takes_hit_miss_or_partial(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        result = self.run_in(plain, "--meta", "cache=hit")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual({"cache": "hit"}, self.last_history()["meta"])
        bad = self.run_in(plain, "--meta", "cache=warm")
        self.assertEqual(2, bad.returncode)
        self.assertIn("cache takes hit, miss or partial", bad.stderr)


def line(label: str, count: int, cpu: float, wall: float = 100, cores: int = 12, capacity: int = 4,
         ended: str = "2026-11-12T10:00:00Z", **extra: Any) -> dict[str, Any]:
    return {"v": 2, "label": label, "cmd": "./verify.sh", "cwd": "/w", "count": count, "pid": 2,
            "queued": "2026-11-12T09:00:00Z", "granted": "2026-11-12T09:01:00Z", "ended": ended,
            "outcome": "released", "waited": 60, "ran": wall, "status": 0, "cores": cores, "capacity": capacity,
            "cpu": cpu, "cpu_detached": 0.0, "wall": wall, "rss_peak_mb": 10.0, "rss_mean_mb": 5.0, "processes": 2,
            "samples": 3, **extra}


class AskUseTests(SlotCase):
    def history(self, items: list[dict[str, Any]]) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "history.jsonl").write_text("".join(json.dumps(i) + "\n" for i in items))

    def run_hint(self, count: int | str, label: str = "lane-300") -> list[str]:
        result = self.tool("run", "-n", str(count), "--label", label, "--", "true")
        self.assertEqual(0, result.returncode, result.stderr)
        return [line for line in result.stderr.splitlines() if "hint" in line]

    def test_run_hints_when_its_family_used_far_fewer_cores_than_it_asks(self):
        self.history([line(f"lane-{n}", 3, 160) for n in range(6)])  # 1.6 cores each, asking 9
        [hint] = self.run_hint(3)
        self.assertIn("lane", hint)
        self.assertIn("1.6 cores", hint)
        self.assertIn("-n 1", hint)
        self.assertEqual([], self.run_hint(1), "one slot is the least a request can ask")
        self.assertEqual([], self.run_hint("all"), "an all-slot request asks for the machine to itself")
        self.assertEqual([], self.run_hint(3, label="other-1"), "another family has no runs")

    def test_all_slot_runs_and_sessions_do_not_count_as_use(self):
        self.history([line(f"lane-{n}", 4, 50) for n in range(6)]
                     + [line(f"lane-{n}", 3, 50, session={"name": "s"}) for n in range(6)])
        self.assertEqual([], self.run_hint(3), "they ask for the machine, not for cores")

    def test_no_hint_with_few_runs_or_use_near_the_ask(self):
        self.history([line(f"lane-{n}", 3, 160) for n in range(4)])
        self.assertEqual([], self.run_hint(3), "four runs are too few to judge")
        self.history([line(f"lane-{n}", 3, 600) for n in range(6)])  # 6 cores of 9: more than half the ask
        self.assertEqual([], self.run_hint(3))
        self.history([line(f"lane-{n}", 3, 400) for n in range(6)])  # 4 cores: needs 2 slots, under half of 9
        [hint] = self.run_hint(3)
        self.assertIn("-n 2", hint)

    def test_stats_reports_asks_against_use_per_label_family(self):
        self.history([line("lane-1", 3, 160), line("lane-2", 3, 140), line("lane-3", 3, 180),
                      line("kit-wt-4", 1, 250), line("lane-5", 2, 100, outcome="left", cpu_detached=None)])
        summary = json.loads(self.tool("stats", "--json").stdout)
        asks = {row["family"]: row for row in summary["asks"]}
        lane = asks["lane"]
        self.assertEqual((3, 3, 1.6, 9.0, 1, True), (lane["measured"], lane["slots_p50"], lane["cores_p50"],
                                                     lane["cores_asked_p50"], lane["slots_needed"], lane["over_ask"]))
        self.assertFalse(asks["kit-wt"]["over_ask"])
        by_family = json.loads(self.tool("stats", "--json", "--by", "family").stdout)
        self.assertEqual({"lane": 4, "kit-wt": 1}, {g["family"]: g["requests"] for g in by_family["groups"]})
        text = self.tool("stats").stdout
        self.assertIn("lane", text.split("asks far above use")[1])


class HostHistoryTests(SlotCase):
    def test_export_gives_both_history_files_oldest_first(self):
        self.state.mkdir(parents=True)
        (self.state / "history.jsonl.1").write_text(json.dumps(line("a-1", 1, 10)) + "\n")
        (self.state / "history.jsonl").write_text("torn{\n" + json.dumps(line("b-1", 1, 10)) + "\n")
        result = self.tool("history", "export")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(["a-1", "b-1"], [json.loads(x)["label"] for x in result.stdout.splitlines()])

    def import_lines(self, host: str, items: list[dict[str, Any]], raw: str = "") -> subprocess.CompletedProcess[str]:
        data = raw + "".join(json.dumps(i) + "\n" for i in items)
        return subprocess.run([TOOL, "history", "import", host], input=data, env=self.env, capture_output=True,
                              text=True, timeout=20)

    def test_import_keeps_another_hosts_history_once_beyond_its_rotation(self):
        first = [line("ci-1", 2, 100, ended="2026-11-10T10:00:00Z", cores=6, capacity=2),
                 line("ci-2", 2, 100, ended="2026-11-11T10:00:00Z", cores=6, capacity=2)]
        result = self.import_lines("vm110", first, raw="not json\n[1]\n")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("2 new", result.stdout)
        later = [first[1], line("ci-3", 2, 50, ended="2026-11-12T10:00:00Z", cores=6, capacity=2)]
        result = self.import_lines("vm110", later)
        self.assertIn("1 new", result.stdout)
        archive = self.state / "hosts" / "vm110" / "history.jsonl"
        self.assertEqual(["ci-1", "ci-2", "ci-3"], [json.loads(x)["label"] for x in archive.read_text().splitlines()])
        summary = json.loads(self.tool("stats", "--host", "vm110", "--json").stdout)
        self.assertEqual((3, 6, 2, "vm110"), (summary["overall"]["requests"], summary["cores"], summary["capacity"],
                                              summary["host"]))
        self.assertIn("vm110", self.tool("stats", "--host", "vm110").stdout)
        self.assertFalse((self.state / "history.jsonl").exists(), "this host's own history is untouched")

    def test_import_refuses_a_host_name_that_is_not_a_plain_name(self):
        for bad in ("../x", "VM 110", ""):
            result = self.import_lines(bad, [line("ci-1", 1, 1)])
            self.assertEqual(2, result.returncode, bad)
        self.assertEqual(2, self.tool("stats", "--host", "nowhere").returncode)


if __name__ == "__main__":
    unittest.main()
