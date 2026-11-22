"""Contract tests for `<tool> changelog --json` across Slotfather, Lanefather and Junkfather (fathers#119).

One set of changelog fixtures in `tests/fixtures/changelog/` is run against all three tools' parsers (and their CLIs in
temporary layouts), asserting that every tool produces the identical reply shape for `godfather/docs/contracts/changelog.md`
and that `godfather.news.from_reply()` reads all three without per-tool handling.
"""
from __future__ import annotations

import helpers  # noqa: F401  # pyright: ignore[reportUnusedImport] - src/ on sys.path

import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable, cast

from godfather import news
from helpers import ROOT

REPOSITORY = ROOT.parent
FIXTURES = ROOT / "tests" / "fixtures" / "changelog"
TOOLS = ("slotfather", "lanefather", "junkfather")


def _load_parsers() -> dict[str, Callable[[str | None, str | None, str | None], dict[str, Any]]]:
    """Import each sibling package's changelog report entrypoint directly on any supported Python (3.11+)."""
    for extra in (REPOSITORY / "slotfather" / "src", REPOSITORY / "lanefather", REPOSITORY / "junkfather",
                  REPOSITORY / "common"):
        path_str = str(extra)
        if path_str not in sys.path:
            sys.path.insert(0, path_str)

    jf_changelog: Any = importlib.import_module("junkfather.changelog")
    lf_changelog: Any = importlib.import_module("lanefather.changelog")
    sf_changelog: Any = importlib.import_module("slotfather.changelog")

    return {
        "slotfather": lambda since, path, commit: cast(dict[str, Any],
                                                       sf_changelog.changelog(since=since, path=path, commit=commit)),
        "lanefather": lambda since, path, commit: cast(dict[str, Any],
                                                       lf_changelog.report(since=since, changelog_file=path,
                                                                           commit=commit)),
        "junkfather": lambda since, path, commit: cast(dict[str, Any],
                                                       jf_changelog.contract(since=since, path=path, commit=commit)),
    }


class ChangelogContractTests(unittest.TestCase):
    expected: dict[str, Any]
    parsers: dict[str, Callable[[str | None, str | None, str | None], dict[str, Any]]]

    @classmethod
    def setUpClass(cls) -> None:
        cls.expected = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))
        cls.parsers = _load_parsers()

    def normalized(self, tool: str, reply: dict[str, Any]) -> dict[str, Any]:
        self.assertEqual({"v", "tool", "version", "source", "entries"}, set(reply), tool)
        self.assertEqual(1, reply["v"], tool)
        self.assertEqual(tool, reply["tool"], tool)
        self.assertIsInstance(reply["version"], str, tool)
        self.assertTrue(reply["version"], tool)
        return {"source": reply["source"], "entries": reply["entries"]}

    def test_full_fixture_gives_the_same_reply_from_all_three_tools_installed_and_checkout(self) -> None:
        fixture = str((FIXTURES / "full.md").resolve())
        for commit in ("81abd86c0ffe", None):
            wanted_entries = [
                {**entry, "source": commit} if entry.get("unreleased") else dict(entry)
                for entry in self.expected["full_installed"]
            ]
            replies: dict[str, dict[str, Any]] = {}
            for tool, run_parser in self.parsers.items():
                with self.subTest(tool=tool, commit=commit):
                    reply = run_parser(None, fixture, commit)
                    replies[tool] = reply
                    self.assertEqual({"source": fixture, "entries": wanted_entries}, self.normalized(tool, reply))
                    parsed = news.from_reply(tool, reply)
                    self.assertIsNotNone(parsed)
                    assert parsed is not None
                    self.assertEqual(len(wanted_entries), len(parsed[1]))
            # Godfather's own CHANGELOG.md parser follows the exact same entry selection and continuation rules.
            _, own_items = news.own((FIXTURES / "full.md").read_text(encoding="utf-8"), stale=False,
                                    commit=commit or "")
            for tool, reply in replies.items():
                parsed = news.from_reply(tool, reply)
                assert parsed is not None
                self.assertEqual(
                    [(item.version, item.date, item.section, item.text, item.commit) for item in own_items],
                    [(item.version, item.date, item.section, item.text, item.commit) for item in parsed[1]],
                    tool,
                )

    def test_since_filters_identically_across_all_three_tools_for_numbers_and_build_headings(self) -> None:
        fixture = str((FIXTURES / "full.md").resolve())
        cases = (
            ("0.2.0", self.expected["since_0_2_0"]),
            ("0.1.9", self.expected["since_0_1_9"]),
            ("0.1.0", self.expected["since_0_1_0"]),
            ("0.1.0+7eca44a", self.expected["since_0_1_0_build"]),
            ("99.0.0", [None, None, None]),
        )
        for since, expected_versions in cases:
            for tool, run_parser in self.parsers.items():
                with self.subTest(tool=tool, since=since):
                    reply = run_parser(since, fixture, "81abd86c0ffe")
                    self.assertEqual(expected_versions, [e["version"] for e in reply["entries"]])

    def test_released_only_empty_missing_and_invalid_since_behave_identically(self) -> None:
        released_fixture = str((FIXTURES / "released-only.md").resolve())
        empty_fixture = str((FIXTURES / "empty.md").resolve())
        missing_fixture = str(FIXTURES / "does-not-exist.md")
        for tool, run_parser in self.parsers.items():
            with self.subTest(tool=tool):
                released = run_parser(None, released_fixture, "81abd86c0ffe")
                self.assertEqual({"source": released_fixture, "entries": self.expected["released_only"]},
                                 self.normalized(tool, released))
                empty = run_parser(None, empty_fixture, "81abd86c0ffe")
                self.assertEqual({"source": empty_fixture, "entries": []}, self.normalized(tool, empty))
                missing = run_parser(None, missing_fixture, "81abd86c0ffe")
                self.assertEqual({"source": None, "entries": []}, self.normalized(tool, missing))
                for bad in ("latest", "x", "0.1", ""):
                    with self.assertRaisesRegex(Exception, "--since", msg=f"{tool} accepted invalid --since {bad!r}"):
                        run_parser(bad, released_fixture, None)

    def test_cli_commands_emit_identical_entries_for_a_staged_release(self) -> None:
        with tempfile.TemporaryDirectory(prefix="godfather-changelog-cli-") as raw_tmp:
            tmp = Path(raw_tmp)
            changelog_bytes = (FIXTURES / "full.md").read_bytes()
            commit = "81abd86c0ffe"
            env = {k: v for k, v in os.environ.items()
                   if not k.startswith(("SLOTFATHER_", "JUNKFATHER_", "LANEFATHER_"))}
            env.update(SLOTFATHER_DIR=str(tmp / "sf-state"), SLOTFATHER_LEGACY_DIRS="",
                       JUNKFATHER_STATE=str(tmp / "jf-state"), LANEFATHER_STATE=str(tmp / "lf-state"))

            # Stage minimal release layouts for each tool with full.md and commit metadata.
            sf_rel = tmp / "sf-release"
            shutil.copytree(REPOSITORY / "slotfather" / "src" / "slotfather", sf_rel / "slotfather",
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            if (REPOSITORY / "common" / "fathers_common").is_dir():
                shutil.copytree(REPOSITORY / "common" / "fathers_common", sf_rel / "fathers_common",
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            (sf_rel / "slotfather" / "CHANGELOG.md").write_bytes(changelog_bytes)
            (sf_rel / "slotfather" / "version.json").write_text(json.dumps({"v": 1, "commit": commit}), encoding="utf-8")

            jf_rel = tmp / "jf-release"
            shutil.copytree(REPOSITORY / "junkfather" / "junkfather", jf_rel / "junkfather",
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            shutil.copytree(REPOSITORY / "common" / "fathers_common", jf_rel / "fathers_common",
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            (jf_rel / "CHANGELOG.md").write_bytes(changelog_bytes)
            (jf_rel / "junkfather" / "COMMIT").write_text(commit + "\n", encoding="utf-8")

            invocations: list[tuple[str, Path, list[str]]] = [
                ("slotfather", sf_rel / "slotfather" / "CHANGELOG.md",
                 [sys.executable, "-c", f"import sys; sys.path.insert(0, {str(sf_rel)!r}); "
                                        "from slotfather.cli import main; sys.exit(main(sys.argv[1:]))"]),
                ("junkfather", jf_rel / "CHANGELOG.md",
                 [sys.executable, "-c", f"import sys; sys.path.insert(0, {str(jf_rel)!r}); "
                                        "from junkfather.cli import main; sys.exit(main(sys.argv[1:]))"]),
            ]
            lf_rel = tmp / "lf-release"
            shutil.copytree(REPOSITORY / "lanefather" / "lanefather", lf_rel / "lanefather",
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            shutil.copytree(REPOSITORY / "common" / "fathers_common", lf_rel / "fathers_common",
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            (lf_rel / "lanefather" / "CHANGELOG.md").write_bytes(changelog_bytes)
            (lf_rel / "lanefather" / "COMMIT").write_text(commit + "\n", encoding="utf-8")
            invocations.append((
                "lanefather", lf_rel / "lanefather" / "CHANGELOG.md",
                [sys.executable, "-c", f"import sys; sys.path.insert(0, {str(lf_rel)!r}); "
                                       "from lanefather.cli import main; sys.exit(main(sys.argv[1:]))"],
            ))

            for tool, expected_source, prefix in invocations:
                with self.subTest(tool=tool):
                    ok = subprocess.run([*prefix, "changelog", "--json", "--since", "0.1.0"],
                                        capture_output=True, text=True, env=env, timeout=30)
                    self.assertEqual(0, ok.returncode, ok.stderr)
                    reply = json.loads(ok.stdout)
                    self.assertEqual(str(expected_source.resolve()), reply["source"])
                    self.assertEqual(self.expected["since_0_1_0"], [e["version"] for e in reply["entries"]])
                    bad = subprocess.run([*prefix, "changelog", "--json", "--since", "not-a-version"],
                                         capture_output=True, text=True, env=env, timeout=30)
                    self.assertEqual(2, bad.returncode, bad.stderr)


if __name__ == "__main__":
    unittest.main()
