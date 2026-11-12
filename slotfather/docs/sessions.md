# Measurement sessions

A measurement series (`-n all` runs one after another) drained the queue once per run: every run waited for all
running leases to end, and the slots sat idle while it waited. A session drains once and holds the machine for the
whole series.

```sh
slotfather session start --label perf-640 --idle 2m --expect 40m   # returns once the slots are held
slotfather run --session perf-640 -- ./bench.sh a
slotfather run --session perf-640 -- ./bench.sh b
slotfather session stop perf-640                                    # or let it end after 2 minutes idle
```

- `session start --label NAME` queues one request, `-n all` unless `-n N` says otherwise, with `--priority`,
  `--ticket`, `--meta` and `--wait` as for `run`. A detached holder process waits in the queue and holds the lease;
  `start` relays what it says while it waits and returns once the lease is held (75 when `--wait` ran out). One
  session of a name at a time; a session cannot start inside a lease.
- `run --session NAME [--label L] [--meta K=V]... -- COMMAND` runs COMMAND under the session's lease, taking none of its own:
  `SLOTFATHER_LEASE` names the session's entry, so slotfather runs nested in it take no lease either. It waits while
  the session still waits for its slots, and refuses (exit 2) when there is no such session or it is stopping. It
  exits with COMMAND's status.
- The session ends when no run has been active for `--idle` seconds (default 120; a run counts as use for as long as
  it runs), at `session stop NAME` (after the active runs end; no new run starts meanwhile), or when its holder dies:
  the next look at the queue reaps it like any crashed holder.
- To the queue, older clients and the dashboard a session is one held lease. Waiters' expected starts use
  `--expect` where given, else the run time of earlier sessions of the label. `status` marks it
  (`session=NAME runs=N active=N`); `status --json` adds `session`: `name`, `idle`, `expect`, `stopping`, `runs`,
  `busy` (seconds runs were active), `last_use` and `active` runs (`pid`, `label`, `cmd`, `since`).
- The history has one line for the session (no line per run), with the summed CPU and peak memory of its runs and
  `session`: `name`, `idle`, `runs`, `busy`, `ended_by` (`idle` or `stopped`; a reaped session has none). Events:
  `session.run_started`, `session.run_ended` and `session.ended` beside the lease's own (see [api](api.md)).
