"""Focused unit tests for Tidy 6 seams (fathers#74, fathers#216): github.py, review_gate.py, review_record._transition,
lock-free gh in done, and acyclic top-level imports among lanes, review, done, tickets, checks, history and
review_record."""
from __future__ import annotations

import ast
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest

from support import FIXTURE_CONFIG, LaneCase, ScratchCase, close_process

from lanefather import config, github

PKG_DIR = Path(__file__).resolve().parent.parent / "lanefather"
CORE_MODULES = {
    "lanes",
    "review",
    "done",
    "tickets",
    "checks",
    "history",
    "review_record",
    "lifecycle",
    "postmerge",
    "land",
}


def _imported_internal_modules(path: Path) -> tuple[set[str], set[str]]:
    """Return `(top_level, local)` relative module names imported from `lanefather` in `path`."""
    tree = ast.parse(path.read_text(), filename=str(path))
    top: set[str] = set()
    local: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            names = [a.name for a in node.names] if node.module is None else [node.module]
            target = top if node.col_offset == 0 else local
            target.update(names)
    return top, local


class Tidy6ArchitectureTests(unittest.TestCase):
    def test_no_gh_subprocess_outside_github_module(self) -> None:
        for py_file in sorted(PKG_DIR.glob("*.py")):
            if py_file.name == "github.py":
                continue
            tree = ast.parse(py_file.read_text(), filename=str(py_file))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    if (isinstance(func, ast.Attribute) and func.attr in ("run", "Popen")
                            and isinstance(func.value, ast.Name) and func.value.id == "subprocess"):
                        src = ast.get_source_segment(py_file.read_text(), node) or ""
                        self.assertNotIn("gh", src.lower(), f"{py_file.name} invokes gh outside github.py: {src}")

    def test_no_import_cycle_or_local_import_among_core_modules(self):
        graph: dict[str, set[str]] = {}
        for mod in CORE_MODULES:
            top, local = _imported_internal_modules(PKG_DIR / f"{mod}.py")
            local_core = local & CORE_MODULES
            self.assertEqual(local_core, set(), f"{mod}.py has function-local imports: {local_core}")
            graph[mod] = top & CORE_MODULES

        visiting: set[str] = set()
        visited: set[str] = set()

        def dfs(node: str, trail: list[str]) -> None:
            if node in visited:
                return
            visiting.add(node)
            for nxt in graph[node]:
                if nxt in visiting:
                    self.fail(f"import cycle among core modules: {' -> '.join([*trail, nxt])}")
                dfs(nxt, [*trail, nxt])
            visiting.remove(node)
            visited.add(node)

        for mod in sorted(CORE_MODULES):
            dfs(mod, [mod])

        history_top, history_local = _imported_internal_modules(PKG_DIR / "history.py")
        self.assertEqual(history_local, set(), f"history.py has local imports: {history_local}")
        self.assertNotIn("review", history_top | history_local)
        self.assertIn("review_record", history_top)

    def test_review_receipts_written_only_by_review_record_transition(self):
        writers: list[tuple[str, str]] = []
        for py_file in sorted(PKG_DIR.glob("*.py")):
            tree = ast.parse(py_file.read_text(), filename=str(py_file))
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef):
                    for inner in ast.walk(node):
                        if not isinstance(inner, ast.Call) or not isinstance(inner.func, ast.Attribute):
                            continue
                        if (inner.func.attr == "write"
                                and isinstance(inner.func.value, ast.Name)
                                and inner.func.value.id == "receipts"):
                            kinds = [
                                *(inner.args[3:4]),
                                *(kw.value for kw in inner.keywords if kw.arg == "kind"),
                            ]
                            for kind_arg in kinds:
                                if isinstance(kind_arg, ast.Constant) and kind_arg.value == "review":
                                    self.fail(f"{py_file.name}:{node.name} calls receipts.write(..., 'review')")
                        if inner.func.attr == "write_json":
                            target_src = ast.unparse(inner.args[0]) if inner.args else ""
                            if py_file.name == "review_record.py" or '"review"' in target_src or "'review'" in target_src:
                                writers.append((py_file.name, node.name))
        self.assertEqual(writers, [("review_record.py", "_transition")])

    def test_post_merge_and_recheck_receipts_written_only_by_postmerge(self):
        writers: list[tuple[str, str]] = []
        for py_file in sorted(PKG_DIR.glob("*.py")):
            tree = ast.parse(py_file.read_text(), filename=str(py_file))
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef):
                    for inner in ast.walk(node):
                        if not isinstance(inner, ast.Call) or not isinstance(inner.func, ast.Attribute):
                            continue
                        if (inner.func.attr == "write"
                                and isinstance(inner.func.value, ast.Name)
                                and inner.func.value.id == "receipts"):
                            kinds = [
                                *(inner.args[3:4]),
                                *(kw.value for kw in inner.keywords if kw.arg == "kind"),
                            ]
                            for kind_arg in kinds:
                                if isinstance(kind_arg, ast.Constant) and kind_arg.value in ("post-merge", "recheck"):
                                    self.fail(f"{py_file.name}:{node.name} calls receipts.write(..., {kind_arg.value!r})")
                        if inner.func.attr == "write_json":
                            target_src = ast.unparse(inner.args[0]) if inner.args else ""
                            if py_file.name == "postmerge.py" or any(
                                k in target_src for k in ('"post-merge"', "'post-merge'", '"recheck"', "'recheck'")
                            ):
                                writers.append((py_file.name, node.name))
        self.assertEqual(writers, [("postmerge.py", "_write_receipt")])


class GitHubAdapterTests(ScratchCase):
    def test_github_adapter_origin_repo_and_gh_command(self) -> None:
        repo = self.tmp / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "git@github.com:example/widgets.git"],
                       check=True, capture_output=True)
        cfg = config.load(self.write(repo, ".lanefather.toml", FIXTURE_CONFIG))
        common = str(repo / ".git")
        self.assertEqual(github.origin_repo(common), "example/widgets")
        self.assertEqual(github.repo_args(common), ["-R", "example/widgets"])
        self.assertEqual(github.repository(common, cfg.tickets), "example/widgets")
        self.assertTrue(github.gh_command(cfg).endswith("gh") or "gh" in github.gh_command(cfg))


class DoneLockFreeGhTests(LaneCase):
    def test_done_runs_gh_outside_state_lock_so_list_proceeds(self):
        # Land a lane with a ticket, then hold `gh` inside `done` and verify `list` takes the state lock meanwhile.
        self.lane("start", "a", "--ticket", "42", "--claims", "a.txt", "--create")
        wt = Path(self.record("a")["worktree"])
        (wt / "a.txt").write_text("hello\n")
        self.git(wt, "add", "a.txt")
        self.git(wt, "commit", "-m", "add a")
        self.lane("check", "a")
        self.lane("land", "a", "--wait", "0", "--keep")
        self.lane("review", "a")

        started = self.tmp / "gh-started"
        release = self.tmp / "gh-release"
        blocking_gh = self.tmp / "blocking-gh"
        blocking_gh.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys, time\n"
            "from pathlib import Path\n"
            f"Path({str(started)!r}).write_text('started')\n"
            f"deadline = time.monotonic() + 10\n"
            f"while not Path({str(release)!r}).exists() and time.monotonic() < deadline:\n"
            "    time.sleep(0.05)\n"
            "print(json.dumps({'comments': [{'createdAt': '2099-01-01T00:00:00Z', 'body': 'evidence'}]}))\n"
        )
        blocking_gh.chmod(0o755)

        env = {**os.environ, **self.env, "LANEFATHER_GH": str(blocking_gh)}
        proc = subprocess.Popen(
            [sys.executable, "-m", "lanefather", "done", "a"],
            cwd=self.repo,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(close_process, proc)
        try:
            deadline = time.monotonic() + 10
            while not started.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(started.exists(), "stand-in gh did not start")

            # `list` must acquire the state lock without blocking on `done`'s gh call.
            listed = subprocess.run(
                [sys.executable, "-m", "lanefather", "list", "--json"],
                cwd=self.repo,
                env=env,
                capture_output=True,
                text=True,
                timeout=3,
            )
            self.assertEqual(listed.returncode, 0, listed.stderr)
        finally:
            release.write_text("go")
            out, err = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, err + out)


if __name__ == "__main__":
    unittest.main()
