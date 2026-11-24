"""Tests for Lanefather's built-in verified-tree stamp ledger and CLI (lanefather.stamp, fathers#48)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from support import ROOT

from lanefather import config, gate, promote, stamp, verify

PACKAGE = ROOT


def _run_git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _run_git(path, "init", "-q", "-b", "main")
    _run_git(path, "config", "user.name", "Test")
    _run_git(path, "config", "user.email", "test@example.com")
    return path


def _commit_files(repo: Path, files: dict[str, str | None], message: str = "change") -> tuple[str, str]:
    for rel, content in files.items():
        target = repo / rel
        if content is None:
            target.unlink(missing_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-q", "-m", message)
    return _run_git(repo, "rev-parse", "HEAD"), _run_git(repo, "rev-parse", "HEAD^{tree}")


class StampUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="lanefather-stamp-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = _init_repo(self.tmp / "repo")
        self.common = Path(_run_git(self.repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))

    def test_record_and_exact_match_and_amend_invariance(self) -> None:
        sha1, tree1 = _commit_files(self.repo, {"app.py": "print('hi')\n"}, "initial")
        before = stamp.check_tree(str(self.repo), self.common, "HEAD")
        self.assertFalse(before.verified)
        self.assertIsNone(before.match)

        entry = stamp.add(self.common, tree1, sha1)
        self.assertEqual(entry.tree, tree1)
        self.assertEqual(entry.commit, sha1)

        exact = stamp.check_tree(str(self.repo), self.common, "HEAD")
        self.assertTrue(exact.verified)
        self.assertEqual(exact.match, "exact")
        self.assertEqual(exact.matched_tree, tree1)

        # Amending the commit message changes commit SHA but keeps tree hash identical.
        _run_git(self.repo, "commit", "--amend", "-q", "-m", "amended message")
        sha2 = _run_git(self.repo, "rev-parse", "HEAD")
        self.assertNotEqual(sha1, sha2)
        after_amend = stamp.check_tree(str(self.repo), self.common, sha2)
        self.assertTrue(after_amend.verified)
        self.assertEqual(after_amend.match, "exact")

    def test_md_only_change_counts_as_verified_and_code_change_invalidates(self) -> None:
        sha1, tree1 = _commit_files(self.repo, {"src/lib.py": "x = 1\n", "README.md": "v1\n"}, "base")
        stamp.add(self.common, tree1, sha1)

        # Adding, editing, and deleting .md files only still counts as verified.
        sha_md, tree_md = _commit_files(
            self.repo,
            {"README.md": "v2\n", "docs/GUIDE.MD": "guide\n"},
            "docs update",
        )
        self.assertNotEqual(tree_md, tree1)
        md_result = stamp.check_tree(str(self.repo), self.common, sha_md)
        self.assertTrue(md_result.verified)
        self.assertEqual(md_result.match, "md-only")
        self.assertEqual(md_result.matched_tree, tree1)
        self.assertEqual(set(md_result.md_paths), {"README.md", "docs/GUIDE.MD"})

        # A non-.md change invalidates the stamp, even alongside a .md change.
        sha_code, _ = _commit_files(
            self.repo,
            {"README.md": "v3\n", "src/lib.py": "x = 2\n"},
            "code change",
        )
        code_result = stamp.check_tree(str(self.repo), self.common, sha_code)
        self.assertFalse(code_result.verified)
        self.assertIsNone(code_result.match)

        # A .mdx file is not .md and must not count as .md-only.
        _run_git(self.repo, "reset", "--hard", "-q", sha_md)
        sha_mdx, _ = _commit_files(self.repo, {"docs/page.mdx": "# mdx\n"}, "mdx change")
        self.assertFalse(stamp.check_tree(str(self.repo), self.common, sha_mdx).verified)

    def test_missing_stamped_tree_in_ledger_is_skipped_cleanly(self) -> None:
        sha1, tree1 = _commit_files(self.repo, {"a.py": "1\n"}, "base")
        stamp.add(self.common, "0" * 40, "1" * 40, "2026-01-01T00:00:00Z")
        stamp.add(self.common, tree1, sha1)
        sha_md, _ = _commit_files(self.repo, {"NOTES.md": "hi\n"}, "notes")
        result = stamp.check_tree(str(self.repo), self.common, sha_md)
        self.assertTrue(result.verified)
        self.assertEqual(result.match, "md-only")
        self.assertEqual(result.matched_tree, tree1)

    def test_retention_and_transaction_abort_and_complete_match_fixture_contract(self) -> None:
        ledger_dir = self.tmp / "ledger"
        ledger_dir.mkdir()
        for index in range(52):
            stamp.add(ledger_dir, f"{index:040x}", f"{index:040x}", f"2026-01-01T00:00:{index % 60:02d}Z")
        entries = stamp.read_entries(ledger_dir)
        self.assertEqual(len(entries), 50)
        self.assertEqual(entries[0].tree, f"{2:040x}")
        self.assertEqual(entries[-1].tree, f"{51:040x}")

        token_a = "a" * 32
        token_b = "b" * 32
        before_rows = (ledger_dir / "verified-trees").read_text(encoding="utf-8").splitlines()
        stamp.add(ledger_dir, "c" * 40, "c" * 40, "2026-01-02T00:00:00Z", token=token_a)
        self.assertTrue(stamp.journal_path(ledger_dir, token_a).exists())
        # Duplicate token is refused.
        with self.assertRaisesRegex(ValueError, "already used"):
            stamp.add(ledger_dir, "d" * 40, "d" * 40, "2026-01-02T00:00:01Z", token=token_a)

        # Interleaved second writer commits and completes, then first aborts and restores its evicted row.
        stamp.add(ledger_dir, "e" * 40, "e" * 40, "2026-01-02T00:00:02Z", token=token_b)
        stamp.complete(ledger_dir, token_b)
        self.assertFalse(stamp.journal_path(ledger_dir, token_b).exists())

        stamp.abort(ledger_dir, token_a)
        self.assertFalse(stamp.journal_path(ledger_dir, token_a).exists())
        after_rows = (ledger_dir / "verified-trees").read_text(encoding="utf-8").splitlines()
        self.assertFalse(any(token_a in row for row in after_rows))
        self.assertIn(before_rows[0], after_rows)
        self.assertTrue(any(token_b in row for row in after_rows))

    def test_start_and_finish_enforce_clean_unchanged_tree_and_match_verify_regex(self) -> None:
        _, tree1 = _commit_files(self.repo, {"app.py": "v1\n"}, "base")
        snap = " ".join(stamp.snapshot(str(self.repo)))
        self.assertEqual(snap, f"{tree1} clean")

        # Clean run stamps and emits standard line parsed by verify.stamp_result.
        ok, reason, entry = stamp.finish(str(self.repo), self.common, snap)
        self.assertTrue(ok)
        self.assertEqual(reason, "")
        assert entry is not None
        line = stamp.stamp_line(entry.tree)
        self.assertEqual(verify.stamp_result(line + "\n", full=True), {"stamped": True, "tree": tree1})

        # Dirtying a tracked file during the run prevents stamping.
        (self.repo / "app.py").write_text("dirty\n", encoding="utf-8")
        ok_dirty, reason_dirty, entry_dirty = stamp.finish(str(self.repo), self.common, snap)
        self.assertFalse(ok_dirty)
        self.assertIsNone(entry_dirty)
        self.assertIn("tracked files changed during the run", reason_dirty)
        not_line = stamp.not_stamped_line(reason_dirty)
        self.assertEqual(
            verify.stamp_result(not_line + "\n", full=True),
            {"stamped": False, "reason": "tracked files changed during the run"},
        )

    def test_gate_and_promote_support_builtin_stamp_tool(self) -> None:
        sha1, tree1 = _commit_files(self.repo, {"verify.sh": "#!/bin/sh\nexit 0\n", "app.py": "1\n"}, "base")
        cfg = config.parse({"gate": {"stamp_tool": "builtin"}})
        self.assertTrue(promote.has_batch_gate(str(self.repo), sha1, cfg))
        self.assertFalse(gate.verified(str(self.repo), str(self.common), sha1, cfg))

        stamp.add(self.common, tree1, sha1)
        self.assertTrue(gate.verified(str(self.repo), str(self.common), sha1, cfg))

        # .md-only commit is verified under stamp_tool = "builtin" without an external pre-push script.
        sha_md, _ = _commit_files(self.repo, {"CHANGELOG.md": "- note\n"}, "docs")
        self.assertTrue(gate.verified(str(self.repo), str(self.common), sha_md, cfg))

        # Non-.md commit is not verified.
        sha_code, _ = _commit_files(self.repo, {"app.py": "2\n"}, "code")
        self.assertFalse(gate.verified(str(self.repo), str(self.common), sha_code, cfg))


class StampCLITests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="lanefather-stamp-cli-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = _init_repo(self.tmp / "repo")
        self.env = {
            k: v for k, v in os.environ.items()
            if not k.startswith(("LANEFATHER_", "STRAYLIGHT_"))
        }
        self.env["PYTHONPATH"] = str(PACKAGE)
        self.env["HOME"] = str(self.tmp)

    def run_cli(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "lanefather", *args],
            cwd=cwd or self.repo,
            env=self.env,
            capture_output=True,
            text=True,
        )

    def test_cli_record_check_list_and_json(self) -> None:
        sha1, tree1 = _commit_files(self.repo, {"main.py": "print(1)\n"}, "initial")

        # Unstamped check exits 1 with valid v=1 JSON.
        miss = self.run_cli("stamp", "check", "--json")
        self.assertEqual(miss.returncode, 1)
        miss_data = json.loads(miss.stdout)
        self.assertEqual(miss_data["v"], 1)
        self.assertFalse(miss_data["verified"])
        self.assertEqual(miss_data["tree"], tree1)

        # Record emits standard stamp line parseable by verify.stamp_result.
        rec = self.run_cli("stamp", "record")
        self.assertEqual(rec.returncode, 0, rec.stderr)
        self.assertEqual(verify.stamp_result(rec.stdout, full=True), {"stamped": True, "tree": tree1})

        # Check exits 0 for exact match.
        hit = self.run_cli("stamp", "check", sha1, "--json")
        self.assertEqual(hit.returncode, 0, hit.stderr)
        hit_data = json.loads(hit.stdout)
        self.assertEqual(hit_data["v"], 1)
        self.assertTrue(hit_data["verified"])
        self.assertEqual(hit_data["match"], "exact")

        # .md-only change exits 0 with match="md-only".
        _, tree_md = _commit_files(self.repo, {"README.md": "docs\n"}, "readme")
        md_hit = self.run_cli("stamp", "check", "--json")
        self.assertEqual(md_hit.returncode, 0, md_hit.stderr)
        md_data = json.loads(md_hit.stdout)
        self.assertEqual(md_data["match"], "md-only")
        self.assertEqual(md_data["tree"], tree_md)
        self.assertEqual(md_data["matched_tree"], tree1)
        self.assertEqual(md_data["md_paths"], ["README.md"])

        # List shows the recorded entry in text and JSON.
        listed = self.run_cli("stamp", "list", "--json")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        list_data = json.loads(listed.stdout)
        self.assertEqual(list_data["v"], 1)
        self.assertEqual(len(list_data["entries"]), 1)
        self.assertEqual(list_data["entries"][0]["tree"], tree1)

    def test_cli_start_finish_abort_and_usage_errors(self) -> None:
        _, tree1 = _commit_files(self.repo, {"main.py": "print(1)\n"}, "initial")
        started = self.run_cli("stamp", "start")
        self.assertEqual(started.returncode, 0, started.stderr)
        token = "0123456789abcdef0123456789abcdef"

        finished = self.run_cli("stamp", "finish", started.stdout.strip(), "--token", token)
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertEqual(verify.stamp_result(finished.stdout, full=True), {"stamped": True, "tree": tree1})
        self.assertEqual(self.run_cli("stamp", "check").returncode, 0)

        aborted = self.run_cli("stamp", "abort", token)
        self.assertEqual(aborted.returncode, 0, aborted.stderr)
        self.assertEqual(self.run_cli("stamp", "check").returncode, 1)

        # Usage errors exit 2.
        bad_token = self.run_cli("stamp", "record", "--token", "not-hex")
        self.assertEqual(bad_token.returncode, 2)
        bad_flag = self.run_cli("stamp", "check", "--token", token)
        self.assertEqual(bad_flag.returncode, 2)

        # Outside a git repo exits 1 cleanly.
        outside = self.tmp / "not-a-repo"
        outside.mkdir()
        out_res = self.run_cli("stamp", "check", cwd=outside)
        self.assertEqual(out_res.returncode, 1)
        self.assertIn("not in a Git repository", out_res.stderr)


if __name__ == "__main__":
    unittest.main()
