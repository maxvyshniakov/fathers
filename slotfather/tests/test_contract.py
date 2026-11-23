"""Contract checks (fathers#125): `slotfather.contract` TypedDicts match `docs/api.md` and live JSON outputs."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from slotfather import board as sf_board
from slotfather import config as sf_config
from slotfather import contract as sf_contract
from slotfather import entry as sf_entry
from slotfather import events as sf_events
from slotfather import query as sf_query
from slotfather import state as sf_state

ROOT = Path(__file__).resolve().parents[1]
API_DOC = ROOT / "docs/api.md"
BACKTICK = re.compile(r"`([^`]+)`")


def strip_parens(text: str) -> str:
    """Remove parenthetical explanations `( ... )` (including nested ones) so only top-level tokens remain."""
    out: list[str] = []
    depth = 0
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")" and depth > 0:
            depth -= 1
        elif depth == 0:
            out.append(ch)
    return "".join(out)


def typed_dict_keys(td: type) -> set[str]:
    required: frozenset[str] = getattr(td, "__required_keys__", frozenset())
    optional: frozenset[str] = getattr(td, "__optional_keys__", frozenset())
    return set(required | optional)


def typed_dict_required(td: type) -> set[str]:
    return set(getattr(td, "__required_keys__", frozenset()))


def bullet_head_keys(block: str) -> set[str]:
    """Extract backticked field names before the first `:` of each top-level `- ` bullet in `block`."""
    found: set[str] = set()
    for line in block.splitlines():
        if not line.startswith("- "):
            continue
        head = strip_parens(line[2:].split(":", 1)[0])
        found.update(BACKTICK.findall(head))
    return found


class ApiDocContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.doc = API_DOC.read_text(encoding="utf-8")

    def test_entry_reexported_from_entry_module(self) -> None:
        self.assertIs(sf_entry.Entry, sf_contract.Entry)

    def test_status_json_top_level_and_entry_keys_match_docs_api(self) -> None:
        section = self.doc.split("## `status --json`", 1)[1].split("## Dashboard contracts", 1)[0]
        top_part = section.split("Top-level fields:", 1)[1].split("Per-entry fields:", 1)[0]
        entry_part = section.split("Per-entry fields:", 1)[1].split("**Unknown entries**", 1)[0]
        self.assertEqual(typed_dict_keys(sf_contract.StatusSnapshot), bullet_head_keys(top_part))
        self.assertEqual(typed_dict_keys(sf_contract.StatusEntry), bullet_head_keys(entry_part))

    def test_full_status_and_dashboard_block_keys_match_docs_api(self) -> None:
        dash = self.doc.split("**`slotfather status --json --full`**", 1)[1].split("**`slotfather settings`**", 1)[0]
        intro, bullets = dash.split("\n- every datetime", 1)
        added_top = set(BACKTICK.findall(strip_parens(intro.split("plus three top-level fields:", 1)[1].split(".", 1)[0])))
        self.assertEqual(
            typed_dict_keys(sf_contract.StatusSnapshot) | added_top,
            typed_dict_keys(sf_contract.FullSnapshot),
        )
        items: list[str] = []
        for raw in ("\n- every datetime" + bullets).split("\n- "):
            if raw.strip():
                items.append(raw.split("\n\n", 1)[0])
        block_bullet = next(item for item in items if item.startswith("`v`"))
        row_bullet = next(item for item in items if item.startswith("a row:"))
        event_bullet = next(item for item in items if item.startswith("an event:"))

        self.assertEqual(
            typed_dict_keys(sf_contract.DashboardBlock),
            set(BACKTICK.findall(strip_parens(block_bullet))),
        )
        unknown_json = re.search(r"`(\{\"id\".*?\})`", block_bullet)
        self.assertIsNotNone(unknown_json)
        assert unknown_json is not None
        unknown_keys = set(re.findall(r'"([^"]+)"', unknown_json.group(1)))
        self.assertEqual(typed_dict_keys(sf_contract.DashboardUnknown), unknown_keys)

        row_body = row_bullet.split("a row:", 1)[1].rsplit("Strings have", 1)[0]
        self.assertEqual(
            typed_dict_keys(sf_contract.DashboardRow),
            set(BACKTICK.findall(strip_parens(row_body))),
        )
        event_body = event_bullet.split("an event:", 1)[1]
        self.assertEqual(
            typed_dict_keys(sf_contract.DashboardEvent),
            set(BACKTICK.findall(strip_parens(event_body))),
        )

    def test_events_envelope_and_every_kind_details_match_docs_api(self) -> None:
        events_sec = self.doc.split("**`events.jsonl`**", 1)[1].split("**Run types**", 1)[0]
        bullets_part, table_part = events_sec.split("| Kind | When | `details` |", 1)
        envelope_bullets = "\n".join(
            line for line in bullets_part.splitlines()
            if line.startswith("- `")
        )
        self.assertEqual(typed_dict_keys(sf_contract.Event), bullet_head_keys(envelope_bullets))

        subject_line = next(
            block for block in bullets_part.split("\n- ") if block.startswith("`subject`:")
        )
        actor_line = next(
            block for block in bullets_part.split("\n- ") if block.startswith("`actor`:")
        )
        subject_parens = " ".join(re.findall(r"\(([^)]*)\)", subject_line))
        self.assertEqual(
            typed_dict_keys(sf_contract.Subject),
            {tok for tok in BACKTICK.findall(subject_parens) if tok.isidentifier()},
        )
        actor_tokens = {
            tok for tok in BACKTICK.findall(actor_line.split(":", 1)[1])
            if tok.isidentifier() and tok.islower() and tok not in {"slotfather", "godfather"}
        }
        self.assertEqual(typed_dict_keys(sf_contract.Actor), actor_tokens)

        documented_kinds: dict[str, set[str]] = {}
        for line in table_part.splitlines():
            line = line.strip()
            if not line.startswith("|") or line.startswith("| ---"):
                continue
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            self.assertEqual(3, len(cells), line)
            kinds = BACKTICK.findall(cells[0])
            detail_keys = set(BACKTICK.findall(strip_parens(cells[2])))
            for kind in kinds:
                documented_kinds[kind] = detail_keys

        self.assertEqual(set(sf_contract.KINDS), set(documented_kinds))
        for kind, shape in sf_contract.KINDS.items():
            with self.subTest(kind=kind):
                self.assertEqual(typed_dict_keys(shape), documented_kinds[kind])


class LiveShapeConformanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="slotfather-contract-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.state = self.tmp / "state"
        (self.state / "queue").mkdir(parents=True)

    def test_status_json_full_status_and_events_conform_to_contracts(self) -> None:
        pid = os.getpid()
        table = sf_query.status_processes(self.state, pid)
        start = table[pid][1]
        held_entry = sf_entry.new_request(
            pid, start, 1, "contract-held", "true", str(self.tmp), sf_config.now(), "normal", "lease-held-1",
            max_count=2,
        )
        sf_entry.to_held(held_entry, sf_config.now(), 1_800_000_000.0)
        waiter_entry = sf_entry.new_request(
            pid, start, 2, "contract-wait", "sleep 1", str(self.tmp), sf_config.now(), "batch", "lease-wait-2",
        )
        sf_state.write(self.state / "queue/0000000001-held.json", held_entry)
        sf_state.write(self.state / "queue/0000000002-wait.json", waiter_entry)
        sf_state.write(self.state / "queue/0000000003-future.json", {"state": "future", "pid": pid, "start": start, "count": 1})
        sf_events.record(
            self.state, "granted 1 to contract-held (pid 1)", "lease.granted",
            {"type": "request", "id": "0000000001-held", "label": "contract-held", "pid": pid},
            {"count": 1, "priority": "normal", "waited": 0, "meta": {}},
        )

        snap = sf_query.status_json(self.state)
        self.assertTrue(typed_dict_required(sf_contract.StatusSnapshot) <= set(snap))
        self.assertTrue(set(snap) <= typed_dict_keys(sf_contract.StatusSnapshot))
        self.assertEqual(3, len(snap["entries"]))
        for item in snap["entries"]:
            self.assertTrue(typed_dict_required(sf_contract.StatusEntry) <= set(item), set(item))
            self.assertTrue(set(item) <= typed_dict_keys(sf_contract.StatusEntry), set(item) - typed_dict_keys(sf_contract.StatusEntry))

        by_pid = sf_query.status_json(self.state, pid=pid)
        self.assertTrue(typed_dict_required(sf_contract.StatusSnapshot) <= set(by_pid))
        self.assertTrue(set(by_pid) <= typed_dict_keys(sf_contract.StatusSnapshot))
        self.assertEqual(typed_dict_keys(sf_contract.PidQuery), set(by_pid["query"]))

        full = sf_board.full_status(self.state)
        self.assertTrue(typed_dict_required(sf_contract.FullSnapshot) <= set(full))
        self.assertTrue(set(full) <= typed_dict_keys(sf_contract.FullSnapshot))
        dash = full["dashboard"]
        self.assertEqual(typed_dict_keys(sf_contract.DashboardBlock), set(dash))
        for row in dash["holders"] + dash["waiters"]:
            self.assertEqual(typed_dict_keys(sf_contract.DashboardRow), set(row))
        for unk in dash["unknown"]:
            self.assertEqual(typed_dict_keys(sf_contract.DashboardUnknown), set(unk))
        for ev in dash["events"]:
            self.assertTrue(typed_dict_required(sf_contract.DashboardEvent) <= set(ev))
            self.assertTrue(set(ev) <= typed_dict_keys(sf_contract.DashboardEvent))

        raw_event = json.loads((self.state / "events.jsonl").read_text(encoding="utf-8").splitlines()[0])
        self.assertTrue(typed_dict_required(sf_contract.Event) <= set(raw_event))
        self.assertTrue(set(raw_event) <= typed_dict_keys(sf_contract.Event))

    def test_each_entry_names_its_holders_uid_from_its_file(self) -> None:
        """fathers#88: `uid` is the entry file's owner, so an entry an older client wrote has one too; once the file
        is gone it is null. --full carries the same entries (Godfather pairs its rows with them)."""
        pid = os.getpid()
        start = sf_query.status_processes(self.state, pid)[pid][1]
        older = {"pid": pid, "start": start, "count": 1, "label": "older", "cmd": "true", "cwd": str(self.tmp),
                 "since": sf_config.now(), "state": "waiting"}  # no lease_id, no uid: as an older client wrote it
        path = self.state / "queue/0000000001-older.json"
        sf_state.write(path, older)
        self.assertNotIn("uid", json.loads(path.read_text()), "nothing new is stored in the entry")
        snap = sf_query.status_json(self.state)
        self.assertEqual([os.geteuid()], [item["uid"] for item in snap["entries"]])
        full = sf_board.full_status(self.state)
        self.assertEqual([os.geteuid()], [item["uid"] for item in full["entries"]])
        self.assertEqual(os.geteuid(), sf_query.entry_item(path, older, {})["uid"])
        path.unlink()
        self.assertIsNone(sf_query.entry_item(path, older, {})["uid"])


if __name__ == "__main__":
    unittest.main()
