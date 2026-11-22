# State, metadata and JSON status

The separate [lifetime lane-admission query](lane-admission.md) is stateless;
it does not use execution job budgets or reserve slots.

[Domain definitions](../GLOSSARY.md) · [Compatibility aliases](terminology.md)

## State and history

State lives in `~/.local/state/slotfather/` (`SLOTFATHER_DIR` overrides it). `queue.log` records grants, releases, and
reaping; `status` lists current holders and waiters.

`history.jsonl` stores one JSON line per request that left the queue. Past 4 MB it rotates to `history.jsonl.1`
(readers take `.1` first). `history [N]` lists the last `N` entries (default 20); `history export` prints both files
as JSON lines oldest first; `history import HOST [FILE]` merges another host's lines (stdin without `FILE`) into
`hosts/HOST/history.jsonl`, which `stats --host HOST` reads.

Key fields on a `history.jsonl` record:

- `label`, `cmd`, `cwd`, `count`: request label, command, working directory, and slot count.
- `queued`, `granted`, `ended`, `waited`, `ran`: queue, grant, and end timestamps plus seconds waited and run.
- `outcome`: `released`, `left` (gave up), or `reaped`.
- `until`: deadline timestamp for a request that used `--wait`.
- `status` and usage fields: for `run`, the exit status and process-tree CPU/RSS measurements (see Statistics).
- `git`: for a request in a git checkout, `head`, `tree`, and `dirty` (tracked files only; `SLOTFATHER_GIT_FACTS=0` disables it).
- `lease_id`: unique entry identifier matching `status --json` and request events' `subject` (older clients omit it; `status` synthesizes a stable `old-…` id).
- `label_derived` / `label_given`: derived label from project, command, and lane metadata, plus the explicit `--label` when different.
- `session`: present for a measurement [session](sessions.md) record.
- `meta.cache`: `--meta cache=hit|miss|partial` records cache reuse (other values are refused).

**Request fields.**

- `priority`: set via `run --priority interactive|normal|batch` (library: `acquire(..., priority=...)`); defaults to `normal`.
- `ticket`: optional issue identifier set via `--ticket ID` / `ticket=...` (`null` when absent in query responses).
- Both are top-level queue and history fields; inherited leases keep the outer request's fields.
- Built-in fields cannot be passed through metadata: `--class`, `meta.class`, `meta.priority`, and `meta.ticket` are rejected.

**Lease metadata.**

- Set via repeatable `run --meta key=value` (library: `acquire(..., meta={...})`) for caller context (`lane`, `phase`, `model`, `session`, and custom tags).
- Keys are lowercase (`a-z`, digits, `_ . -`), at most 32 characters; values are printable text of at most 200 characters; at most 16 keys (later keys replace earlier ones).
- Stored on the queue entry and history line (`"meta": {...}`), displayed by `status`, `history`, and the dashboard details panel, and grouped via `stats --by meta.KEY`.
- A nested `run` inheriting a lease runs under the outer lease's metadata and adds none of its own.

## `status --json`

`status --json` prints queue state for programs (such as a guard checking whether a process holds a lease).

Top-level fields:

- `v`: schema version (`2`).
- `slots`, `held`, `free`: total capacity, held slots, and free slots.
- `draining`: slots held above a capacity that shrank under running leases (`0` otherwise).
- `entries`: active holders, waiters, and unknown entries.
- `legacy`: leases still held in earlier queues during migration.
- `query` (`--pid PID` only): `{"pid", "start"}` where `start` is `ps -o lstart=` (`null` if not running), so a caller can check against PID reuse.
- `lease_unknown` (`--pid PID` only, when unreadable): `[PID]` when the process's environment cannot be read (another user's process or an Apple platform binary on macOS).

Per-entry fields:

- `id`, `lease_id`, `lease`: entry filename stem, unique lease identifier, and full path (`SLOTFATHER_LEASE`).
- `state`, `phase`: `state` is `held` or `waiting`; `phase` is `waiting`, `yielded`, `held`, `asked`, or `unknown`.
- `count`, `min_count`, `max_count`, `label`, `cmd`, `cwd`, `meta`: requested (or granted) slots, `-n MIN-MAX` bounds when a slot range was requested, label, command, working directory, and metadata.
- `pid`, `pgid`, `start`, `tree`: holder PID, process group, process start time, and live descendant PIDs.
- `since`, `granted`: queue arrival and grant timestamps.
- `priority`, `ticket`, `effective_priority`, `aged`, `reprioritized`: requested class and ticket, served class, whether ageing promoted it, and original class before `reprioritize` (`null` if unchanged).
- `yieldable`, `yields`, `yield_asked`, `position`, `lock`: cooperative-yield settings, yield count, standing yield request, queue position, and lock path while waiting on `slotfather lock` (only when waiting on a lock).
- `idle_since`: epoch seconds since a held lease became idle, or `null` (see Idle leases in scheduling).
- `est_start`, `head`, `reserved`, `waited`, `long_wait`, `memory_wait`: waiter's expected start, head flag, reserved start (UTC or `null`), wait duration in seconds, whether it passed `warn_wait`, and why memory holds admission back (`null` otherwise).
- `est_memory_mb`, `est_memory_source`, `expected`, `overrun` (omitted with `--pid`): expected peak RSS in MB and its source (`"history"` or `"default"`), expected run-type duration summary (see Run types below), and whether a held lease passed its `p90` (`overrun` only on held entries with `p90`).
- `session` (measurement sessions only): active [session](sessions.md) summary (`name`, `idle`, `expect`, `stopping`, `runs`, `busy`, `last_use`, `active`).
- `blocked_by`, `idle_slots`, `gave_way` (only when applicable, fathers#160): leases with no end estimate blocking a head past its reserved start (`[{"label", "pid"}]`), idle slot count, and the blocking list recorded when a head gave way.
- `unknown`, `holds`, `warning` (unknown entries only, fathers#19): why the entry is unknown, slots reserved while its holder runs, and the `status` warning text.
- `via` (`--pid PID` only): `"tree"` (holder or descendant), `"env"` (detached process carrying `SLOTFATHER_LEASE`), or `"helper"` (detached macOS browser/XPC helper attributed to a live lease, fathers#202). `procs --json` and `sweep` use the same membership rule (fathers#38).

**Unknown entries** (fathers#19): an entry whose `state` is neither `waiting` nor `held`, or whose `pid`, `count`, `start`, or `since` is missing or malformed, may belong to a newer client and is left untouched (never granted, reaped, ranked, or deleted).

- While its holder runs, its `count` stays reserved in `held` and `used`; if its holder cannot be checked, it reserves nothing.
- `status` lists it last as `unknown: queue/NAME (REASON; label=… pid=…): …`.
- `status --json` lists it with `phase: "unknown"`, `unknown` (reason), `holds` (reserved slots), and `warning` (`status` line text); scheduling fields are `null` or `false`.
- `procs --json` and `install.py` ignore an unknown entry whose holder is gone (with a warning) and refuse to proceed beside one whose holder is still running.

## Dashboard contracts

[Godfather](https://github.com/maxvyshniakov/fathers/tree/main/godfather), the shared terminal dashboard, reads Slotfather only through
these commands; it never imports Slotfather or reads its state files. Each reply carries its own version, and a
reader treats a version it does not know as unsupported.

**`slotfather dashboard [ARGS...]`**, and `slotfather` alone with a terminal on stdin and stdout, replace their
process with `godfather ARGS...` (found on `PATH`): the arguments pass through untouched and the exit code is
Godfather's. Without an executable `godfather` on `PATH` they print `slotfather: the dashboard is part of godfather
now; install it from https://github.com/maxvyshniakov/fathers/tree/main/godfather` on stderr and exit 2. Slotfather itself carries no
terminal code since #28; `slotfather-dashboard` is no longer installed, and the installer removes an earlier one.

**`slotfather capabilities --json`** says which contracts this release supports; a feature it does not name is
not there:

```json
{"v": 1, "tool": "slotfather", "version": "0.1.0",
 "dashboard": {"v": 1, "tag": "slot",
   "snapshot": {"argv": ["status", "--json", "--full"], "versions": [2], "poll": 1},
   "events": {"path": "/Users/me/.local/state/slotfather/events.jsonl", "versions": [1]},
   "settings": {"versions": [1]},
   "actions": ["reprioritize", "ask-yield"],
   "changelog": "/Users/me/.local/lib/slotfather/releases/<release>/slotfather/CHANGELOG.md"}}
```

`argv` runs after the `slotfather` executable; `versions` are the `status --json` versions it prints; `poll` is a
hint in seconds; `changelog` is the CHANGELOG.md shipped with the running package (absent when there is none).

**`slotfather status --json --full`** is `status --json` (version 2, every field as above, nothing added to an
entry) plus three top-level fields: `host` (the short host name, lowercase), `time` (RFC 3339 UTC with
milliseconds) and `dashboard`, the dashboard's model (`board.dashboard_model()`) as JSON. The status and
the block come from one look at the queue under its lock (`query.look()`), so each entry's `yield_asked` and
`idle_since` agree with its row's `asked` and `idle_since`; like `status`, it may reap dead entries and changes
nothing else. The block:

- every datetime is an RFC 3339 UTC string with milliseconds or `null`; tuples are arrays;
- `v` (1), `directory`, `slots`, `cores`, `saved`, `basis` (the capacity basis as `slotfather slots` reads it),
  `averages` (`[l1, l5, l15]`), `draining`, `advice` (as `slotfather advise` computes it), `holders` and `waiters`
  (rows, in the order the dashboard shows them), `used`, `free`, `memory_block` (why new leases wait for memory,
  or `null`), `memory_budget_mb` and `memory_expected_mb` (expected-peak memory budget and held leases' expected
  peak RSS in MB, fathers#9), `admission`, `order`, `start` and `reserved` (the head's expected and reserved start),
  `limit`,
  `memory` (the memory facts, plus `low`: `true`/`false`, `null` when unknown; `swap_low`: free swap under
  SLOTFATHER_MIN_FREE_SWAP_MB; and `psi_low`: on Linux, PSI memory some or full at or over SLOTFATHER_MAX_PSI_SOME /
  _FULL, so the mem row shows `psi N%`, always `false` on macOS, whose `level` passes through), `history`
  (`[[start, end, slots]]`, the occupancy steps of the last two hours), `events` (the last 60 described events
  of queue.log, oldest first; transitional, removed in the first release after a Godfather that reads
  `events.jsonl` is installed), `updated`, `overtake_window` (seconds) and `unknown` (the queue entries this
  Slotfather does not know, each `{"id", "lease", "label", "pid", "state", "count", "reason", "holds", "warning"}`,
  `holds` the slots it reserves while its holder runs, which `used` includes; see
  Unknown entries below);
- a row: `key` (`[pid, since]`, `since` as stored), `kind` (`held`, `legacy`, `waiting`), `count`, `pid`, `procs`,
  `cpu` (percent), `rss` (KB), `tree` (`[[pid, cpu, rss_kb, name]]`, highest CPU first), `queue` (an earlier
  queue's directory), `label`, `cmd`, `meta`, `workdir`, `context` (the metadata), `priority`, `ticket`,
  `served` (the class it is served in), `reprioritized`, `yieldable`, `yields`, `asked` (the label of the waiter
  that asked it to yield, `""` when none) and `asked_at`, `idle_since`, `est_start`, `since`, `granted`,
  `passable`, `all`, `est_memory_mb`, `est_memory_source`, `lock` (lock path when waiting on `slotfather lock`,
  `""` otherwise). Strings have their control characters blanked;
- an event: `when`, `verb` (`granted`, `released`, `left`, `reaped`, `priority`, `waiting`), `count`, `pid`,
  `delta` (the change to held slots), `label`, `detail`, and `state` or `unsure` where queue.log alone cannot say.

Presentation stays with the reader: holder colours, the lane of a request (Lanefather) and a holder's current
working directory (lsof) are not in the block; a row's `workdir` is the directory the request was made in.

**`slotfather settings`** reads and changes the shared settings (capacity.json and order.json) with the same
validation, ownership check and queue lock as `slotfather slots` and `slotfather order`; the in-tree Settings panel
takes its labels, checks and previews from the same module:

```text
slotfather settings --json                                the settings and the queue's STATUS rows
slotfather settings check KEY VALUE --json                validate without saving
slotfather settings set KEY VALUE [--revision R] --json   save
slotfather settings reset (KEY | --all) [--revision R] --json
```

```json
{"v": 1, "tool": "slotfather", "writable": true, "reason": null,
 "status": [{"label": "queue", "value": "~/.local/state/slotfather", "style": "text"},
            {"label": "slots", "value": "4 total · 3 free · 1 held · 0 waiting", "style": "text"}, ...],
 "basis": {"mode": "automatic", "slots": 4, "cpu_slots": 4, "cores": 12, "memory_slots": 8, "memory_gib": 32.0,
           "gib_per_slot": 4.0, "saved": false, "cgroup": null},
 "sections": [
   {"id": "capacity", "title": "Capacity", "settings": [
     {"key": "capacity.mode", "label": "Capacity", "type": "choice", "value": "automatic", "default": "automatic",
      "choices": [{"value": "automatic", "label": "Automatic"}, ...], "source": "default", "writable": true,
      "revision": "absent", "description": "..."},
     {"key": "capacity.slots", "label": "Slots", "type": "integer", "value": 4, "default": null, "min": 1,
      "max": 48, "step": 1, "unit": "slots", "when": {"capacity.mode": ["fixed"]}, ...},
     {"key": "capacity.gib_per_slot", "label": "GiB per slot", "type": "number", "value": 4.0, "default": 4.0,
      "min": 0, "min_exclusive": true, "max": null, "step": 0.5, "unit": "GiB", "when": {"capacity.mode": ["memory", "lower"]}, ...}]},
   {"id": "order", "title": "Queue order", "settings": [
     {"key": "order.order", "label": "Queue order", "type": "choice", "value": "backfill", ...},
     {"key": "order.warn_wait", "label": "Warn after waiting", "type": "duration", "value": 1800.0,
      "default": 1800.0, "min": 0.0, "max": null, "step": 300, "unit": "s", "zero": "never",
      "effective": 900.0, "source": "env SLOTFATHER_WARN_WAIT", "writable": true, "revision": "sha256:...",
      "description": "..."}, ...]}]}
```

- Keys, in the panel's order: `capacity.mode` (`automatic`, `fixed`, `memory`, `lower`), `capacity.slots` (saves
  Fixed with that count), `capacity.gib_per_slot` (saves the current memory mode with that amount; refused in
  another mode), `order.order` (`backfill`, `window`) and `order.KNOB` for `safety`, `fallback`, `overrun`,
  `limit`, `interactive`, `batch`, `aging_batch`, `aging_normal`, `warn_wait`, `reservation_timeout`, `idle_cpu`,
  `idle_after`, `yield_after`. `when` says under which capacity mode a row is shown. A mode set alone keeps the current
  slots (Fixed) or GiB per slot (the automatic 4 GiB when there is none).
- `value` is the saved value, the environment ignored; `effective`, present only when this environment tightens it,
  is the value that applies here, and `source` then names the variable (else `saved` or `default`). Durations are
  seconds. `set` and `check` also take `90s`, `30m`, `1.5h` or `2m30s`. `zero` names what 0 means.
  `order.reservation_timeout` measures how long a blocked head waits past its reserved start before it gives way.
  Zero disables the timeout. A saved setting unavailable to older clients adds a `warning` to its row and a
  `compatibility` row to `status`, explaining that those clients may use Arrival order.
- `basis` is the capacity basis with the memory budget filled in by the automatic rule where Fixed does not
  work it out, as STATUS' budgets row shows it.
- `revision` is opaque: one token for the three capacity keys (capacity.json), one for the order keys
  (order.json). With `--revision` a `set` or `reset` saves only while that file is unchanged (`reset --all
  --revision R` checks capacity.json's); without it, order settings merge with whatever else is saved.
  `min_exclusive: true` means the value must be above `min`.
- `check` replies `{"ok", "error", "preview"}`, and `slots` for a capacity key: the capacity the value gives, with
  a preview such as `→ 6 slots · will start 1 waiting` or `→ 2 slots · will drain 1: running work keeps its slots`.
  Errors use the panel's words (`Enter a whole number of slots, at least 1.`, `At most 48 slots (4 per core).`).
- `set` and `reset` reply with the whole settings object plus `ok: true` and `message` (the panel's confirmation,
  `Saved · capacity 6 slots`). `reset --all` resets both files.
- Exit codes: 0 done; 2 invalid, with `{"ok": false, "error"}`; 3 a stale revision, with the whole current
  settings object, `ok: false`, `conflict: true` and `error`; 4 another user's queue, with `{"ok": false,
  "error"}` (`settings --json` there has `writable: false`, its `reason`, and every row `writable: false`).

**`events.jsonl`** (beside the queue; `capabilities` gives its absolute path under `dashboard.events`) holds the
queue's events in Godfather's shared event schema (version 1, validated by `common/eventcheck.py`). Each site that
writes a `queue.log` line also appends its event after the change; event write failures never fail or alter an
operation, and nothing is written into another user's queue.

- **Format & rotation**: one JSON object per line, appended with one `O_APPEND` write (mode `0600`), no control
  characters, max 16 KiB (larger events keep `details: {"truncated": true}`, or are skipped if still too large). Past
  4 MiB it rotates to `events.jsonl.1` under an `flock` on `events.lock`.
- `v`, `time`, `host`, `source`: schema version (`1`), RFC 3339 UTC timestamp with milliseconds, host name, and
  `"slotfather"`.
- `kind`, `details`, `level`: event kind (table below), kind-specific payload, and optional `level` (e.g. `"warn"`).
- `subject`: the request (`type`: `"request"`, `id` matching `status --json`, `lease_id`, `label`, `pid`) unless noted
  otherwise in the table below.
- `actor`: holder process (`pid`, with `lane` from metadata) for client actions, `type`: `"tool"` (`name`:
  `"slotfather"`) for queue-look decisions, or the command's process otherwise (`via`: `"godfather"` when
  `GODFATHER_ACTOR` names Godfather).

| Kind | When | `details` |
| --- | --- | --- |
| `lease.granted` | a request is granted, or a yielded lease again | `count`, `min_count`, `max_count`, `priority`, `waited`, `served` (when ageing moved it), `resumed`, `meta`, `session` |
| `lease.released` | a lease ends | `count`, `exit`, `ran`, `cpu`, `rss_peak_mb`, `unknown` (only `count` and `unknown: true` when its entry was already gone) |
| `request.left` | a waiter gives up (`--wait`, a signal) | `count`, `gave_up_after`, `reason` |
| `lease.reaped`, `request.reaped` | a holder that died is reaped (level warn) | `count`, `ran`, `waited`, `reason` |
| `reap.skipped` | ps gave no process table to trust (it failed, timed out, or left out its caller), so a look that changes the queue reaped nothing; once per failure streak of a client (subject `queue`, level warn) | `reason` |
| `reap.resumed` | ps answers again after that client's `reap.skipped` (subject `queue`) | `after` (the reason skipped) |
| `request.reprioritized` | `slotfather reprioritize` | `from`, `to`, `state` |
| `request.waiting_long` | a waiter says it has waited the warn time (level warn) | `waited`, `est_start`, `behind` (`earlier`, `running`), `priority`, `aged` |
| `lease.yield_asked` | a waiter, or `ask-yield`, asks a lease to yield | `count`, `for` (`id`, `label`, `priority`), `by_hand` |
| `lease.yield_withdrawn` | that request is withdrawn | — |
| `lease.yielded` | the lease yields; for a lock (fathers#159), `to` is `{"id": null, "label": "lock PATH"}` | `count`, `to` (`id`, `label`), `yields`, `lock` (for a lock) |
| `lease.lock_ended` | a lease's wait for a lock ends (fathers#159; level warn when its waiter is gone) | `lock`, `why` (`took`, `stopped`, `resumed`, `gone`), `yielded`, `waited` |
| `request.head_dropped` | a head blocked by leases with no end estimate gives way, `late` seconds past its reserved start (fathers#160; actor `tool`, level warn) | `count`, `late`, `blocked_by` (`id`, `label`, `pid`) |
| `capacity.changed` | a capacity is saved or reset (a save that changes nothing is no event, as for order) (subject `capacity`) | `saved` (capacity.json, `null` for reset), `mode`, `slots` |
| `order.changed` | order settings are saved or reset (subject `order`) | `saved` (the keys the change named), `reset` (`true` on reset) |
| `process.swept` | `sweep --live` stops a leftover process (subject `process`, level warn) | `action`, `class`, `kind`, `age`, `processes`, `cmd` |
| `session.run_started`, `session.run_ended` | a `run --session` starts or ends inside a session (subject the session's request) | `session`, `run` (`pid`, `label`, `cmd`; at the end `exit`, `ran`, `cpu`, and `meta` and `git` where it has them) |
| `run.outlier` | a run above its run type's p90 is settled as an outlier (subject `run_type`, `id` the key; level warn) | `lease_id`, `ran`, `p50`, `p90`, `ended`, `later` |
| `run.trend` | a run type's trend changes (subject `run_type`; level warn when slower) | `verdict`, `p50_recent`, `p50_before`, `ratio`, `recent`, `before`, `plan_change`, `why` |
| `session.ended` | a session releases its lease ([sessions](sessions.md)) | `reason` (`idle`, `stopped`), `runs`, `busy`, `held` |

**Run types** (fathers#106, for agents). A run type is keyed by `(project, phase, plan)` when a request carries a
`phase` meta (Lanefather passes `phase`, `plan`, and `checks`), or by `(label, kind)` otherwise.

- `expected` (on `status --json` and `--full`, not `--pid`): `{"key", "p50", "p90", "runs", "fallback"}` from the last
  20 released runs of the most specific key with at least 3 runs (`project+phase+plan`, `project+phase`, `label+kind`,
  `label`), or `null`.
- `overrun` (held entries): `true` once a running lease passes its `p90` by more than 10% and 2 s.
- `run_key` / `verdict` (on released `history.jsonl` lines): the matched key and initial verdict (`"normal"`, or
  `"pending"` above `p90`).
- `verdicts.jsonl`: once 3 later runs of the key land, a `"pending"` run settles to `"outlier"` (most later runs
  normal, emitting `run.outlier`) or `"slower"`; recorded as `{"lease_id", "verdict", "key", "ran", "at"}` without
  rewriting `history.jsonl`.
- Trends (`slotfather stats --trend [--project P] [--phase X] [--since WHEN] --json`): compares the median of the last
  10 runs against the 30 before (`"slower"`/`"faster"` beyond 1.25×, else `"steady"`; changes emit `run.trend`).
  Returns `{"v": 1, "keys": [...]}` with per-key `project`, `phase`, `plan`, `checks`, `runs`, weekly `weeks`
  (`week`, `count`, `p50`, `p90`), `plan_changes`, `trend` (including `why`: `plan grew: +N checks`, `plan shrank: ...`,
  `plan changed`, `same plan`, or `no plan recorded`), and `verdicts`. Without `--json`, prints one line per key.

**Changelog** (fathers#103, fathers#119): `slotfather changelog --json [--since VERSION]` prints the installed
release's CHANGELOG.md as `{"v": 1, "tool": "slotfather", "version", "source": its resolved path or null, "entries":
[{"version", "date", "section", "text"}]}`, one entry per `- ` bullet under a `### ` section in file order with
indented continuation lines joined, newest first ([contract](../../godfather/docs/contracts/changelog.md)). Installs
come from main, so the `## Unreleased` entries are what is installed: they come first, with `version` and `date` null,
`unreleased: true` and `source` set to the installed commit (`null` from a checkout); dated release entries omit
`unreleased` and `source`. `--since VERSION` keeps releases newer than VERSION, and Unreleased. It reads the changelog
and the release's `version.json`, nothing else.

**Dashboard actions** (`capabilities.dashboard.actions`): `reprioritize` and `ask-yield`, each identity-checked
with the start time the caller saw (`status --json`'s `start`):

- `slotfather reprioritize PID CLASS --start START` refuses (exit 2) when PID is now another process; `--start`
  takes a pid, not a label.
- `slotfather ask-yield PID --start START` asks the yieldable lease that PID holds to yield for the first waiter, in
  the queue's order, of a higher class that its slots would start. It writes the marker an interactive waiter
  writes (`SLOTFATHER_YIELD`), with `asker` recorded; the request stands while that waiter waits, `status` shows
  `asked-to-yield-for=`, and the holder yields at its next check (`slotfather yield`). Nothing stops a holder. It
  refuses a pid whose start differs, a pid that holds no lease, a lease that did not take `--yieldable` or has
  yielded `YIELDS_MAX` times, and a queue with no such waiter; asked twice, it says it was already asked.

## Waiting on a lock

`slotfather lock PATH -- COMMAND...` (fathers#159) runs COMMAND holding an exclusive `flock` on PATH (created if
missing), released when it exits; it exits with COMMAND's status and runs it as `slotfather run` runs its command
(SIGTERM and SIGHUP passed on, 128 + the signal that ended it). Outside a lease it is a plain lock. When another
process holds the lock, the lease the caller runs under (`SLOTFATHER_LEASE`, or a holder among its ancestors):

- **gives up its slots** while it waits, when nothing else runs under it (no process in its holder's tree but the
  waiter and its ancestors; Slotfather's own `ps` and `lsof` aside): the entry goes back among the waiters in its
  own place, keeping its rank and its ageing (`lock_yield`), as a yielded lease (`yielded_at`, `first_granted`)
  with `lock: PATH` and `lock_owner: {"pid", "start"}`, the waiting process; nothing grants it while the mark
  stands, and a lock wait spends none of the lease's polite yields (`yields`, `YIELDS_MAX`);
- **keeps its slots** otherwise, since other work (a parallel step, a container) runs on them, and only says what it
  waits for: `lock` and `lock_owner` on the held entry.

Once the lock is taken the mark goes and COMMAND runs only after the lease is held again. A signal that ends the wait
clears the mark too, and the waiter still waits until the lease is held before it exits, so whatever runs next under
the lease runs on its slots. A waiter that dies without clearing its mark (SIGKILL) leaves a mark the next look
clears (its `lock_owner` is not running), and the lease is granted again at the next look that grants. Work that
starts under a lease that gave up its slots for a lock (a nested `slotfather run`, or `slotfather lock` on a free
lock) takes them back: the mark goes and the work waits for the grant, as under any yielded lease; so a lease's own
tree never waits on its own mark. `status` tags such an entry `waiting-for-lock=PATH`, `status --json` gives it
`lock` (only on such an entry), the dashboard block's items carry `lock` (empty when none), queue.log says
`yielded N (ENTRY) while waiting for lock PATH` and how the wait ended, and EVENTS gets a `lease.yielded` with
`details.lock` and `to: {"id": null, "label": "lock PATH"}`, and `lease.lock_ended` with `lock`, `why` (`took`,
`stopped`, `resumed`, `gone`), `yielded` and `waited` (seconds, where it yielded). A grant clears a stale mark. An
older client reads the entry as a yielded waiter and may grant it during the wait, which only holds the slots as
before. Slotfather never yields a lease by itself for a lock it sees: a guess would free slots under running work.

While a process in a lease's tree runs a container or a virtual machine (`docker`/`podman`/`nerdctl`/`container`
with `run`, `exec`, `start`, `attach`, `up`, `build`; `docker-compose up`; `qemu-system-*`, `vfkit`, `firecracker`
and the like: `idle.container()`, by its command line), the lease is never idle, though its work uses no host CPU.
A tool of that family that runs none (`docker ps`, `kubectl port-forward`, `limactl list`) is no such work.

## Process queries

Use `procs` for process ownership checks and worktree inspection:

```sh
slotfather procs --json --pid 4242
slotfather procs --json --cwd /path/to/worktree
```

Unlike `status`, which can reap abandoned leases while reporting the queue,
`procs` is strictly read-only. It never creates state, takes the queue lock,
reaps an entry or appends history. Unknown future state schemas and failed
process-table reads produce an error, not an empty ownership answer.

The versioned response has these fields:

| Field | Meaning |
| --- | --- |
| `v` | Response schema version, currently `2`. |
| `query` | Requested `pid` and canonical `cwd`; `start` identifies the requested PID, or is `null` when it is not live. |
| `processes` | Matching live process records, in PID order. |
| `cwd_unknown` | Own-user PIDs whose working directories could not be read as local paths. Relevant when `--cwd` is requested. |
| `lease_unknown` | PIDs in no live lease's tree whose environment could not be read, so whether they carry a lease is unknown. |
| `unknown` | One record per PID in either list: `pid`, `start`, `cmd`, and `cwd_unknown` and `lease_unknown` (booleans: which list it is in), so a consumer can name what holds it up. Added in fathers#158. |

A PID enters `cwd_unknown` or `lease_unknown` only if a second, targeted `ps` finds it still running with the start
time the scan saw: a short-lived helper (`lsof`, git, a shell) that exited between the scan and the read is gone, not
unknown. A re-check that `ps` does not answer fails the query.

Each process contains `pid`, `ppid`, `uid`, `tty`, `start`, `cmd`, `cwd`,
`ancestors`, `sessions` and `leases`. `start` is the C-locale `ps lstart`
identity with whitespace normalized. Zombies are excluded. `ancestors` lists
parents nearest first, without PID 1. `sessions` contains the process itself
and ancestors recognized as terminal, agent, remote-login or CI sessions;
each record contains `pid`, `start` and `cmd`. This is observed ancestry,
not an assertion that an agent owns another process.

A `leases` record contains `id`, `lease` (path), `state`, `count`, `label`,
`pid` (holder), `start` (holder identity), `priority`, `ticket`, `meta`, `legacy` and `via`.
Priority is `interactive`, `normal` or `batch`. `via` is `tree`
for the holder or its descendants, or `env` for a detached process carrying
that exact `SLOTFATHER_LEASE`. Stale holder identities never match. Legacy
counts use that older queue's capacity units. No environment contents are
returned.

`--pid` selects exactly that PID, not its descendants. It reads one process
table and skips the working-directory scan; `cwd` in its process record is
therefore `null` unless `--cwd` is also supplied. Without either filter all
live processes are reported, also without a working-directory scan.

On macOS, exact-PID queries use standard-library `ctypes` with libproc for
process identities, then one targeted `ps` read for the requested process's
and its ancestors' full arguments. That command never requests environments.
The adapters' identities must agree. Unsupported native interfaces or an
unavailable required identity fall back to the portable process-table adapter.

`--cwd` selects this user's processes whose canonical working directory is
the root or a descendant directory: `/work/lane-other` does not match
`/work/lane`. The query process and its ancestors are omitted. Both filters
may be combined and intersect. Linux processes in a different or unreadable
mount namespace are listed in `cwd_unknown`, since their pathnames cannot
be interpreted safely in the caller's filesystem; non-dumpable long-lived
managed services (`systemd --user`, `(sd-pam)`, and systemd `.service`
`MainPID`s) whose `/proc` links answer `EACCES` are omitted (fathers#164),
while any non-dumpable process started in or outside a service's `MainPID`
remains in `cwd_unknown`.

Consumers that decide whether to signal a process or remove a worktree must
fail closed on query errors and relevant `cwd_unknown` or `lease_unknown`
entries. An empty `leases` list is only the membership visible to this
process: macOS and Linux permissions can hide detached processes'
environments. A negative macOS `ps -wwE` observation alone proves nothing, since
ps prints no environment it may not show: there the kernel's copy (sysctl
`KERN_PROCARGS2`) proves the absence of the marker, and a process it hides
(another user's, an Apple platform binary, whose environment it gives empty)
remains in `lease_unknown`. Readable Linux `/proc` bytes likewise prove the
absence of that marker. It is not permission to stop a process. Recheck `(pid, start)`
immediately before any action, because PIDs can be reused after the query.
