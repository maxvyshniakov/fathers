"""Shared CHANGELOG.md layout and release-version checks across all packages (fathers#219).

Replaces the four per-package `tests/test_changelog.py` copies while keeping each package's `CHANGELOG.md`,
`pyproject.toml` and runtime version constant in lockstep.

Run from the repository root: python3 -m unittest discover -s common/tests
"""
from __future__ import annotations

from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = ("slotfather", "lanefather", "junkfather", "godfather", "tokenfather")
SECTIONS = ("Breaking", "Added", "Changed", "Deprecated", "Removed", "Fixed", "Security")
STRICT_RELEASE_RE = re.compile(r"## (\d+)\.(\d+)\.(\d+) - (\d{4}-\d\d-\d\d)")
BUILD_RELEASE_RE = re.compile(r"## (\d+)\.(\d+)\.(\d+)(?:\+[0-9a-f]+)? - (\d{4}-\d\d-\d\d)")


def check_changelog_layout(case: unittest.TestCase, package: str, text: str, *, allow_build: bool = False) -> str:
    """Verify the Sid-Kit changelog layout for `package` and return the newest base release version (`X.Y.Z`)."""
    lines = text.splitlines()
    case.assertTrue(lines, f"{package}: empty CHANGELOG.md")
    case.assertEqual(f"# {package.capitalize()} Changelog", lines[0], package)
    case.assertEqual(1, sum(line.startswith("# ") for line in lines), package)
    headings = [line for line in lines if line.startswith("## ")]
    case.assertGreaterEqual(len(headings), 2, f"{package}: missing release headings")
    case.assertEqual("## Unreleased", headings[0], package)
    case.assertEqual(1, headings.count("## Unreleased"), package)

    pattern = BUILD_RELEASE_RE if allow_build else STRICT_RELEASE_RE
    releases: list[tuple[tuple[int, int, int], str]] = []
    for heading in headings[1:]:
        found = pattern.fullmatch(heading)
        case.assertIsNotNone(found, f"{package}: {heading}")
        assert found is not None
        version_tuple = (int(found[1]), int(found[2]), int(found[3]))
        releases.append((version_tuple, found[4]))
    case.assertEqual(sorted(releases, reverse=True), releases, f"{package}: newest first, dates too")

    section: list[str] = []
    for line in [*lines, "## end"]:
        if line.startswith("## "):
            case.assertEqual(
                sorted(section, key=SECTIONS.index),
                section,
                f"{package}: section order before {line}",
            )
            section = []
        elif line.startswith("### "):
            name = line[4:]
            case.assertIn(name, SECTIONS, f"{package}: {line}")
            section.append(name)

    for index, line in enumerate(lines):
        if line.startswith("### "):
            case.assertLess(index + 2, len(lines), f"{package}: truncated section {line}")
            case.assertEqual("", lines[index + 1], f"{package}: a blank line after {line}")
            case.assertTrue(lines[index + 2].startswith("- "), f"{package}: {line} has entries")

    newest_triple = releases[0][0]
    return f"{newest_triple[0]}.{newest_triple[1]}.{newest_triple[2]}"


def runtime_version(package_root: Path, package: str) -> tuple[str, str]:
    """Read `(version_string, relative_path)` from the package's runtime version module."""
    src_version = package_root / "src" / package / "version.py"
    if src_version.is_file():
        found = re.search(r'^VERSION = "([^"]+)"', src_version.read_text(encoding="utf-8"), re.M)
        assert found is not None, f"no VERSION in {src_version}"
        return found[1], f"src/{package}/version.py"
    init_py = package_root / package / "__init__.py"
    found = re.search(r'^__version__ = "([^"]+)"', init_py.read_text(encoding="utf-8"), re.M)
    assert found is not None, f"no __version__ in {init_py}"
    return found[1], f"{package}/__init__.py"


class ChangelogLayoutTests(unittest.TestCase):
    def check_package(self, package: str, *, allow_build: bool = False) -> None:
        package_root = ROOT / package
        changelog_text = (package_root / "CHANGELOG.md").read_text(encoding="utf-8")
        newest = check_changelog_layout(self, package, changelog_text, allow_build=allow_build)
        pyproject_text = (package_root / "pyproject.toml").read_text(encoding="utf-8")
        project = re.search(r'^version = "([^"]+)"', pyproject_text, re.M)
        self.assertIsNotNone(project, f"{package}/pyproject.toml")
        assert project is not None
        self.assertEqual(newest, project[1], f"{package}/pyproject.toml")
        code_version, code_path = runtime_version(package_root, package)
        self.assertEqual(newest, code_version, f"{package}/{code_path}")

    def test_slotfather_changelog_layout_and_version(self) -> None:
        self.check_package("slotfather")

    def test_godfather_changelog_layout_and_version(self) -> None:
        self.check_package("godfather")

    def test_junkfather_changelog_layout_and_version(self) -> None:
        self.check_package("junkfather")

    def test_tokenfather_changelog_layout_and_version(self) -> None:
        self.check_package("tokenfather")

    def test_lanefather_changelog_layout_and_version(self) -> None:
        self.check_package("lanefather", allow_build=True)

    def test_layout_validator_rejects_malformed_changelogs(self) -> None:
        valid = (
            "# Slotfather Changelog\n\n"
            "## Unreleased\n\n"
            "### Added\n\n"
            "- Something new.\n\n"
            "## 0.2.0 - 2026-11-12\n\n"
            "### Breaking\n\n"
            "- First section.\n\n"
            "### Fixed\n\n"
            "- Second section.\n\n"
            "## 0.1.0 - 2026-11-10\n\n"
            "### Added\n\n"
            "- Initial.\n"
        )
        self.assertEqual("0.2.0", check_changelog_layout(self, "slotfather", valid))

        bad_cases = (
            ("wrong title", valid.replace("# Slotfather Changelog", "# Wrong Changelog", 1)),
            ("missing unreleased", valid.replace("## Unreleased\n\n### Added\n\n- Something new.\n\n", "", 1)),
            ("release order", valid.replace("## 0.2.0 - 2026-11-12", "## 0.0.9 - 2026-11-12", 1)),
            (
                "section order",
                valid.replace(
                    "### Breaking\n\n- First section.\n\n### Fixed",
                    "### Fixed\n\n- First.\n\n### Breaking",
                    1,
                ),
            ),
            ("unknown section", valid.replace("### Added", "### Mystery", 1)),
            ("missing blank line after section", valid.replace("### Added\n\n- Initial.", "### Added\n- Initial.", 1)),
        )
        for label, broken in bad_cases:
            with self.subTest(label=label), self.assertRaises(AssertionError):
                check_changelog_layout(self, "slotfather", broken)


if __name__ == "__main__":
    unittest.main()
