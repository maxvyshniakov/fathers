#!/usr/bin/env python3
"""A holder's identity does not depend on its client's locale (fathers#122): every client runs ps in the C locale,
and an entry an older client wrote in its own locale's words still matches its live holder."""
from __future__ import annotations
import sys as _path_sys
from pathlib import Path as _Path
_path_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "src"))

import json
import subprocess
import sys
import unittest

from slotfather import platform as sf_platform
from test_slotfather import SlotCase

LOCALES = ("ru_RU.UTF-8", "de_DE.UTF-8")


def lstart(pid: int, locale: str) -> str:
    return subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                          env={"PATH": "/bin:/usr/bin", "LC_ALL": locale}).stdout.strip()


class SameStartTests(unittest.TestCase):
    observed = "Fri Nov 13 15:50:34 2026"

    def test_ps_runs_in_the_c_locale_everywhere(self) -> None:
        self.assertEqual("C", sf_platform.ps_environment().get("LC_ALL"))

    def test_older_clients_locale_words_match_the_same_moment(self) -> None:
        for written in ("Fri Nov 13 15:50:34 2026", "Fri Nov 13 15:50:34 2026   ", "",
                        "пятница, 13 ноября 2026 г. 15:50:34", "Fr. 13 Nov. 15:50:34 2026", "Fri 13 Nov 15:50:34 2026",
                        "ven. 13 nov. 15:50:34 2026", "金 11/13 15:50:34 2026"):
            with self.subTest(written):
                self.assertTrue(sf_platform.same_start(written, self.observed))

    def test_another_moment_never_matches(self) -> None:
        for written in ("Fri Nov 13 15:50:35 2026", "Thu Nov 12 15:50:34 2026", "Fri Nov 13 15:50:34 2025",
                        "Fri Oct 13 15:50:34 2026", "пятница, 13 ноября 2026 г. 15:50:35",
                        "четверг, 12 ноября 2026 г. 15:50:34", "Fr. 13 Nov. 15:50:34 2025", "junk", "15:50:34",
                        "Fri Nov 13 15:50:34 2026x", "Fri Nov 13 15:50:34 20266"):
            with self.subTest(written):
                self.assertFalse(sf_platform.same_start(written, self.observed))

    def test_alive_reads_an_old_entry_by_its_moment(self) -> None:
        table = {42: (1, self.observed)}
        self.assertTrue(sf_platform.alive({"pid": 42, "start": "Fr. 13 Nov. 15:50:34 2026"}, table))
        self.assertFalse(sf_platform.alive({"pid": 42, "start": "Fr. 13 Nov. 15:50:33 2026"}, table))
        self.assertFalse(sf_platform.alive({"pid": 43, "start": self.observed}, table))


@unittest.skipUnless(sys.platform == "darwin", "macOS ps writes lstart in the caller's locale (Linux runs C already)")
class MixedLocaleTests(SlotCase):
    def entry(self, pid: int, start: str, label: str, sequence: int) -> None:
        (self.state / "queue").mkdir(parents=True, exist_ok=True)
        (self.state / "queue" / f"{sequence:010d}-{pid}.json").write_text(json.dumps(
            {"pid": pid, "start": start, "count": 1, "label": label, "cmd": "fake", "since": "-", "state": "held"}))

    def queued(self) -> list[str]:
        return sorted(path.name for path in (self.state / "queue").glob("*.json"))

    def test_two_clients_in_different_locales_keep_each_others_leases(self) -> None:
        first, second = LOCALES
        self.env["LC_ALL"] = first
        holder = self.request("holder", 1)
        self.started("holder")
        before = self.queued()
        for locale in (second, "C", first):
            status = self.tool("status", LC_ALL=locale)
            self.assertIn("held: 1 slot(s) holder", status.stdout, (locale, status.stdout, status.stderr))
        self.env["LC_ALL"] = second
        other = self.request("other", 1)
        self.started("other")
        self.assertIn("held: 1 slot(s) other", self.tool("status", LC_ALL=first).stdout)
        self.assertIn("held: 1 slot(s) holder", self.tool("status", LC_ALL=second).stdout)
        self.assertTrue(set(before) <= set(self.queued()))
        self.assertNotIn("reaped", (self.state / "queue.log").read_text())
        for label, process in (("holder", holder), ("other", other)):
            self.release(label)
            self.finish(process)

    def test_an_entry_written_in_an_older_clients_locale_matches_its_live_holder(self) -> None:
        pid, _ = self.holder()
        for sequence, locale in enumerate(("en_US.UTF-8", *LOCALES), start=1):
            start = lstart(pid, locale)
            self.assertTrue(start)
            self.entry(pid, start, f"old-{locale}", sequence)
        self.entry(pid, "четверг, 12 ноября 2026 г. 10:00:00", "recycled", 9)
        status = self.tool("status")
        for locale in ("en_US.UTF-8", *LOCALES):
            self.assertIn(f"held: 1 slot(s) old-{locale}", status.stdout, status.stdout)
        self.assertNotIn("recycled", status.stdout, "a pid started at another moment is not the holder")
        self.assertEqual(3, len(self.queued()))
        log = (self.state / "queue.log").read_text()
        self.assertIn("reaped held lease of 1 for recycled", log)
        self.assertEqual(1, log.count("reaped "), log)
        entry = json.loads(next((self.state / "queue").glob("*.json")).read_text())
        self.assertEqual(pid, entry["pid"])


if __name__ == "__main__":
    unittest.main()
