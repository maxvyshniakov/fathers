# Architecture and compatibility

Slotfather is a Python package under `src/slotfather/`. The three repository executables
(`slotfather`, `slotfather-stats`, `slotfather-sweep`) are entry points; installation copies a
self-contained package, without pip or runtime packages.
The source package lives under `src` because the root `slotfather` name is the executable.

Definitions: [glossary](../GLOSSARY.md). Naming and wire aliases: [terminology](terminology.md).

## Modules

| Module | Owns |
| --- | --- |
| `archive` | Cross-host history export and import (`history export`, `history import HOST [FILE]`) into `hosts/HOST/history.jsonl`. |
| `board` | The dashboard's model from one look, the `dashboard` block of `status --json --full` that Godfather draws. |
| `capabilities` | `capabilities --json`: the dashboard contracts this release supports. |
| `changelog` | `changelog --json [--since VERSION]`: parses the installed release's `CHANGELOG.md` as structured entries. |
| `cli` | Table-driven argument parsing, per-command `--help`, `--version`, and command dispatch. |
| `client` | The waiting client: `Lease`, `acquire()`, `held_again()` and `yield_now()`, one wait loop (`wait_until()`) with the poll settings read once. Requests share `wait_held()`, including runs joining a measurement session. The library's entry point. |
| `config` | Environment settings, installation defaults, constants, shared errors and the clock helpers. |
| `contract` | `TypedDict` definitions for JSON snapshots (`status --json`, `status --json --full` `dashboard` block) and `events.jsonl` envelopes and payloads (`KINDS`), checked against `docs/api.md` (fathers#125). |
| `durations` | The three duration grammars: `clock()` (`1h30m`: order, settings, sessions), `span()` (`1d`: sweep config), `since()` (ages and dates: stats). |
| `entry` | A queue entry: its persisted fields, the accessors that read them whoever wrote the file, its phase, and the one definition of each transition (new request, grant and resume, yield, priority change, outcome). Pure: callers read and write the files and log. |
| `estimates` | Run-time estimates from the history's finished runs (`Estimates`), their `estimates.json` cache and `estimator()`, which asks a run type's expectations first. Loaded only when an estimate is first asked (`leases.estimator()`), off the admission path. |
| `events` | `record()`, the one writer of a transaction's queue.log line and its event (fathers#37), and the event-kind table `KINDS` with a TypedDict per kind's details; events.jsonl in Godfather's shared event schema, never failing an operation. |
| `idle` | Idle lease detection and container/VM exemption (`idle.container()`), one definition shared across readers. |
| `labels` | Default label derivation (`choose()`) from the request's git repository, command, lane, ticket, and phase. |
| `lane_admission` | Stateless versioned lifetime lane policy and JSON validation; no execution state. |
| `lane_context` | Optional read-only Lanefather summaries without depending on Lanefather's storage schema. |
| `leases` | The queue's transactions (fathers#37), each short and under the lock: a new request (`enqueue()`), `grant()`, yield requests, yields (also for a lock: `yield_for_lock()`, `lock_taken()`), `reprioritize()` and `release()`. |
| `lineage` | Process trees and sessions on a table already read: parents, ancestors, descendants and the one session rule; it runs nothing. |
| `lockwait` | `slotfather lock PATH -- COMMAND` (fathers#159): an flock whose wait gives the enclosing lease's slots up. |
| `macos_process` | Pure leaf macOS `libproc` process identities, `KERN_PROCARGS2` environments, and targeted `/bin/ps` argument reads with identity validation (stdlib-only imports; loaded lazily on macOS). |
| `platform` | macOS/Linux host inspection: `linux()`/`macos()` predicates, process identity (`same_holder()`), unified `memory_snapshot()`, swap, `vm_stat`, PSI, load, CPU ticks, and cgroups. |
| `process` | The one module that observes live processes (`ps`, `lsof`, `/proc/PID/cwd`, `launchctl`/`systemctl` service managers, lazy native macOS dispatch): the process tables with their column sets and failure policy (fathers#121), lease markers in environments, detached browser helper attribution, and `lease_membership()`, the one rule for which lease a process is in (fathers#38). |
| `procs` | Read-only process queries (`slotfather procs --json`) for guards and other tools, delegating OS process/CWD reads to `process`. |
| `query` | Compatible queue snapshots and status output; `look()`, the one look under the queue lock that every reader takes (#20, fathers#36): `status`, `status --json` (with `--pid`, `--full`), the long-wait line, `available`, `advise`, `slots`, the dashboard block and the sweep's protection. |
| `queue` | The queue as stored, read (fathers#36): the process table a look is judged on, reaping, the earlier queues' holders, unknown live entries, the lease this process runs under, lazy estimators, and live `schedule.Host` / `schedule.Admission` wiring (`live_host()`, `admission()`). |
| `runner` | The command runner: `execute()` (environment, SIGTERM/SIGHUP forwarding, wait4, the usage `Watch`) and `run()`, `slotfather run`. |
| `runtype` | Expected durations per run type (fathers#106) with their own cache, outliers and trends; `kind()` and `family()`, the command kind and label family the estimates key requests by. |
| `schedule` | The queue's policy (fathers#35, fathers#214): `Admission` and lazy `Host` unify ranking, backfill, reservations, resource gates (memory pressure, `memory_share` peak budget, sustained CPU load), room checks, and forecasts; yield and idle asks; on entries, capacity, order settings, a moment, the overtaking window and estimators its callers hand it. It reads no file, environment or clock; `free_at()` is the one walk of when slots free by expected ends. |
| `session` | Measurement sessions (`session start`, `session stop`, `run --session`): one lease held across a series of runs until stopped or idle. |
| `settings` | The shared settings (fathers#34): capacity (`capacity_basis()`, `total()`, what a `slots` mode gives), order (`order_settings()`, `checked_order()`) and sweep settings, and `configure()`, the only write path of capacity.json, order.json and sweep.json (lock, ownership, conflicts, rollback; the caller passes the grant to run after a change). |
| `settings_cli` | `settings --json/check/set/reset`: the shared settings as data; labels, checks and previews the Settings panel shares. |
| `state` | Queue locking, atomic files, bounded journals and persisted schema contracts; it imports no admission code (no `leases`, `schedule`, `runtype`, `events` or `platform`). |
| `stats` | Historical analysis of `history.jsonl`, `machine.jsonl` and `sweep.jsonl` (`slotfather stats` / `slotfather-stats`). |
| `sweep` | Guarded leftover-process and disk-hygiene command, settings, report and launchd plist (`slotfather sweep` / `slotfather-sweep`). |
| `sweep_disk` | Machine-wide disk hygiene (`docker-anonymous`, `docker-dangling-images`, `docker-superseded`, `claude-scratch`, `package-caches`, `low-disk`) with Junkfather scratchpad sparing (fathers#6). |
| `sweep_os` | Machine adapters for `sweep`: `ps`, `lsof`, `/proc`, `launchd`, `systemd`, `docker`, memory inspection, and signalling. |
| `sweep_plan` | Pure process classifier and cleanup planner operating on a `sweep_os` snapshot. |
| `usage` | Process-tree accounting and machine sampling while work runs. |
| `version` | Runtime source identity (`version.json` or checkout digest) and `version` / `--version` output. |

No module imports terminal code (`termios`, `tty`, `curses`): the dashboard is Godfather's (#28), and `slotfather
dashboard` only runs `godfather`. `tests/test_godfather.py` checks that admission, status, settings, procs, stats,
history and version load none of it.

Scheduling decisions operate on entry data; OS inspection and file mutation stay outside
those decisions. Optional views load separately from admission. Tests cross the same package
interfaces as callers and replace OS/time inputs at their boundaries.

## State on disk

The queue directory defaults to `~/.local/state/slotfather`; `SLOTFATHER_DIR` overrides it.
A pinned installation shares one user's directory even when a service changes `HOME`.

| File | Format and compatibility |
| --- | --- |
| `schema.json` | State schema 1. Unstamped queues are adopted without rewriting entries; unknown versions are refused before mutation. Old clients ignore the stamp and can release existing leases. |
| `queue/*.json`, `sequence`, `.lock` | Existing queue format: atomic JSON entries and a shared advisory lock. PID plus process start time identifies a holder. Current entries add `started` (epoch seconds; see Stable surfaces). Entries and `sequence` are written whole: fsynced, renamed, then the directory fsynced. A new request's sequence is past every entry in the queue as well as the file's, so a torn `sequence` (older clients write it in place) never puts it ahead. An entry that does not read is left in place and listed by `status` as `unreadable`. An entry that reads but that this client does not know (a `state` other than `waiting`/`held`, none, or a `pid`, `start`, `since` or `count` missing; `entry.unknown()`, fathers#19) may be a newer client's lease: it is left in place, never granted, reaped or ranked, its count reserved while its holder runs, and listed by `status`, `status --json` and the dashboard block as `unknown`; `procs --json` and the installer read one whose holder is gone as absent, with a warning. |
| `capacity.json`, `order.json` | Shared settings. Keep old keys and meanings; older clients ignore added fields. |
| `queue/*.yield` | Cooperative requests, never permission to stop a holder. |
| `queue.log[.1]` | Timestamped grants, releases and reaping; retained event syntax supports existing history readers. Past 4 MiB it becomes `queue.log.1`, replacing the older one, under the queue lock; older clients keep appending to whichever file is current. |
| `history.jsonl[.1]` | Current record version 2. Unversioned version-1 history remains readable. |
| `machine.jsonl[.1]` | Record version 1; unavailable facts remain unavailable. |
| `estimates.json` | Disposable cache version 2. Rebuilt when its version or history stamp changes. |
| `sweep.json`, `sweep.jsonl[.1]` | Cleanup settings and version-1 results. Cleanup never owns lease admission. |

Every client that looks at the queue grants for all of it by its own order, so during an upgrade the queue
admits what either version's order allows. An old client still makes a batch request the head in front of
waiting normal work (#5), and a new client's look lets that work pass. Entries and history carry no new
fields for this, so either version reads the other's queue as before.

A structural migration must preserve these files and active leases. Adding data is safe only
when old readers can ignore it and old writers can still release their leases. A breaking
schema needs a distinct migration, validation and an explicit live-client cutoff; moving Python code is
not a reason to rewrite queue state. Derived caches may be rebuilt rather than migrated.

## Stable surfaces

- The documented CLI and JSON request format is current; retired metadata-field aliases are rejected.
- `status --json` and `procs --json` use version 2 with priority and ticket request fields.
- Process identity is a PID plus its start time. A recycled PID must never inherit a lease
  or permission to stop a process.
- A holder's identity on disk (fathers#122): `start` is `ps -o lstart` as every older client of that system
  writes it (Linux: the C locale; macOS: the client's own locale and time zone), and current clients add
  `started`, the start in epoch seconds (macOS libproc; Linux `/proc`), which older clients read past.
  Current clients compare with one function (`platform.same_holder()`): `started` against the pid's own start
  where both are known, so neither locale nor time zone matters between them; otherwise `start` as text, then by
  its clock time, day, year and numeric or C-locale month (`platform.same_start()`). Older clients compare `start`
  as text, exactly as before, so during an upgrade an older and a current client read each other's leases as
  two older clients in the same locales and time zones would: no worse than before.
- A process table ps cannot give (it fails, times out, or leaves out its caller) reaps nothing (fathers#121). A
  request already in the queue skips that look and looks again, as do the other long-running looks; a command
  that has not queued yet (`run`, `session start`, `ask-yield`, `reprioritize`, `status`, `sweep`) fails at once
  with the reason and changes nothing.
- Installed commands bind to an immutable package release; replacement entry points do not
  change code underneath an already running old client. Releases are retained while old
  clients may still use them.
- `slotfather version` identifies the installed source so local and remote installations can
  be compared. Installation does not take over queue admission or cancel running work. Installation converts
  live entries of clients that already write request fields in place and refuses live clients
  older than them until they have drained (#8).
- Full-argument inspection on macOS (`macos_process`) uses targeted `/bin/ps` validated against native `libproc`
  identities rather than falling back to a truncated executable name; empty lease start identities are rejected
  rather than invented, and hidden macOS process environments remain explicitly `lease_unknown`.
- `lane-admission --json` is strictly stateless and additive: it validates inputs strictly (rejecting duplicate
  JSON fields) and never reads or mutates execution queue state.

## Scheduling design decisions

The shipped scheduling defaults were validated by replaying recorded queues through `schedule.plan()` with
`python3 tools/replay.py` (supports `--since`, `--until`, `--families`, and `--json`):

- **Backfill with overtaking window (`600 s`) and safety factor (`3.0`)**: pure EASY backfill without an overtaking
  window worsened total wait because every non-fitting waiter became a head immediately; lower safety (`1.5`) admitted
  overruns that stretched large-request waits.
- **Priority classes (`±1800 s`) with cooperative yields after `120 s`**: holding batch work behind normal/interactive
  requests and asking yieldable lower-class holders to yield between steps cut interactive p90 waits sharply without
  ever stopping or signalling a holder (`SIGSTOP`/`SIGTERM` are never used for preemption).
- **Heads that give up first hold nobody back**: a waiter whose `--wait` deadline expires before held leases are
  expected to free its slots (counting current overrun) does not block fitting work behind it.
- **Batch whole-machine waiters yield to waiting normal/interactive work**: a batch `-n all` request does not hold
  back waiting normal or interactive requests until it ages, eliminating idle slot-hours under interactive load.

Executable-file internals are not the package API. External tools should use documented JSON
commands; Python integrations should import the module that owns the behavior they use.
