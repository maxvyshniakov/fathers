# Checks

Installed commands use only Python's standard library. Ruff and Pyright are development
checks, pinned in `requirements-dev.txt`; they are not installed on runner hosts.

## Gate

The repository's [checks](../../../docs/agents/checks.md) run Slotfather's lint gate and tests whenever a change
touches `slotfather/` (`bun run verify`, the pre-push hook, a lane check). By hand, each through one machine slot:

```sh
slotfather run -n 1 --label "<task> slotfather lint" -- python3 tools/check.py
slotfather run -n 1 --label "<task> slotfather tests" -- python3 -m unittest discover -s tests
```

`tools/type-baseline.json` is the lint gate's budget of legacy debt, by relative file, tool, rule and message: an
increased count or a diagnostic in another file fails, a moved line does not consume a new budget, and the budget
cannot grow beyond committed history (CI fetches full history for that). It is not a claim that the codebase is
fully typed; new process-query and quality-gate modules start without debt. Do not suppress a new error, expand the
baseline or weaken the configuration to make a change pass. After fixing debt, `python3 tools/check.py
--shrink-baseline` removes its budget and refuses new diagnostics; `--initialize-baseline` is only for initial
adoption.

For a focused change, run its test file before the full gate. Tests use isolated directories
and child processes; they must not modify the live queue. See [testing](../testing.md) for
Linux-container and media acceptance. Remote installation acceptance must compare local and
remote `slotfather version` and preserve active leases.

## Review

Review the issue's behavior and compatibility contract separately from coding standards.
The architecture is described in [architecture](../architecture.md). Keep command parsing in
`cli`, filesystem mutation in `state`/`leases`, OS inspection in `platform`, and scheduling
decisions in `schedule`. No shared mutable execution namespace or wildcard-import coupling.

New process queries must validate process start times, distinguish unavailable inspection
from an empty result, and leave queue files unchanged. Installer changes must prove old
clients can finish active leases during an upgrade. Do not rerun heavy gates inside review.
