# Checks

- Full gate: `python3 tools/check.py` runs Ruff, strict Pyright against the
  shrinking baseline, and all unit and compatibility tests. The test runner
  acquires machine capacity through Slotfather.
- Focused tests: `slotfather run -n 1 -- python3 -m unittest discover -s tests -p 'test_<area>.py'`.
- Lint and types: `slotfather run -n 1 -- python3 tools/check.py --lint`.
- Compatibility fixtures in `tests/fixtures/` preserve the older tool's behavior;
  update current expectations without rewriting that historical reference.
