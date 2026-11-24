"""tools/prepush.py, the pre-push gate (fathers#235): which pushed commits it checks, its verified-tree shortcut, and
whole pushes to a local bare remote through a hook whose tools/checks.py is a stub that passes or fails."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import prepush  # tools/prepush.py, beside this file

ROOT = Path(__file__).resolve().parents[1]
ZERO = "0" * 40
A, B = "a" * 40, "b" * 40

# The fixture's tools/checks.py: `unit` passes while the committed file `verdict` says pass; `always_ok` passes when
# `two_steps` exists. A change selects `unit` (and `always_ok` when `two_steps` exists), while no change (a commit
# already on origin/main) selects every check, `other` too, as tools/checks.py does.
STUB = '''import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import verify
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE = "origin/main"
NAMES = ["unit", "other"]
class Selection:
    def __init__(self, paths):
        self.checks = {"unit": sorted(paths)}
        self.unowned = []
def select(paths):
    return Selection(paths)
def cache_keys(root, tree, names, *, full_tree=False, env=None):
    if full_tree:
        return {name: tree for name in names}
    steps = [verify.Step(name, [], inputs=("verdict", "notes.txt") if name == "unit" else ()) for name in names]
    return verify.tree_keys(root, tree, steps, env=env)
if __name__ == "__main__" and sys.argv[1:] == ["verify", "--keep-going"]:
    verdict = (ROOT / "verdict").read_text().strip()
    base = os.environ.get("FATHERS_CHECK_BASE")
    code = f"print('unit ran with base', {base!r}); raise SystemExit({verdict!r} != 'pass')"
    steps = [verify.Step("unit", [sys.executable, "-c", code], 0.1, 1, ROOT, inputs=("verdict", "notes.txt"))]
    if (ROOT / "two_steps").exists():
        steps.append(verify.Step("always_ok", [sys.executable, "-c", "print('always_ok ran')"], 0.1, 1, ROOT))
    sys.exit(verify.gate(steps, root=ROOT, keep_going=True))
if __name__ == "__main__":
    sys.exit(2)
'''


class TipsTests(unittest.TestCase):
    def test_each_pushed_branch_tip_once_without_deletions_and_tags(self) -> None:
        stdin = (f"refs/heads/x {A} refs/heads/x {ZERO}\n"
                 f"refs/heads/y {A} refs/heads/main {B}\n"
                 f"(delete) {ZERO} refs/heads/old {B}\n"
                 f"refs/tags/v1 {B} refs/tags/v1 {ZERO}\n"
                 f"refs/heads/z {B} refs/tags/z {ZERO}\n"
                 "\n")
        self.assertEqual([A], prepush.tips(stdin))
        self.assertEqual([], prepush.tips(""))


class PushTests(unittest.TestCase):
    """Pushes from a fixture checkout whose .githooks/pre-push and tools/ are this repository's, with a stub
    tools/checks.py, to a bare remote: the hook really runs."""

    def setUp(self) -> None:
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        self.remote, self.root = folder / "remote.git", folder / "checkout"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.remote)], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.root)], check=True)
        (self.root / "tools").mkdir()
        (self.root / ".githooks").mkdir()
        for name in ("verify.py", "prepush.py"):
            shutil.copy(ROOT / "tools" / name, self.root / "tools" / name)
        shutil.copy(ROOT / ".githooks/pre-push", self.root / ".githooks/pre-push")
        (self.root / "tools/checks.py").write_text(STUB)
        (self.root / "verdict").write_text("pass\n")
        (self.root / ".gitignore").write_text("__pycache__/\n")
        self.git("remote", "add", "origin", str(self.remote))
        self.git("config", "core.hooksPath", ".githooks")
        self.git("add", ".")
        self.git("commit", "-qm", "base")

    def git(self, *arguments: str) -> str:
        return subprocess.run(["git", "-C", str(self.root), "-c", "user.name=Fixture", "-c",
                               "user.email=fixture@example.invalid", *arguments], check=True, capture_output=True,
                              text=True).stdout.strip()

    def push(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        env = {key: value for key, value in os.environ.items() if not key.startswith(("GIT_", "FATHERS_"))}
        env["SLOTFATHER_LEASE"] = "test"  # take no machine lease (the lease's argv has its own tests)
        return subprocess.run(["git", "-C", str(self.root), "push", "origin", *arguments], env=env,
                              capture_output=True, text=True, timeout=120)

    def worktrees(self) -> list[str]:
        return [line for line in self.git("worktree", "list", "--porcelain").splitlines()
                if line.startswith("worktree ")]

    def test_a_push_runs_the_commits_own_checks_once(self) -> None:
        first = self.push("main")
        self.assertEqual(0, first.returncode, first.stderr)
        self.assertIn("pre-push: tools/checks.py verify on", first.stdout)
        self.assertIn("[unit] unit ran with base None", first.stdout)
        self.assertEqual(1, len(self.worktrees()))
        (self.root / "notes.txt").write_text("a change\n")
        self.git("add", "notes.txt")
        self.git("commit", "-qm", "a change")
        change = self.push("main")
        self.assertEqual(0, change.returncode, change.stderr)
        self.assertIn("unit ran with base", change.stdout)
        # origin/main is that commit now: its base has no change, which selects every check, and only the record of
        # the commit itself says it passed (fathers#235).
        again = self.push("main:refs/heads/copy")
        self.assertEqual(0, again.returncode, again.stderr)
        self.assertIn("already passed its checks", again.stdout)
        self.assertNotIn("unit ran", again.stdout)
        self.git("commit", "-q", "--amend", "-m", "the same tree")  # another commit, the same tree and selection
        amended = self.push("HEAD:refs/heads/amended")
        self.assertEqual(0, amended.returncode, amended.stderr)
        self.assertIn("already passed its checks", amended.stdout)
        self.assertNotIn("unit ran", amended.stdout)

    def test_a_failing_commit_is_refused_and_its_worktree_removed(self) -> None:
        self.assertEqual(0, self.push("main").returncode)
        self.git("fetch", "-q", "origin")
        (self.root / "verdict").write_text("fail\n")
        self.git("commit", "-qam", "red")
        refused = self.push("main")
        self.assertEqual(1, refused.returncode)
        base = self.git("rev-parse", "origin/main")
        self.assertIn(f"[unit] unit ran with base {base}", refused.stdout)
        self.assertIn("tools/checks.py verify failed on", refused.stderr)
        self.assertIn("nothing was pushed", refused.stderr)
        self.assertEqual(base, subprocess.run(["git", "-C", str(self.remote), "rev-parse", "main"],
                                              capture_output=True, text=True, check=True).stdout.strip())
        self.assertEqual(1, len(self.worktrees()))
        directory = re.search(r"verify on \w+ in (\S+)", refused.stdout)
        assert directory, refused.stdout
        self.assertFalse(Path(directory[1]).exists())

    def test_uncommitted_files_cannot_make_a_commit_pass(self) -> None:
        (self.root / "verdict").write_text("fail\n")
        self.git("commit", "-qam", "red")
        (self.root / "verdict").write_text("pass\n")  # in the checkout only
        self.assertEqual(1, self.push("main").returncode)

    def test_deleting_a_branch_and_pushing_a_tag_check_nothing(self) -> None:
        self.assertEqual(0, self.push("main").returncode)
        (self.root / "verdict").write_text("fail\n")
        self.git("commit", "-qam", "red")
        self.git("tag", "red-tag")
        tag = self.push("refs/tags/red-tag")
        self.assertEqual(0, tag.returncode, tag.stderr)
        self.assertNotIn("unit ran", tag.stdout)
        deleted = self.push(":refs/heads/main")
        self.assertNotIn("unit ran", deleted.stdout)

    def test_a_commit_whose_rules_differ_from_the_hooks_is_always_checked(self) -> None:
        self.assertEqual(0, self.push("main").returncode)
        (self.root / "notes.txt").write_text("a change\n")
        self.git("add", "notes.txt")
        self.git("commit", "-qm", "a change")
        self.assertEqual(0, self.push("main").returncode)
        self.git("commit", "-q", "--amend", "-m", "the same tree, not pushed")  # no record of this commit itself
        sha = self.git("rev-parse", "HEAD")
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self.assertEqual(set(), _pending(self.root, env, sha))
        with open(self.root / "tools/checks.py", "a") as stub:
            stub.write("# changed in the checkout, not in the commit\n")
        self.assertIsNone(_pending(self.root, env, sha))

    def test_keep_going_records_passing_steps_even_when_another_step_fails(self) -> None:
        self.assertEqual(0, self.push("main").returncode)
        (self.root / "two_steps").write_text("1\n")
        (self.root / "verdict").write_text("fail\n")
        self.git("add", "two_steps", "verdict")
        self.git("commit", "-qm", "one fails, one passes")
        refused = self.push("main")
        self.assertEqual(1, refused.returncode)
        self.assertIn("[always_ok] always_ok ran", refused.stdout)
        # `always_ok` was recorded in fathers-verified despite `unit` failing.
        common = Path(self.git("rev-parse", "--path-format=absolute", "--git-common-dir"))
        record = (common / "fathers-verified").read_text(encoding="utf-8")
        self.assertIn("\talways_ok\n", record)

    def test_pending_uses_per_check_input_cache_keys(self) -> None:
        self.assertEqual(0, self.push("main").returncode)
        # Touch an unrelated file: `verdict` is unchanged, so `unit`'s subtree cache key still hits.
        (self.root / "unrelated.txt").write_text("hello\n")
        self.git("add", "unrelated.txt")
        self.git("commit", "-qm", "unrelated change")
        sha = self.git("rev-parse", "HEAD")
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self.assertEqual(set(), _pending(self.root, env, sha))
        again = self.push("main")
        self.assertEqual(0, again.returncode, again.stderr)
        self.assertIn("already passed its checks", again.stdout)
        self.assertNotIn("unit ran", again.stdout)


def _pending(root: Path, env: dict[str, str], sha: str) -> set[str] | None:
    """prepush.pending as the fixture's own hook computes it (its tools/ on the path, its stub checks.py)."""
    script = (f"import json, os, pathlib, sys; sys.path.insert(0, {str(root / 'tools')!r}); import prepush; "
              f"found = prepush.pending(pathlib.Path({str(root)!r}), dict(os.environ), {sha!r}); "
              "print(json.dumps(None if found is None else sorted(found)))")
    out = subprocess.run([sys.executable, "-c", script], cwd=root, env=env, capture_output=True, text=True,
                         check=True).stdout
    found = json.loads(out)
    return None if found is None else set(found)


if __name__ == "__main__":
    unittest.main()
