#!/usr/bin/env python3
"""A holder's identity does not depend on its client's locale or time zone (fathers#122): current clients write
`started` (epoch seconds) beside `start` and compare it, `start` stays as older clients write it so that they
still match a current client's entry, and an entry an older client wrote in its own locale's words still matches
its live holder."""
from __future__ import annotations
import sys as _path_sys
from pathlib import Path as _Path
_path_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "src"))

import json
import os
import subprocess
import sys
import time
import unittest
from typing import Any

from slotfather import leases as sf_leases
from slotfather import platform as sf_platform
from test_slotfather import SlotCase

LOCALES = ("ru_RU.UTF-8", "de_DE.UTF-8")


def lstart(pid: int, locale: str, zone: str | None = None) -> str:
    env = {"PATH": "/bin:/usr/bin", "LC_ALL": locale, **({"TZ": zone} if zone else {})}
    return subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                          env=env).stdout.strip()


class SameStartTests(unittest.TestCase):
    observed = "Fri Nov 13 15:50:34 2026"

    def test_older_clients_locale_words_match_the_same_moment(self) -> None:
        for written in ("Fri Nov 13 15:50:34 2026", "Fri Nov 13 15:50:34 2026   ",
                        "пятница, 13 ноября 2026 г. 15:50:34", "Fr. 13 Nov. 15:50:34 2026", "Fri 13 Nov 15:50:34 2026",
                        "ven. 13 nov. 15:50:34 2026", "mar. 13 nov. 15:50:34 2026", "金 11/13 15:50:34 2026",
                        "13/11/2026 15:50:34"):
            with self.subTest(written):
                self.assertTrue(sf_platform.same_start(written, self.observed))

    def test_another_moment_never_matches(self) -> None:
        for written in ("Fri Nov 13 15:50:35 2026", "Thu Nov 12 15:50:34 2026", "Fri Nov 13 15:50:34 2025",
                        "Fri Oct 13 15:50:34 2026", "Fri 13 Oct 15:50:34 2026", "пятница, 13 ноября 2026 г. 15:50:35",
                        "четверг, 12 ноября 2026 г. 15:50:34", "Fr. 13 Nov. 15:50:34 2025", "junk", "15:50:34",
                        "Fri Nov 13 15:50:34 2026x", "Fri Nov 13 15:50:34 20266",
                        "金 10/13 15:50:34 2026", "13/10/2026 15:50:34"):
            with self.subTest(written):
                self.assertFalse(sf_platform.same_start(written, self.observed))

    def test_the_epoch_gives_the_moment_in_local_time(self) -> None:
        epoch = 1794585399
        local = time.strftime("%a %b %d %H:%M:%S %Y", time.localtime(epoch))
        self.assertTrue(sf_platform.same_start(local, "", epoch))
        self.assertFalse(sf_platform.same_start(local, "", epoch + 1))


class SameHolderTests(unittest.TestCase):
    def test_started_decides_where_both_sides_know_it(self) -> None:
        me = os.getpid()
        epoch = sf_platform.start_epoch(me)
        if epoch is None:
            self.skipTest("this system gives no start in epoch seconds")
        shown = sf_platform.processes()[me][1]
        self.assertTrue(sf_platform.same_holder("Thu Jan  1 00:00:00 1970", epoch, me, shown),
                        "an entry from a client in another locale or time zone")
        self.assertFalse(sf_platform.same_holder(shown, epoch + 5, me, shown), "a recycled pid with equal words")
        self.assertTrue(sf_platform.alive({"pid": me, "start": "?", "started": epoch}, {me: (1, shown)}))

    def test_without_started_the_words_decide(self) -> None:
        me = os.getpid()
        shown = sf_platform.processes()[me][1]
        self.assertTrue(sf_platform.same_holder(shown, None, me, shown))
        self.assertTrue(sf_platform.same_holder("", None, me, shown))
        self.assertFalse(sf_platform.same_holder("Thu Jan  1 00:00:00 1970", None, me, shown))
        self.assertFalse(sf_platform.same_holder(None, None, me, shown))

    def test_same_process_takes_an_older_clients_locale_words(self) -> None:
        """ask-yield and reprioritize --start compare what `status --json` gave: an entry's own `start`."""
        if sys.platform != "darwin":
            self.skipTest("macOS ps writes lstart in the caller's locale")
        me = os.getpid()
        written = lstart(me, LOCALES[0])
        table = {me: (os.getppid(), lstart(me, "C"))}
        if written == table[me][1]:
            self.skipTest(f"{LOCALES[0]} is not installed: its lstart is the C locale's")
        self.assertTrue(sf_leases.same_process(table, me, written))
        self.assertFalse(sf_leases.same_process(table, me, ""))
        self.assertFalse(sf_leases.same_process(table, me, "Thu Jan  1 00:00:00 1970"))


@unittest.skipUnless(sys.platform == "darwin", "macOS ps writes lstart in the caller's locale (Linux runs C already)")
class MixedLocaleTests(SlotCase):
    def setUp(self) -> None:
        super().setUp()
        pid, _ = self.holder()
        if lstart(pid, LOCALES[0]) == lstart(pid, "C") or lstart(pid, LOCALES[1]) == lstart(pid, "C"):
            self.skipTest(f"{' and '.join(LOCALES)} are not both installed: their lstart is the C locale's")

    def entry(self, pid: int, start: str, label: str, sequence: int) -> None:
        (self.state / "queue").mkdir(parents=True, exist_ok=True)
        (self.state / "queue" / f"{sequence:010d}-{pid}.json").write_text(json.dumps(
            {"pid": pid, "start": start, "count": 1, "label": label, "cmd": "fake", "since": "-", "state": "held"}))

    def queued(self) -> list[dict[str, Any]]:
        return [json.loads(path.read_text()) for path in sorted((self.state / "queue").glob("*.json"))]

    def test_clients_in_different_locales_and_zones_keep_each_others_leases(self) -> None:
        clients = (("holder", LOCALES[0], "Europe/Moscow"), ("other", LOCALES[1], "Asia/Tokyo"),
                   ("third", "C", "UTC"))
        running: list[subprocess.Popen[str]] = []
        for label, locale, zone in clients:
            self.env.update(LC_ALL=locale, TZ=zone)
            running.append(self.request(label, 1))
            self.started(label)
        for _, locale, zone in clients:
            status = self.tool("status", LC_ALL=locale, TZ=zone)
            for label, _, _ in clients:
                self.assertIn(f"held: 1 slot(s) {label}", status.stdout, (locale, zone, status.stdout))
        self.assertNotIn("reaped", (self.state / "queue.log").read_text())
        for (label, _, _), process in zip(clients, running):
            self.release(label)
            self.finish(process)

    def test_an_older_client_in_the_same_locale_reads_a_current_clients_entry_as_its_own(self) -> None:
        """The comparison an older macOS client makes: `start` equal, as text, to its own ps in its locale."""
        self.env.update(LC_ALL=LOCALES[0])
        holder = self.request("holder", 1)
        self.started("holder")
        entry = self.queued()[0]
        self.assertEqual(lstart(entry["pid"], LOCALES[0]), entry["start"])
        self.assertIsInstance(entry.get("started"), int)
        self.release("holder")
        self.finish(holder)

    def test_an_entry_written_in_an_older_clients_locale_matches_its_live_holder(self) -> None:
        pid, _ = self.holder()
        for sequence, locale in enumerate(("en_US.UTF-8", *LOCALES), start=1):
            self.entry(pid, lstart(pid, locale), f"old-{locale}", sequence)
        self.entry(pid, "четверг, 12 ноября 2026 г. 10:00:00", "recycled", 9)
        for locale in ("C", *LOCALES):
            status = self.tool("status", LC_ALL=locale)
            for old in ("en_US.UTF-8", *LOCALES):
                self.assertIn(f"held: 1 slot(s) old-{old}", status.stdout, (locale, status.stdout))
            self.assertNotIn("recycled", status.stdout, "a pid started at another moment is not the holder")
        log = (self.state / "queue.log").read_text()
        self.assertEqual(1, log.count("reaped "), log)
        self.assertIn("reaped held lease of 1 for recycled", log)


if __name__ == "__main__":
    unittest.main()
