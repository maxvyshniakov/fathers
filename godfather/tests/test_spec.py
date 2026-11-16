"""SPEC.md says what is built (fathers#144): its module table names every module under src/godfather/ and no other,
and every numbered behaviour item has a status, built with its version or planned with its open ticket."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "godfather"
SPEC = (ROOT / "SPEC.md").read_text(encoding="utf-8")


def section(title: str) -> str:
    """The text of `## title`, up to the next heading of that level."""
    start = SPEC.index(f"\n## {title}\n")
    end = SPEC.find("\n## ", start + 1)
    return SPEC[start:end if end >= 0 else len(SPEC)]


def table(text: str) -> dict[str, str]:
    """A two-column Markdown table's rows: first cell → second cell, the header and rule left out."""
    rows: dict[str, str] = {}
    for line in text.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")] if line.startswith("|") else []
        if len(cells) == 2 and not set(cells[0]) <= set("-") and cells[0] not in ("Module", "Item"):
            rows[cells[0]] = cells[1]
    return rows


class SpecTests(unittest.TestCase):
    def test_the_module_table_is_the_source_tree(self) -> None:
        listed = {name.strip("`") for name in table(section("Decisions").split("### Event schema")[0])}
        built = {".".join(path.relative_to(PACKAGE).with_suffix("").parts) for path in PACKAGE.rglob("*.py")
                 if path.stem not in ("__init__", "__main__")}
        self.assertEqual(sorted(built - listed), [], "modules the SPEC does not list")
        self.assertEqual(sorted(listed - built), [], "modules the SPEC lists that do not exist")

    def test_every_behaviour_item_says_whether_it_is_built(self) -> None:
        items = re.findall(r"^\s*(\d+)\. ", section("Behavior"), re.M)
        self.assertEqual([str(n) for n in range(1, len(items) + 1)], items, "items numbered in order")
        status = table(section("Status"))
        self.assertEqual(items, list(status), "one status row per item, in order")
        for item, said in status.items():
            with self.subTest(item=item):
                self.assertRegex(said, r"^(built (0\.\d+\.\d+|Unreleased|in )|planned \(fathers#\d+\))")
                for planned in re.findall(r"planned[^;]*", said):
                    self.assertRegex(planned, r"\(fathers#\d+\)", "a planned part links its ticket")

    def test_the_status_line_no_longer_says_not_built(self) -> None:
        self.assertNotIn("not built", SPEC.splitlines()[2])


if __name__ == "__main__":
    unittest.main()
