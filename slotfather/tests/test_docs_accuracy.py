"""Documentation accuracy guards (fathers#127).

Verifies that:
- Every non-dunder module in `src/slotfather/` is documented in `docs/architecture.md`'s module table (and no
  removed module remains listed).
- Executable counts in `docs/architecture.md` and `docs/installation.md` match `install.py`'s `COMMANDS`.
- Removed historical review/experiment files stay deleted and unlinked.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import re
from typing import Any
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "slotfather"
DOCS = ROOT / "docs"
REMOVED_DOCS = ("review-issue1.md", "review-issue2.md", "scheduler-experiments.md")


def architecture_modules() -> set[str]:
    """Extract backticked module names from the `## Modules` table in docs/architecture.md."""
    text = (DOCS / "architecture.md").read_text(encoding="utf-8")
    in_section = False
    found: set[str] = set()
    for line in text.splitlines():
        if line.startswith("## "):
            if in_section:
                break
            in_section = line.strip() == "## Modules"
            continue
        if not in_section or not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if not cells or cells[0] in ("Module", "---"):
            continue
        found.update(re.findall(r"`([a-zA-Z0-9_]+)`", cells[0]))
    return found


class DocsAccuracyTests(unittest.TestCase):
    def test_every_source_module_is_listed_in_architecture_table(self) -> None:
        actual = {path.stem for path in SRC.glob("*.py") if not path.name.startswith("__")}
        documented = architecture_modules()
        missing = sorted(actual - documented)
        stale = sorted(documented - actual)
        self.assertEqual([], missing, f"modules missing from docs/architecture.md: {missing}")
        self.assertEqual([], stale, f"modules in docs/architecture.md that do not exist: {stale}")

    def test_executable_counts_match_installer_commands(self) -> None:
        spec = importlib.util.spec_from_file_location("slotfather_installer_docs_check", ROOT / "install.py")
        assert spec and spec.loader
        installer: Any = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        self.assertEqual(3, len(installer.COMMANDS))
        for doc_name in ("architecture.md", "installation.md"):
            text = (DOCS / doc_name).read_text(encoding="utf-8").lower()
            self.assertNotIn("four repository executables", text, doc_name)
            self.assertNotIn("the four commands", text, doc_name)
            self.assertRegex(text, r"\bthree\b.*(?:executables|commands)", doc_name)

    def test_historical_docs_are_removed_and_not_linked(self) -> None:
        for name in REMOVED_DOCS:
            self.assertFalse((DOCS / name).exists(), f"historical file should be removed: docs/{name}")
        targets = [ROOT / "README.md", *sorted(DOCS.rglob("*.md"))]
        for path in targets:
            text = path.read_text(encoding="utf-8")
            for name in REMOVED_DOCS:
                self.assertNotIn(name, text, f"stale reference to {name} in {path.relative_to(ROOT)}")


if __name__ == "__main__":
    unittest.main()
