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

# The fixture's tools/checks.py: one check, `unit`, which passes while the committed file `verdict` says pass.
STUB = '''import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import verify
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE = "origin/main"
NAMES = ["unit"]
class Selection:
    def __init__(self, paths):
        self.checks = {"unit": sorted(paths)}
def select(paths):
    return Selection(paths)
if __name__ == "__main__" and sys.argv[1:] == ["verify"]:
    verdict = (ROOT / "verdict").read_text().strip()
    base = os.environ.get("FATHERS_CHECK_BASE")
    code = f"print('unit ran with base', {base!r}); raise SystemExit({verdict!r} != 'pass')"
    step = verify.Step("unit", [sys.executable, "-c", code], 0.1, 1, ROOT)
    sys.exit(verify.gate([step], root=ROOT))
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
        hook = ROOT / ".githooks/pre-push"
        if hook.read_bytes().startswith(b"#!/usr/bin/env python3"):
            shutil.copy(hook, self.root / ".githooks/pre-push")
        else:  # before the switch to the Python hook: the same three lines
            (self.root / ".githooks/pre-push").write_text(
                "#!/usr/bin/env python3\nfrom pathlib import Path\nimport sys\n"
                "sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))\nimport prepush\n"
                "sys.exit(prepush.main(sys.stdin.read(), sys.argv))\n")
            (self.root / ".githooks/pre-push").chmod(0o755)
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
        self.git("fetch", "-q", "origin")
        second = self.push("main:refs/heads/copy")
        self.assertEqual(0, second.returncode, second.stderr)
        self.assertIn("already passed its checks", second.stdout)
        self.assertNotIn("unit ran", second.stdout)
        self.git("commit", "-q", "--amend", "-m", "the same tree")  # another commit, the same tree
        third = self.push("HEAD:refs/heads/amended")
        self.assertIn("already passed its checks", third.stdout)
        self.assertNotIn("unit ran", third.stdout)

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
        sha = self.git("rev-parse", "HEAD")
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self.assertEqual(set(), _pending(self.root, env, sha))
        with open(self.root / "tools/checks.py", "a") as stub:
            stub.write("# changed in the checkout, not in the commit\n")
        self.assertIsNone(_pending(self.root, env, sha))


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
