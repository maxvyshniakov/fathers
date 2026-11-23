# Checks

The installed command uses only Python's standard library (3.11+). Ruff and Pyright are development checks, pinned
in `requirements-dev.txt`.

## Gate

The repository's [checks](../../../docs/agents/checks.md) run Spawnfather's lint gate and tests whenever a change
touches `spawnfather/` (`bun run verify`, the pre-push hook, a lane check). By hand, each through one machine slot:

```sh
slotfather run -n 1 --priority batch --label "<task> spawnfather lint" -- python3 tools/check.py
slotfather run -n 1 --priority batch --label "<task> spawnfather tests" -- python3 -m unittest discover -s tests
```

Timing budgets live in `tests/perf/` (fathers#99), which the default discovery does not enter; run them before a
release, alone on the machine: `slotfather run -n all --priority batch --label "<release> spawnfather perf" --
python3 -m unittest discover -s tests/perf`. Elsewhere a test waits on a condition or an injected clock, never on a
fixed sleep or an elapsed-time assert (`repo-kit test-hygiene`).

Spawnfather has no `tools/type-baseline.json`: every Ruff or Pyright diagnostic fails. For a focused change, run its
test file first (`python3 -m unittest tests/test_reap.py`). Tests run real child processes in a scratch state
directory with a fake Claude Code session registry, and must clean up every process they start. Nothing of the
machine may count in them: the resource tests name their own Safari Technology Preview (`SPAWNFATHER_STP_APP`), so
a running instance, a person's or another suite's, never makes their quit refuse.

## Review

- Every signal goes to a pid whose start time was read again just before; a reap never uses a group id without a
  verified leader of that group (docs/reference.md, Ending an entry).
- Nothing spawnfather did not start or adopt is stopped, except `reap --pid --force` on a pid the last sweep listed
  and `reap --pid --started --cmd` on a pid whose start second and command both still match (fathers#171).
  The sweep deletes two things no entry owns, both owner-approved: an unowned git worktree that passes every guard
  of `worktrees.plan` (read again right before) and was idle for a day (fathers#112), and an agent-browser session's
  files whose daemon and starting pid are gone and that did not change for a day, checked again right before each
  removal (fathers#115). Any other deletion surface is a review finding.
- Unknown liveness (unreadable process table, no owner process) never reaps.
- The hook never blocks or fails, and prints only its answer to a background Bash call (docs/reference.md, Background
  Bash calls): a slow reap runs detached. That rewrite adopts only the shell that runs it; its watcher takes from
  the shell's group only at the kernel's word (or a start time that held), and an adopted entry's reap leaves out
  what another entry holds.
