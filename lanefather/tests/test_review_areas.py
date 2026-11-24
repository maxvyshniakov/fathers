"""Tests for area-split and path-filtered overall reviews (fathers#155):
`lanefather review --overall SHA --since BASE --paths GLOB...` and `--areas FILE`."""
from __future__ import annotations

import json
import textwrap
import unittest
from unittest import mock

from support import NO_FINDINGS, TWO_FINDINGS, LaneCase

from lanefather import review, reviewer


class OverallReviewPathsAndAreasTests(LaneCase):
    def setUp(self) -> None:
        super().setUp()
        self.base = self.tip()
        self.tip_sha = self.commit(
            self.integration,
            {
                "lib/api.txt": "alpha_changed\n",
                "lib/sub/helper.txt": "helper_changed\n",
                "native/core.txt": "native_changed\n",
                "docs/guide.md": "docs_changed\n",
            },
            "multi-area change",
        )

    def test_paths_restricts_diff_and_prompt_to_matching_files(self) -> None:
        self.review_text.write_text(TWO_FINDINGS)
        output = self.lane(
            "review",
            "--overall",
            self.tip_sha,
            "--since",
            self.base,
            "--paths",
            "lib/**",
            "--paths",
            "docs/*.md",
        )
        self.assertIn("Paths: lib/**, docs/*.md", output)
        prompt = (self.tmp / "codex.log.prompt").read_text()
        self.assertIn("alpha_changed", prompt)
        self.assertIn("helper_changed", prompt)
        self.assertIn("docs_changed", prompt)
        self.assertNotIn("native_changed", prompt)
        self.assertNotIn("native/core.txt", prompt)

        label = review.paths_review_label(self.tip_sha, ["lib/**", "docs/*.md"])
        receipt = self.review(label)
        self.assertEqual(receipt["status"], "finished")
        self.assertEqual(receipt["kind"], "overall")
        self.assertEqual(receipt["paths"], ["lib/**", "docs/*.md"])
        self.assertNotIn("area", receipt)
        self.assertTrue(receipt["issue"].startswith("https://github.com/fixture/"))
        # Does not collide with a full overall review receipt (`ship-<sha>`).
        self.assertFalse((self.state / f"{review.ship_review_label(self.tip_sha)}.review.json").exists())

        # Marking works on a `--paths` overall review receipt.
        marked = self.lane(
            "review",
            "--overall",
            self.tip_sha,
            "--paths",
            "lib/**",
            "--paths",
            "docs/*.md",
            "--mark",
            "1",
            "rejected:verified",
        )
        self.assertIn("rejected: verified", marked)
        self.assertEqual(self.review(label)["findings"][0]["mark"]["kind"], "rejected")

    def test_paths_with_no_matching_changes_skips_without_running_reviewer(self) -> None:
        output = self.lane(
            "review",
            "--overall",
            self.tip_sha,
            "--since",
            self.base,
            "--paths",
            "nonexistent/**",
        )
        self.assertIn("skipping review", output)
        self.assertEqual(self.codex_calls(), [])
        self.assertEqual(self.claude_calls(), [])

    def test_areas_toml_reviews_each_non_empty_area_skips_empty_and_files_labelled_tickets(self) -> None:
        areas_file = self.tmp / "areas.toml"
        areas_file.write_text(
            textwrap.dedent("""\
                [areas]
                backend = ["lib/**"]
                native = ["native/**"]
                empty_area = ["unmatched/**"]
            """),
            encoding="utf-8",
        )
        # Capture each reviewer prompt in order by inspecting a small wrapper or checking per-area runs.
        self.review_text.write_text(TWO_FINDINGS)
        output = self.lane(
            "review",
            "--overall",
            self.tip_sha,
            "--since",
            self.base,
            "--areas",
            str(areas_file),
        )
        self.assertIn("Area: backend (paths: lib/**)", output)
        self.assertIn("Area: native (paths: native/**)", output)
        self.assertIn("Area empty_area: no matching changes", output)

        # Exactly two reviewer executions: backend and native (empty_area skipped).
        exec_calls = [c for c in self.codex_calls() if c[2] == "exec"]
        self.assertEqual(len(exec_calls), 2)

        backend_label = review.area_review_label(self.tip_sha, "backend")
        native_label = review.area_review_label(self.tip_sha, "native")
        empty_label = review.area_review_label(self.tip_sha, "empty_area")

        backend_receipt = self.review(backend_label)
        native_receipt = self.review(native_label)
        self.assertFalse((self.state / f"{empty_label}.review.json").exists())

        self.assertEqual(backend_receipt["area"], "backend")
        self.assertEqual(backend_receipt["paths"], ["lib/**"])
        self.assertEqual(backend_receipt["issue_label"], "area:backend")
        self.assertTrue(backend_receipt["issue"].startswith("https://github.com/fixture/"))

        self.assertEqual(native_receipt["area"], "native")
        self.assertEqual(native_receipt["paths"], ["native/**"])
        self.assertEqual(native_receipt["issue_label"], "area:native")
        self.assertTrue(native_receipt["issue"].startswith("https://github.com/fixture/"))
        self.assertNotEqual(backend_receipt["issue"], native_receipt["issue"])

        # Each area run filed a single ticket with its `area:<name>` label.
        gh_log = (self.tmp / "gh.log").read_text()
        self.assertEqual(gh_log.count("issue create "), 2)
        first, second = gh_log.split("issue create ")[1:]
        self.assertIn("--label area:backend", first)
        self.assertIn(f"Overall review of {self.tip_sha[:10]} (backend): 2 findings", first)
        self.assertIn("--label area:native", second)
        self.assertIn(f"Overall review of {self.tip_sha[:10]} (native): 2 findings", second)

        # Rerunning `--areas` does not run the reviewer again (even without `--since`, reusing saved base).
        rerun = self.lane("review", "--overall", self.tip_sha, "--areas", str(areas_file))
        self.assertIn("Area: backend", rerun)
        self.assertEqual(len([c for c in self.codex_calls() if c[2] == "exec"]), 2)

        # Marking an area review finding via `--area NAME --mark ...` works cleanly.
        marked = self.lane(
            "review",
            "--overall",
            self.tip_sha,
            "--area",
            "backend",
            "--mark",
            "1",
            "rejected:verified in area",
        )
        self.assertIn("rejected: verified in area", marked)
        self.assertEqual(self.review(backend_label)["findings"][0]["mark"]["kind"], "rejected")
        # Native area receipt is unaffected.
        self.assertIsNone(self.review(native_label)["findings"][0]["mark"])

        # Changing an area's globs on rerun refuses without `--force-reason`, and succeeds with it.
        changed_areas = self.tmp / "areas_changed.toml"
        changed_areas.write_text('[areas]\nbackend = ["lib/**", "docs/*.md"]\n', encoding="utf-8")
        err = self.lane(
            "review", "--overall", self.tip_sha, "--since", self.base, "--areas", str(changed_areas), ok=False
        )
        self.assertIn("different paths", err)
        self.lane(
            "review",
            "--overall",
            self.tip_sha,
            "--since",
            self.base,
            "--areas",
            str(changed_areas),
            "--force-reason",
            "area globs expanded",
        )
        self.assertEqual(self.review(backend_label)["paths"], ["lib/**", "docs/*.md"])

    def test_areas_json_and_top_level_toml_formats_and_prompt_isolation(self) -> None:
        # JSON format: one area covering native only; verify lib/ is excluded from the prompt.
        areas_json = self.tmp / "areas.json"
        areas_json.write_text(json.dumps({"native-only": ["native/**"]}), encoding="utf-8")
        self.review_text.write_text(NO_FINDINGS)
        self.lane("review", "--overall", self.tip_sha, "--since", self.base, "--areas", str(areas_json))
        prompt = (self.tmp / "codex.log.prompt").read_text()
        self.assertIn("native_changed", prompt)
        self.assertNotIn("alpha_changed", prompt)
        self.assertNotIn("docs_changed", prompt)
        # Clean review files no ticket.
        self.assertFalse((self.tmp / "gh.log").exists())

        # Top-level TOML format (without [areas] wrapper).
        areas_toml = self.tmp / "top.toml"
        areas_toml.write_text('docs_only = ["docs/*.md"]\n', encoding="utf-8")
        self.lane("review", "--overall", self.tip_sha, "--since", self.base, "--areas", str(areas_toml))
        prompt2 = (self.tmp / "codex.log.prompt").read_text()
        self.assertIn("docs_changed", prompt2)
        self.assertNotIn("native_changed", prompt2)
        self.assertEqual(self.review(review.area_review_label(self.tip_sha, "docs_only"))["area"], "docs_only")

    def test_area_ticket_creates_missing_label_and_retries_issue_create(self) -> None:
        fake_gh = self.tmp / "label-creating-gh"
        log_file = self.tmp / "label-gh.log"
        label_created_flag = self.tmp / "label-created"
        fake_gh.write_text(
            "#!/usr/bin/env python3\n"
            "import pathlib, sys\n"
            "args = sys.argv[1:]\n"
            f"log = pathlib.Path({str(log_file)!r})\n"
            f"flag = pathlib.Path({str(label_created_flag)!r})\n"
            "with log.open('a') as h:\n"
            "    h.write(' '.join(args) + '\\n')\n"
            "if args[:2] == ['issue', 'create']:\n"
            "    if not flag.exists():\n"
            "        print(\"could not add label: 'area:backend' not found\", file=sys.stderr)\n"
            "        sys.exit(1)\n"
            "    print('https://github.com/fixture/straylight/issues/777')\n"
            "    sys.exit(0)\n"
            "if args[:2] == ['label', 'create']:\n"
            "    flag.write_text('ok')\n"
            "    sys.exit(0)\n"
            "sys.exit(0)\n",
            encoding="utf-8",
        )
        fake_gh.chmod(0o755)
        self.env["LANEFATHER_GH"] = str(fake_gh)

        areas_file = self.tmp / "one.toml"
        areas_file.write_text('[areas]\nbackend = ["lib/**"]\n', encoding="utf-8")
        self.review_text.write_text(TWO_FINDINGS)
        self.lane("review", "--overall", self.tip_sha, "--since", self.base, "--areas", str(areas_file))

        receipt = self.review(review.area_review_label(self.tip_sha, "backend"))
        self.assertEqual(receipt["issue"], "https://github.com/fixture/straylight/issues/777")
        self.assertEqual(receipt["issue_label"], "area:backend")
        logged = log_file.read_text()
        self.assertIn("label create area:backend", logged)
        self.assertEqual(logged.count("issue create"), 2)

    def test_reviewer_flag_works_with_paths_and_areas(self) -> None:
        self.review_text.write_text(NO_FINDINGS)
        # `--reviewer opus` with `--paths`
        out_opus = self.lane(
            "review",
            "--overall",
            self.tip_sha,
            "--since",
            self.base,
            "--paths",
            "docs/*.md",
            "--reviewer",
            "opus",
        )
        self.assertIn("claude-opus-5-5", out_opus)
        self.assertEqual(len(self.claude_calls()), 1)
        self.assertEqual(self.codex_calls(), [])

        # `--reviewer both` with `--areas`
        areas_file = self.tmp / "panel.toml"
        areas_file.write_text('[areas]\nnative = ["native/**"]\n', encoding="utf-8")
        out_both = self.lane(
            "review",
            "--overall",
            self.tip_sha,
            "--since",
            self.base,
            "--areas",
            str(areas_file),
            "--reviewer",
            "both",
        )
        self.assertIn("panel sol:", out_both)
        self.assertIn("panel opus:", out_both)
        receipt = self.review(review.area_review_label(self.tip_sha, "native"))
        self.assertEqual(receipt["area"], "native")
        self.assertEqual({e["reviewer"] for e in receipt["panel"]}, {"sol", "opus"})

    def test_cross_area_move_shows_in_both_source_and_destination_areas(self) -> None:
        base = self.tip_sha
        # Move lib/api.txt -> native/moved.txt (pure git rename).
        self.git(self.integration, "mv", "lib/api.txt", "native/moved.txt")
        self.git(self.integration, "commit", "-m", "move file across areas")
        moved_sha = self.tip()

        areas_file = self.tmp / "move_areas.toml"
        areas_file.write_text('[areas]\nbackend = ["lib/**"]\nnative = ["native/**"]\n', encoding="utf-8")
        self.review_text.write_text(NO_FINDINGS)
        output = self.lane(
            "review", "--overall", moved_sha, "--since", base, "--areas", str(areas_file)
        )
        self.assertIn("Area: backend", output)
        self.assertIn("Area: native", output)
        self.assertTrue((self.state / f"{review.area_review_label(moved_sha, 'backend')}.review.json").exists())
        self.assertTrue((self.state / f"{review.area_review_label(moved_sha, 'native')}.review.json").exists())

    def test_inline_diff_batches_large_path_lists_in_order(self) -> None:
        req = reviewer.Request(
            standards="rules",
            subject="subject",
            base=self.base,
            tip=self.tip_sha,
            paths=("docs/guide.md", "lib/api.txt", "lib/sub/helper.txt", "native/core.txt"),
        )
        with mock.patch.object(reviewer, "DIFF_PATH_BATCH", 2):
            diff = reviewer._inline_diff(str(self.integration), req)  # pyright: ignore[reportPrivateUsage]
        self.assertIn("docs_changed", diff)
        self.assertIn("alpha_changed", diff)
        self.assertIn("helper_changed", diff)
        self.assertIn("native_changed", diff)
        self.assertLess(diff.index("docs_changed"), diff.index("alpha_changed"))
        self.assertLess(diff.index("alpha_changed"), diff.index("helper_changed"))
        self.assertLess(diff.index("helper_changed"), diff.index("native_changed"))

    def test_area_ticket_failure_hint_points_to_area_flag_and_retries(self) -> None:
        areas_file = self.tmp / "retry_area.toml"
        areas_file.write_text('[areas]\nbackend = ["lib/**"]\n', encoding="utf-8")
        self.review_text.write_text(TWO_FINDINGS)
        self.env["FAKE_GH_FAIL_CREATE"] = "1"
        failed_out = self.lane(
            "review", "--overall", self.tip_sha, "--since", self.base, "--areas", str(areas_file), ok=False
        )
        self.assertIn(f"lanefather review --overall {self.tip_sha[:10]} --area backend files it again", failed_out)
        del self.env["FAKE_GH_FAIL_CREATE"]
        retried_out = self.lane("review", "--overall", self.tip_sha, "--area", "backend")
        self.assertIn("Ticket: https://github.com/fixture/", retried_out)
        self.assertEqual(len([c for c in self.codex_calls() if c[2] == "exec"]), 1)

    def test_invalid_flag_combinations_and_malformed_areas_files_refuse_cleanly(self) -> None:
        valid_areas = self.tmp / "valid.toml"
        valid_areas.write_text('[areas]\nbackend = ["lib/**"]\n', encoding="utf-8")

        cases: list[tuple[tuple[str, ...], str]] = [
            (("review", "a", "--paths", "lib/**"), "--paths goes with --overall"),
            (("review", "a", "--areas", str(valid_areas)), "--areas goes with --overall"),
            (("review", "a", "--area", "backend"), "--area goes with --overall"),
            (
                ("review", "--overall", self.tip_sha, "--since", self.base, "--paths", "lib/**", "--areas", str(valid_areas)),
                "--paths and --areas do not combine",
            ),
            (
                ("review", "--overall", self.tip_sha, "--area", "backend", "--paths", "lib/**"),
                "--area does not combine with --paths, --areas or --lanes",
            ),
            (
                ("review", "--overall", self.tip_sha, "--area", "backend", "--areas", str(valid_areas)),
                "--area does not combine with --paths, --areas or --lanes",
            ),
            (
                ("review", "--overall", self.tip_sha, "--area", "backend", "--lanes", "a"),
                "--area does not combine with --paths, --areas or --lanes",
            ),
            (
                ("review", "--overall", self.tip_sha, "--area", "a" * 46),
                "invalid area name",
            ),
            (
                ("review", "--overall", self.tip_sha, "--since", self.base, "--paths", "lib/**", "--lanes", "a"),
                "--lanes does not combine with --paths or --areas",
            ),
            (
                ("review", "--overall", self.tip_sha, "--since", self.base, "--areas", str(valid_areas), "--lanes", "a"),
                "--lanes does not combine with --paths or --areas",
            ),
            (
                ("review", "--overall", self.tip_sha, "--areas", str(valid_areas), "--mark", "1", "rejected:no"),
                "--areas does not combine with --mark",
            ),
            (
                (
                    "review",
                    "--overall",
                    self.tip_sha,
                    "--since",
                    self.base,
                    "--areas",
                    str(valid_areas),
                    "--record",
                    str(valid_areas),
                    "--reviewer",
                    "opus",
                ),
                "--areas does not combine with --record",
            ),
            (
                ("review", "--overall", self.tip_sha, "--since", self.base, "--areas", str(self.tmp / "missing.toml")),
                "file does not exist",
            ),
        ]
        for argv, expected in cases:
            with self.subTest(argv=argv):
                err = self.lane(*argv, ok=False)
                self.assertIn(expected, err)

        bad_files: list[tuple[str, str, str]] = [
            ("empty.toml", "", "areas file is empty"),
            ("no_areas.toml", "[areas]\n", "defines no areas"),
            ("bad_name.toml", '[areas]\n"../escape" = ["lib/**"]\n', "invalid area name"),
            ("too_long.toml", f'[areas]\n{"a" * 46} = ["lib/**"]\n', "invalid area name"),
            ("empty_globs.toml", "[areas]\nbackend = []\n", "non-empty list of path globs"),
            ("dup_case.toml", '[areas]\nAPI = ["lib/**"]\napi = ["native/**"]\n', "duplicate area name"),
            ("extra_top.toml", 'other = 1\n[areas]\nbackend = ["lib/**"]\n', "unexpected top-level keys"),
            ("bad.json", "[1, 2, 3]", "must be an object"),
            ("broken.json", "{not json", "invalid JSON"),
        ]
        for filename, content, expected in bad_files:
            with self.subTest(filename=filename):
                path = self.tmp / filename
                path.write_text(content, encoding="utf-8")
                err = self.lane(
                    "review", "--overall", self.tip_sha, "--since", self.base, "--areas", str(path), ok=False
                )
                self.assertIn(expected, err)


if __name__ == "__main__":
    unittest.main()
