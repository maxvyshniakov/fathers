# Checks

The repository's [checks](../../../docs/agents/checks.md) run Lanefather's lint gate and tests whenever a change
touches `lanefather/` (`bun run verify`, the pre-push hook, a lane check). By hand, each through one machine slot:

- Lint and types: `slotfather run -n 1 --label "<task> lanefather lint" -- python3 tools/check.py`: Ruff with the
  shared rules and strict Pyright against `tools/type-baseline.json`, a budget that only shrinks
  (`python3 tools/check.py --shrink-baseline` after fixing debt).
- All tests: `slotfather run -n 2 --label "<task> lanefather tests" -- python3 -m unittest discover -s tests -p 'test_*.py'`.
- Focused tests: `slotfather run -n 1 --label "<task> lanefather tests" -- python3 -m unittest discover -s tests -p 'test_<area>.py'`.
- Compatibility fixtures in `tests/fixtures/` preserve the older tool's behavior;
  update current expectations without rewriting that historical reference.
