"""Documentation hygiene tests: no private former-repo issue references, glossary/module agreement, and valid anchors."""
from __future__ import annotations

import pathlib
import re
import unittest

from support import ROOT

ISSUE_REF = re.compile(r"(?P<prefix>(?:[A-Za-z0-9_.-]+/)?[A-Za-z0-9_.-]+)?#(?P<num>\d+)\b")
AVOID_CLAUSE = re.compile(r"_Avoid_:\s*([^.|]+)", re.IGNORECASE)
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$")
FENCE = re.compile(r"^\s*```")


def _prose_lines(path: pathlib.Path) -> list[tuple[int, str]]:
    """Return (1-based line number, line) outside fenced code blocks."""
    lines: list[tuple[int, str]] = []
    in_fence = False
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            lines.append((lineno, line))
    return lines


def _slugify(heading: str) -> str:
    slug = heading.strip().lower()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"\s+", "-", slug)
    return slug


class DocsHygieneTests(unittest.TestCase):
    def test_no_private_issue_references_in_docs(self):
        doc_files = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]
        offenders: list[str] = []
        allowed_prefixes = {"fathers", "maxvyshniakov/fathers"}
        for path in doc_files:
            rel = path.relative_to(ROOT).as_posix()
            for lineno, line in _prose_lines(path):
                for match in ISSUE_REF.finditer(line):
                    prefix = match.group("prefix") or ""
                    if prefix.lower() not in allowed_prefixes:
                        offenders.append(f"{rel}:{lineno}: {match.group(0)}")
        self.assertEqual(offenders, [], "Found non-fathers #NNN references:\n" + "\n".join(offenders))

    def test_glossary_avoid_terms_do_not_match_module_names(self):
        glossary = (ROOT / "GLOSSARY.md").read_text(encoding="utf-8")
        avoided: set[str] = set()
        for match in AVOID_CLAUSE.finditer(glossary):
            for part in re.split(r"[,;]|\bor\b", match.group(1)):
                term = part.strip().strip("`").lower()
                if term:
                    avoided.add(term)
        modules = {
            path.stem.lower()
            for path in (ROOT / "lanefather").glob("*.py")
            if path.stem not in ("__init__", "__main__")
        }
        overlap = sorted(avoided & modules)
        self.assertEqual(overlap, [], f"Glossary _Avoid_ terms match module names in lanefather/: {overlap}")

    def test_api_status_anchor_resolves_in_reference(self):
        api_text = (ROOT / "docs" / "api.md").read_text(encoding="utf-8")
        match = re.search(r"`lanefather status --json`[^\n]*\[reference\]\(reference\.md#([a-z0-9-]+)\)", api_text)
        self.assertIsNotNone(match, "docs/api.md must link `lanefather status --json` to a reference.md anchor")
        assert match is not None
        anchor = match.group(1)
        ref_headings = {
            _slugify(heading.group(1))
            for _, line in _prose_lines(ROOT / "docs" / "reference.md")
            if (heading := HEADING.match(line))
        }
        self.assertIn(anchor, ref_headings, f"Anchor #{anchor} not found in docs/reference.md")


if __name__ == "__main__":
    unittest.main()
