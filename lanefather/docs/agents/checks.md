# Checks

The repository's [checks](../../../docs/agents/checks.md) run Lanefather's lint gate and tests whenever a change
touches `lanefather/` (`python3 tools/checks.py verify`, the pre-push hook, a lane check). By hand, from `lanefather/`:

- Lint and types (run directly, ~10 s): `python3 tools/check.py` — Ruff with the shared rules and strict Pyright
  against `tools/type-baseline.json`, a budget that only shrinks (`python3 tools/check.py --shrink-baseline` after
  fixing debt). Before committing, also run `PYTHONPATH=tests python3 -m unittest tests/test_static_guards.py` and the
  root hygiene check (`python3 tools/hygiene.py`, or `python3 tools/checks.py run "test hygiene"`).
- Focused single-file tests (run directly): `PYTHONPATH=tests python3 -m unittest tests/test_<area>.py`.
- Whole test suite (under a slot):
  `slotfather run -n 2 --label "<task> lanefather tests" -- python3 -m unittest discover -s tests -p 'test_*.py'`.
- Compatibility fixtures in `tests/fixtures/` preserve the older tool's behavior;
  update current expectations without rewriting that historical reference.

## Review

In addition to the repository [review rules](../../../docs/agents/checks.md#review):

- All overrides go through `journal.override` (never direct `["overrides"].append`), and every ledger/event kind is
  registered in `events.KINDS`.
- Multi-step state transitions (hand-over, promote, post-merge smoke, review runs) must be crash-resumable or atomic
  under one lock without dual ownership.
- External verifier/CLI flags (such as `--only`, `--base`) are capability-gated rather than sent unconditionally to
  older or third-party verifiers.
- Ledger and receipt inputs (tree SHAs, file names, refs) are strictly validated and passed after `--end-of-options`
  or `--`.
