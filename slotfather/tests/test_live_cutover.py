"""The live request-format cutover (#8): the installer converts live old-format queue entries in place, and the
client installed before it keeps working on the converted queue, beside the new code, until it exits.

`tests/fixtures/upgrade/slotfather-fd422b00.tar.gz` is the release that was installed when the cutover ran: an
08ede50-era client that writes top-level priority and ticket and mirrors them into metadata. Every comparison
runs that real code and the checkout's code in their own processes on the same queue files.
"""
from __future__ import annotations

import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests/fixtures/upgrade/slotfather-fd422b00.tar.gz"
FIXTURE_SHA256 = "d7e5945497a18eafe2ea11ae59169c595777eb5fc5d86203e543a33a17ef750c"  # of the uncompressed tar
WORK = 'echo "start $1" >> "$LOG"; while [ ! -e "$RELEASE/$1" ]; do sleep 0.05; done; echo "end $1" >> "$LOG"'
MOMENT = 1762821000.0  # 2026-11-11T00:30:00Z, a little after the entries below were queued

# Runs inside a client's own interpreter: argv = package root, queue directory, action. Prints JSON.
DRIVER = r'''
import json, pathlib, sys
root = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
from slotfather import config, leases, platform, query, schedule, state
assert pathlib.Path(config.__file__).resolve().parent.parent == root, config.__file__
directory, action = pathlib.Path(sys.argv[2]), sys.argv[3]
if action == "look":
    with state.locked(directory):
        live = leases.reap(directory, platform.processes())
    held = [e for _, e in live if e["state"] == "held"]
    waiters = [e for _, e in live if e["state"] != "held"]
    settings = schedule.order_settings(directory)
    slots = config.total()
    ranks = {e["label"]: schedule.rank_time(e, settings) for e in waiters}
    found = schedule.plan(schedule.fit(waiters, slots), held, slots, float(sys.argv[4]),
                          estimate=schedule.estimator(directory), other=0, settings=settings)
    status = query.status_json(directory)
    out = {"read": {e["label"]: [config.priority_of(e), config.ticket_of(e)] for _, e in live},
           "ranks": ranks, "grants": [e["label"] for e in found.grants], "reason": found.reason,
           "status": sorted([x["label"], x["state"], x["priority"], x["ticket"], x["count"]]
                            for x in status["entries"]),
           "lines": [line.split(" pid=")[0] for line in query.status_lines(directory)]}
elif action == "grant":
    with state.locked(directory):
        leases.grant(directory, platform.processes())
    out = {e["label"]: e["state"] for _, e in state.entries(directory)}
else:
    raise SystemExit("unknown action " + action)
print(json.dumps(out, sort_keys=True))
'''


def load_installer() -> Any:
    spec = importlib.util.spec_from_file_location("slotfather_install", ROOT / "install.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def extract_old_client(target: Path) -> Path:
    """The installed release's package under `target`; refuses a fixture whose bytes changed."""
    raw = gzip.decompress(FIXTURE.read_bytes())
    if hashlib.sha256(raw).hexdigest() != FIXTURE_SHA256:
        raise AssertionError("the old-client fixture does not match its recorded digest")
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        for member in archive.getmembers():
            if not member.isfile() or member.name.startswith("/") or ".." in Path(member.name).parts:
                raise AssertionError(f"unexpected fixture member {member.name}")
            source = archive.extractfile(member)
            assert source is not None
            path = target / member.name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(source.read())
    return target


class CutoverCase(unittest.TestCase):
    tmp: Path

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="slotfather-cutover-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.old_root = extract_old_client(self.tmp / "old-client")
        self.new_root = ROOT / "src"
        self.memory = self.tmp / "memory"
        self.memory.write_text("8000 1\n")
        self.release_dir = self.tmp / "release"
        self.release_dir.mkdir()
        self.log = self.tmp / "log"
        self.log.touch()
        self.processes: list[subprocess.Popen[str]] = []
        self.addCleanup(self.stop_all)

    def environment(self, state: Path) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("SLOTFATHER", "STRAYLIGHT_SLOT", "PYTHON"))}
        env.update(SLOTFATHER_DIR=str(state), SLOTFATHER_LEGACY_DIRS="", SLOTFATHER_POLL="0.05",
                   SLOTFATHER_POLL_MAX="0.2", SLOTFATHER_OVERTAKE_WINDOW="0", SLOTFATHER_CORES="12",
                   SLOTFATHER_MEMORY_GIB="64", SLOTFATHER_MEMORY_FILE=str(self.memory), SLOTFATHER_LOADAVG="0",
                   LOG=str(self.log), RELEASE=str(self.release_dir))
        return env

    def stop_all(self) -> None:
        (self.release_dir / "sleeper").touch()
        for process in self.processes:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            process.communicate(timeout=10)

    def sleeper(self) -> tuple[int, str]:
        """A live process to stand for a client: (pid, its ps start time)."""
        process = subprocess.Popen(["sh", "-c", f'while [ ! -e "{self.release_dir}/sleeper" ]; do sleep 0.05; done'],
                                   start_new_session=True, text=True)
        self.processes.append(process)
        start = subprocess.run(["ps", "-o", "lstart=", "-p", str(process.pid)], capture_output=True,
                               text=True).stdout.strip()
        return process.pid, start

    def drive(self, root: Path, state: Path, action: str) -> dict[str, Any]:
        run = subprocess.run([sys.executable, "-c", DRIVER, str(root), str(state), action, str(MOMENT)],
                             env=self.environment(state), capture_output=True, text=True, timeout=60)
        self.assertEqual(0, run.returncode, f"{root} {action}: {run.stderr}")
        found: dict[str, Any] = json.loads(run.stdout)
        return found

    def started(self, label: str) -> Callable[[], bool]:
        return lambda: f"start {label}" in self.log.read_text()

    def wait_for(self, predicate: Callable[[], object], what: str, timeout: float = 30) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail(f"timed out waiting for {what}; log {self.log.read_text()!r}")


def stamp(offset: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(MOMENT + offset))


class ConvertedEntriesTests(CutoverCase):
    """plan(), grant and status of the old and the new code on the converted queue, the old one and a mix."""

    def write_queue(self, state: Path) -> dict[str, bytes]:
        """The shapes the live queue held on 2026-11-11, as the installed client wrote them."""
        queue = state / "queue"
        queue.mkdir(parents=True)
        shapes: list[dict[str, Any]] = [
            {"count": 1, "label": "held-normal-ticket", "state": "held", "priority": "normal", "ticket": "480",
             "granted": stamp(-1200), "meta": {"lane": "480", "phase": "check", "ticket": "480"}},
            {"count": 1, "label": "held-interactive", "state": "held", "priority": "interactive", "ticket": "337",
             "granted": stamp(-700), "meta": {"lane": "337", "class": "interactive", "ticket": "337"}},
            {"count": 1, "label": "held-no-ticket", "state": "held", "priority": "interactive",
             "granted": stamp(-300), "meta": {"class": "interactive"}},
            {"count": 6, "label": "wait-all", "state": "waiting", "priority": "normal", "ticket": "697",
             "all": True, "until": stamp(2300), "meta": {"check": "tools/test-janitor.py", "ticket": "697"}},
            {"count": 2, "label": "wait-normal-ticket", "state": "waiting", "priority": "normal", "ticket": "62",
             "meta": {"lane": "62", "ticket": "62"}},
            {"count": 2, "label": "wait-normal", "state": "waiting", "priority": "normal", "meta": {"lane": "64"}},
            {"count": 1, "label": "wait-interactive", "state": "waiting", "priority": "interactive",
             "ticket": "516", "meta": {"lane": "voice-05", "class": "interactive", "ticket": "516"}},
            {"count": 1, "label": "wait-batch", "state": "waiting", "priority": "batch", "meta": {"class": "batch"}},
            {"count": 1, "label": "wait-plain", "state": "waiting", "priority": "normal"},
        ]
        written: dict[str, bytes] = {}
        for number, shape in enumerate(shapes, 1):
            pid, start = self.sleeper()
            entry = {"pid": pid, "start": start, "cmd": "fake", "cwd": "/w", "since": stamp(-1800 + 60 * number),
                     **shape}
            path = queue / f"{number:010d}-{pid}.json"
            path.write_text(json.dumps(entry) + "\n")
            written[path.name] = path.read_bytes()
        return written

    def convert(self, state: Path, only: Callable[[str], bool] = lambda name: True) -> int:
        installer = load_installer()
        updates = [u for u in installer.queue_updates(state, installer.live_processes()) if only(u[0].name)]
        installer.migrate_queue(updates, self.tmp / "backup" / state.name)
        return len(updates)

    def copy(self, source: Path, name: str) -> Path:
        target = self.tmp / name
        shutil.copytree(source, target)
        return target

    def assert_only_aliases_changed(self, before: dict[str, bytes], state: Path) -> None:
        for name, original in before.items():
            old_entry, new_entry = json.loads(original), json.loads((state / "queue" / name).read_text())
            meta = {k: v for k, v in old_entry.get("meta", {}).items() if k not in ("class", "priority", "ticket")}
            expected = {**old_entry, "meta": meta} if "meta" in old_entry else old_entry
            self.assertEqual(expected, new_entry, name)

    def test_old_and_new_code_decide_alike_on_converted_mixed_and_old_entries(self) -> None:
        original = self.tmp / "original"
        before = self.write_queue(original)
        converted = self.copy(original, "converted")
        self.assertEqual(7, self.convert(converted))
        self.assert_only_aliases_changed(before, converted)
        mixed = self.copy(original, "mixed")
        self.assertEqual(3, self.convert(mixed, only=lambda name: int(name[:10]) % 2 == 0))

        for root in (self.old_root, self.new_root):
            with self.subTest(code=root.name):
                looks = {name: self.drive(root, state, "look")
                         for name, state in (("original", original), ("converted", converted), ("mixed", mixed))}
                self.assertEqual(looks["original"], looks["converted"])
                self.assertEqual(looks["original"], looks["mixed"])
                self.assertEqual(9, len(looks["original"]["read"]), "nobody was reaped")
                self.assertEqual(["interactive", "337"], looks["original"]["read"]["held-interactive"])
                self.assertEqual(["normal", "62"], looks["original"]["read"]["wait-normal-ticket"])
                self.assertEqual(["batch", None], looks["original"]["read"]["wait-batch"])
                self.assertTrue(looks["original"]["grants"], "the comparison must include grants")

        # Both versions read every request alike; their schedulers differ by design since (#5, #7).
        old_look, new_look = self.drive(self.old_root, original, "look"), self.drive(self.new_root, converted, "look")
        self.assertEqual(old_look["read"], new_look["read"])
        self.assertEqual(old_look["status"], new_look["status"])

        for root in (self.old_root, self.new_root):
            with self.subTest(code=root.name, action="grant"):
                states = [self.drive(root, self.copy(state, f"{root.parent.name}-{name}-grant"), "grant")
                          for name, state in (("original", original), ("converted", converted), ("mixed", mixed))]
                self.assertEqual(states[0], states[1])
                self.assertEqual(states[0], states[2])
                self.assertEqual(9, len(states[0]), "a grant reaped nobody")
                self.assertIn("held", [s for label, s in states[0].items() if label.startswith("wait-")])


class RealClientsTests(CutoverCase):
    """Real old-client processes hold and wait across the install, and an old client queues after it."""

    def old_client(self, env: dict[str, str], *args: str) -> subprocess.Popen[str]:
        code = f"import sys; sys.path.insert(0, {str(self.old_root)!r}); from slotfather.cli import main; sys.exit(main())"
        process = subprocess.Popen([sys.executable, "-c", code, *args], env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, start_new_session=True)
        self.processes.append(process)
        return process

    def new_client(self, command: Path, env: dict[str, str], *args: str) -> subprocess.Popen[str]:
        process = subprocess.Popen([str(command), *args], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, start_new_session=True)
        self.processes.append(process)
        return process

    def entries(self, state: Path) -> dict[str, dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        for path in sorted((state / "queue").glob("*.json")):
            try:
                entry = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            found[entry["label"]] = entry
        return found

    def test_old_holders_and_waiters_finish_normally_across_the_install(self) -> None:
        home = self.tmp / "home"
        state = home / ".local/state/slotfather"
        state.mkdir(parents=True)
        source = self.tmp / "source"
        source.mkdir()
        for name in ("install.py", "slotfather", "slotfather-dashboard", "slotfather-stats", "slotfather-sweep"):
            shutil.copyfile(ROOT / name, source / name)
        shutil.copytree(ROOT / "src", source / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        env = {**self.environment(state), "HOME": str(home)}
        work = ("--", "sh", "-c", WORK, "work")

        holder = self.old_client(env, "run", "-n", "all", "--label", "old-holder", "--priority", "interactive",
                                 "--ticket", "11", "--meta", "lane=a", *work, "old-holder")
        self.wait_for(lambda: "start old-holder" in self.log.read_text(), "the old holder to start")
        batch = self.old_client(env, "run", "-n", "1", "--label", "old-batch", "--priority", "batch",
                                "--ticket", "12", *work, "old-batch")
        early = self.old_client(env, "run", "-n", "1", "--label", "old-interactive", "--priority", "interactive",
                                "--meta", "lane=c", *work, "old-interactive")
        self.wait_for(lambda: {"old-batch", "old-interactive"} <= set(self.entries(state)), "the old waiters to queue")
        before = self.entries(state)
        self.assertTrue(all("class" in e["meta"] for e in before.values()), "the old client mirrors its priority")

        deadline = time.monotonic() + 30
        while True:  # Waiting clients take the queue lock for each look; the installer never waits for it.
            install = subprocess.run([sys.executable, str(source / "install.py"), "--python", sys.executable],
                                     env=env, capture_output=True, text=True, timeout=60)
            if "queue operation in progress" not in install.stderr or time.monotonic() > deadline:
                break
            time.sleep(0.1)
        self.assertEqual(0, install.returncode, install.stdout + install.stderr)
        self.assertIn("Converted request fields of 3 live queue entries", install.stdout)
        after = self.entries(state)
        self.assertEqual(set(before), set(after))
        for label, entry in after.items():
            self.assertNotIn("class", entry.get("meta", {}), label)
            self.assertNotIn("ticket", entry.get("meta", {}), label)
            for key in ("pid", "start", "count", "state", "granted", "priority", "ticket", "since", "all", "until"):
                self.assertEqual(before[label].get(key), entry.get(key), (label, key))
        self.assertEqual({"lane": "a"}, after["old-holder"]["meta"])
        for process in (holder, batch, early):
            self.assertIsNone(process.poll(), "the install stopped nobody")

        new = home / ".local/bin/slotfather"
        status = json.loads(subprocess.run([str(new), "status", "--json"], env=env, capture_output=True, text=True,
                                           timeout=20, check=True).stdout)
        self.assertEqual({("old-holder", "held", "interactive", "11"), ("old-batch", "waiting", "batch", "12"),
                          ("old-interactive", "waiting", "interactive", None)},
                         {(e["label"], e["state"], e["priority"], e["ticket"]) for e in status["entries"]})

        # Still-running old code queues again after the install: an old-format entry beside converted ones.
        late = self.old_client(env, "run", "-n", "1", "--label", "old-late", "--priority", "interactive",
                               "--ticket", "14", *work, "old-late")
        self.wait_for(lambda: "old-late" in self.entries(state), "the late old client to queue")
        self.assertEqual({"class": "interactive", "ticket": "14"}, self.entries(state)["old-late"]["meta"])
        current = self.new_client(new, env, "run", "-n", "1", "--label", "new-client", "--priority", "normal",
                                  "--ticket", "15", *work, "new-client")
        self.wait_for(lambda: "new-client" in self.entries(state), "the new client to queue")
        status = json.loads(subprocess.run([str(new), "status", "--json"], env=env, capture_output=True, text=True,
                                           timeout=20, check=True).stdout)
        found = {e["label"]: (e["state"], e["priority"], e["ticket"]) for e in status["entries"]}
        self.assertEqual(("waiting", "interactive", "14"), found["old-late"], "the new code reads the late entry")
        self.assertEqual(5, len(found), "nobody was reaped")
        lines = subprocess.run([str(new), "status"], env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(0, lines.returncode, lines.stderr)

        (self.release_dir / "old-holder").touch()
        for label in ("old-interactive", "old-late", "old-batch", "new-client"):
            self.wait_for(self.started(label), f"{label} to be granted")
            (self.release_dir / label).touch()
        for process in (holder, batch, early, late, current):
            output, _ = process.communicate(timeout=30)
            self.assertEqual(0, process.returncode, output)
        events = (state / "queue.log").read_text()
        self.assertNotIn("reaped", events)
        history = {row["label"]: row for row in map(json.loads, (state / "history.jsonl").read_text().splitlines())}
        self.assertEqual({"old-holder", "old-batch", "old-interactive", "old-late", "new-client"}, set(history))
        self.assertTrue(all(row["outcome"] == "released" for row in history.values()), history)
        self.assertEqual(("interactive", "11"), (history["old-holder"]["priority"], history["old-holder"]["ticket"]))
        self.assertEqual(("batch", "12"), (history["old-batch"]["priority"], history["old-batch"]["ticket"]))
        self.assertEqual("14", history["old-late"]["ticket"])
        self.assertEqual([], [p.name for p in (state / "queue").iterdir()])


if __name__ == "__main__":
    unittest.main()
