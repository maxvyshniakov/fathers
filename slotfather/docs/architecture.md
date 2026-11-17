# Architecture and compatibility

Slotfather is a Python package under `src/slotfather/`. The four repository executables are
entry points; installation copies a self-contained package, without pip or runtime packages.
The source package lives under `src` because the root `slotfather` name is the executable.

Definitions: [glossary](../GLOSSARY.md). Naming and wire aliases: [terminology](terminology.md).

## Modules

| Module | Owns |
| --- | --- |
| `config` | Environment settings, installation defaults, constants, shared errors and the clock helpers. |
| `platform` | macOS/Linux inspection: process identity (`same_holder()`), memory, swap, PSI and cgroups. |
| `process` | The one module that runs `ps`: the process tables with their column sets and failure policy (fathers#121), lease markers in environments, and `lease_membership()`, the one rule for which lease a process is in (fathers#38). |
| `lineage` | Process trees and sessions on a table already read: parents, ancestors, descendants and the one session rule; it runs nothing. |
| `state` | Queue locking, atomic files, bounded journals and persisted schema contracts; it imports no admission code (no `leases`, `schedule`, `runtype`, `events` or `platform`). |
| `entry` | A queue entry: its persisted fields, the accessors that read them whoever wrote the file, its phase, and the one definition of each transition (new request, grant and resume, yield, priority change, outcome). Pure: callers read and write the files and log. |
| `schedule` | The queue's policy (fathers#35): ranking, backfill, reservations, yield and idle asks, the memory gate and the forecast, on entries, capacity, order settings, a moment, the overtaking window and an estimate its callers hand it. It reads no file, environment or clock; `free_at()` is the one walk of when slots free by expected ends. |
| `estimates` | Run-time estimates from the history's finished runs (`Estimates`), their `estimates.json` cache and `estimator()`, which asks a run type's expectations first. Loaded only when an estimate is first asked (`leases.estimator()`), off the admission path. |
| `runtype` | Expected durations per run type (fathers#106) with their own cache, outliers and trends; `kind()` and `family()`, the command kind and label family the estimates key requests by. |
| `queue` | The queue as stored, read (fathers#36): the process table a look is judged on, reaping, the earlier queues' holders, unknown live entries, the lease this process runs under, and the lazy estimator. |
| `leases` | The queue's transactions (fathers#37), each short and under the lock: a new request (`enqueue()`), `grant()`, yield requests, yields (also for a lock: `yield_for_lock()`, `lock_taken()`), `reprioritize()` and `release()`. |
| `client` | The waiting client: `Lease`, `acquire()`, `held_again()` and `yield_now()`, one wait loop (`wait_until()`) with the poll settings read once. Requests share `wait_held()`, including runs joining a measurement session. The library's entry point. |
| `runner` | The command runner: `execute()` (environment, SIGTERM/SIGHUP forwarding, wait4, the usage `Watch`) and `run()`, `slotfather run`. |
| `lockwait` | `slotfather lock PATH -- COMMAND` (fathers#159): an flock whose wait gives the enclosing lease's slots up. |
| `usage` | Process-tree accounting and machine sampling while work runs. |
| `lane_admission` | Stateless versioned lifetime lane policy and JSON validation; no execution state. |
| `query` | Compatible queue snapshots and status output; `look()`, the one look under the queue lock that every reader takes (#20, fathers#36): `status`, `status --json` (with `--pid`, `--full`), the long-wait line, `available`, `advise`, `slots`, the dashboard block and the sweep's protection. |
| `board` | The dashboard's model from one look, the `dashboard` block of `status --json --full` that Godfather draws. |
| `settings` | The shared settings (fathers#34): capacity (`capacity_basis()`, `total()`, what a `slots` mode gives), order (`order_settings()`, `checked_order()`) and sweep settings, and `configure()`, the only write path of capacity.json, order.json and sweep.json (lock, ownership, conflicts, rollback; the caller passes the grant to run after a change). |
| `settings_cli` | `settings --json/check/set/reset`: the shared settings as data; labels, checks and previews the Settings panel shares. |
| `durations` | The three duration grammars: `clock()` (`1h30m`: order, settings, sessions), `span()` (`1d`: sweep config), `since()` (ages and dates: stats). |
| `events` | `record()`, the one writer of a transaction's queue.log line and its event (fathers#37), and the event-kind table `KINDS` with a TypedDict per kind's details; events.jsonl in Godfather's shared event schema, never failing an operation. |
| `capabilities` | `capabilities --json`: the dashboard contracts this release supports. |
| `procs` | Read-only process queries for guards and other tools. |
| `cli` | Argument validation and command dispatch. |
| `stats`, `sweep` | Historical analysis and guarded leftover-process cleanup: `sweep` is the command, its settings, report and plist; `sweep_plan` the pure classifier and planner on a snapshot; `sweep_os` its adapters (ps, lsof, /proc, launchd, systemd, docker, memory, stopping). |

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

Executable-file internals are not the package API. External tools should use documented JSON
commands; Python integrations should import the module that owns the behavior they use.
