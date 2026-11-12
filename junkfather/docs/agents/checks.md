# Checks

The installed command uses only Python's standard library (3.9+). Ruff and Pyright are development checks, pinned
in `requirements-dev.txt` and run through `uvx`.

## Gate

Run each through one machine slot:

```sh
slotfather run -n 1 --priority batch -- python3 tools/check.py
slotfather run -n 1 --priority batch -- python3 -m unittest discover -s tests -v
```

`tools/check.py` has no baseline: every Ruff or Pyright diagnostic fails. For a focused change, run its test file
first (`python3 -m unittest tests/test_reap.py`). Tests run real child processes in a scratch state directory with a
fake Claude Code session registry, and must clean up every process they start.

## Review

- Every signal goes to a pid whose start time was read again just before; a reap never uses a group id without a
  verified leader of that group (docs/reference.md, Ending an entry).
- Nothing junkfather did not start or adopt is stopped, except `reap --pid --force` on a pid the last sweep listed.
- Unknown liveness (unreadable process table, no owner process) never reaps.
- The hook never prints, blocks or fails: a slow reap runs detached.
