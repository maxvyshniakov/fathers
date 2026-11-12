# State, metadata and JSON status

The separate [lifetime lane-admission query](lane-admission.md) is stateless;
it does not use execution job budgets or reserve slots.

[Domain definitions](../GLOSSARY.md) · [Compatibility aliases](terminology.md)

State lives in `~/.local/state/slotfather/`; `queue.log` records grants, releases, and reaping. `history.jsonl` has one JSON line per request that left the queue: label, command, directory, slots, queue, grant and end times, seconds waited and run, the outcome (`released`, `left` after giving up, or `reaped`), `until` for a request with `--wait` (when it would give up) and, for `run`, the exit status and what the command used (see Statistics). Past 4 MB it becomes `history.jsonl.1`, replacing the previous one, so at most two files' worth is kept; readers take `.1` first. `history [N]` lists the last N (default 20). `status` lists holders and waiters.

**Request fields.** `run --priority interactive|normal|batch` (library:
`acquire(..., priority="interactive")`) sets scheduling priority; omitted priority
is `normal`. `--ticket ID` / `ticket=...` sets an optional issue identifier.
Both are top-level queue and history fields and appear in JSON status/process
responses (`ticket` is null when absent in query responses). Inherited leases
keep the outer request's fields. Built-in fields cannot be passed through metadata;
`--class`, `meta.class`, `meta.priority` and `meta.ticket` are rejected.

**Lease metadata.** `run --meta key=value` (repeatable; the library's `acquire(..., meta={...})`) adds caller context: `lane`, `phase`, `model`, a `session`, and custom tags. Keys are lowercase words of at most 32 characters (`a-z`, digits, `_ . -`), values printable text of at most 200, at most 16 keys; a later key replaces an earlier one. The metadata is stored on the queue entry and in the request's history line (`"meta": {...}`), and shown by `status` and `history` (`meta=lane=lane-650,phase=check` before `cmd=`), in the dashboard's details panel, and grouped by `stats --by meta.KEY`. A nested `run` that inherits a lease runs under that lease's metadata and adds none of its own. Priority and ticket use dedicated request fields rather than metadata.

**`status --json`** is the queue for programs, such as a guard that must not kill another holder's lease: `{"v": 2, "slots", "held", "free", "draining", "entries": [...], "legacy": [...]}` (`draining`: slots held above a capacity that shrank under running leases, 0 otherwise; older readers ignore it), each entry `{"id", "lease", "state", "count", "label", "pid", "pgid", "start", "since", "granted", "cmd", "cwd", "meta", "tree", "priority", "ticket", "yieldable", "yields", "yield_asked", "position", "idle_since", "effective_priority", "aged", "reprioritized", "est_start", "head", "reserved", "waited", "long_wait"}` (yieldable to position: see Polite preemption; `idle_since`: since when a held lease has been idle, epoch seconds, or `null`; see Idle leases in scheduling; `effective_priority` to `long_wait`: the class it is served in and whether ageing moved it there, the class it asked before `slotfather reprioritize` or `null`, and for a waiter its expected start, whether it is the head and its reserved start (UTC stamps or `null`), how long it has waited in seconds and whether that is past the warn time; see Ageing in scheduling). `id` is the entry's name, `lease` its path (the value of `SLOTFATHER_LEASE` in the lease's command), `pid` the holder (`slotfather run`, or the process that called `acquire()`), `pgid` its process group, `tree` the holder and its live descendants. `status --json --pid PID` keeps only the entries PID belongs to, each with `via`: `"tree"` (the holder or one of its descendants) or `"env"` (it left the tree, by a double fork or `setsid` and launchd adopting it, but its environment carries the lease's `SLOTFATHER_LEASE`; macOS shows the environment only of the user's own processes that are not Apple platform binaries), and adds `"query": {"pid", "start"}`, the process's start time as `ps -o lstart=` prints it (`null`: not running), so a guard can check that the pid was not reused before it acts. An empty list means the process belongs to no lease of this queue as far as can be seen. The extracted version also reads leases in Straylight's earlier queue during migration; old clients do not see new leases, so retire old wrappers before relying on mutual isolation.

## Dashboard contracts

[Godfather](https://github.com/maxvyshniakov/godfather), the shared terminal dashboard, reads Slotfather only through
these commands; it never imports Slotfather or reads its state files. Each reply carries its own version, and a
reader treats a version it does not know as unsupported.

**`slotfather dashboard [ARGS...]`**, and `slotfather` alone with a terminal on stdin and stdout, replace their
process with `godfather ARGS...` (found on `PATH`): the arguments pass through untouched and the exit code is
Godfather's. Without an executable `godfather` on `PATH` they print `slotfather: the dashboard is part of godfather
now; install it from https://github.com/maxvyshniakov/godfather` on stderr and exit 2. Slotfather itself carries no
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
  or `null`), `admission`, `order`, `start` and `reserved` (the head's expected and reserved start), `limit`,
  `memory` (the memory facts, plus `low`: `true`/`false`, `null` when unknown; `swap_low`: free swap under
  SLOTFATHER_MIN_FREE_SWAP_MB; and `psi_low`: on Linux, PSI memory some or full at or over SLOTFATHER_MAX_PSI_SOME /
  _FULL, so the mem row shows `psi N%`, always `false` on macOS, whose `level` passes through), `history`
  (`[[start, end, slots]]`, the occupancy steps of the last two hours), `events` (the last 60 described events
  of queue.log, oldest first; transitional, removed in the first release after a Godfather that reads
  `events.jsonl` is installed), `updated` and `overtake_window` (seconds);
- a row: `key` (`[pid, since]`, `since` as stored), `kind` (`held`, `legacy`, `waiting`), `count`, `pid`, `procs`,
  `cpu` (percent), `rss` (KB), `tree` (`[[pid, cpu, rss_kb, name]]`, highest CPU first), `queue` (an earlier
  queue's directory), `label`, `cmd`, `meta`, `workdir`, `context` (the metadata), `priority`, `ticket`,
  `served` (the class it is served in), `reprioritized`, `yieldable`, `yields`, `asked` (the label of the waiter
  that asked it to yield, `""` when none) and `asked_at`, `idle_since`, `est_start`, `since`, `granted`,
  `passable`, `all`. Strings have their control characters blanked;
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
  `limit`, `interactive`, `batch`, `aging_batch`, `aging_normal`, `warn_wait`, `idle_cpu`, `idle_after`,
  `yield_after`. `when` says under which capacity mode a row is shown. A mode set alone keeps the current
  slots (Fixed) or GiB per slot (the automatic 4 GiB when there is none).
- `value` is the saved value, the environment ignored; `effective`, present only when this environment tightens it,
  is the value that applies here, and `source` then names the variable (else `saved` or `default`). Durations are
  seconds; `set` and `check` also take `90s`, `30m`, `1.5h`. `zero` names what 0 means.
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

**`events.jsonl`** (beside the queue; `capabilities` gives its absolute path under `dashboard.events`) holds
the queue's events in Godfather's shared event schema, version 1: one JSON object per line with `v`, `time` (RFC 3339
UTC with milliseconds), `host`, `source` (`slotfather`), `kind`, `subject`, `actor`, `details` and, for a warning,
`level`. Each site that writes a queue.log line also appends its event, after the change; queue.log and
history.jsonl are unchanged, and an event that cannot be written never fails or alters the operation. Lines are
appended with one write (O_APPEND, mode 0600), carry no control characters, and stay under 16 KiB (a larger
event keeps `details: {"truncated": true}`, and one still too large is not written); past 4 MiB the log becomes `events.jsonl.1` under an flock on
`events.lock`. Nothing is written into another user's queue: a process of another user (root through a guest
agent) that changes that queue leaves its queue.log line without an event. `subject` is the request (`type: request`, `id` its
entry's name as `status --json` gives it, `label`, `pid`) unless said otherwise; `actor` is the holder process
(with `lane` from its metadata) for what a lease's own client does, `type: tool` (`name: slotfather`) for what a
look at the queue decides, the command's process otherwise, with `via: godfather` when `GODFATHER_ACTOR` names
Godfather. The tests validate every line with Godfather's `eventcheck.py`, vendored in `tests/vendor/` with its
SHA-256 (compared with `$GODFATHER_DIR/src/godfather/eventcheck.py` when that is set).

| Kind | When | `details` |
| --- | --- | --- |
| `lease.granted` | a request is granted, or a yielded lease again (`resumed`) | `count`, `priority`, `waited`, `served` when ageing moved it, `meta` |
| `lease.released` | a lease ends | `count`, `exit`, `ran`, `cpu`, `rss_peak_mb`; only `count` and `unknown: true` when its entry was already gone |
| `request.left` | a waiter gives up (`--wait`, a signal) | `count`, `gave_up_after` |
| `lease.reaped`, `request.reaped` | a holder that died is reaped (level warn) | `count`, `ran`, `waited`, `reason` |
| `request.reprioritized` | `slotfather reprioritize` | `from`, `to`, `state` |
| `request.waiting_long` | a waiter says it has waited the warn time (level warn) | `waited`, `est_start`, `behind` (`earlier`, `running`), `priority`, `aged` |
| `lease.yield_asked` | a waiter, or `ask-yield` (`by_hand`), asks a lease to yield | `count`, `for` (`id`, `label`, `priority`) |
| `lease.yield_withdrawn` | that request is withdrawn | — |
| `lease.yielded` | the lease yields | `count`, `to` (`id`, `label`), `yields` |
| `capacity.changed` | a capacity is saved or reset (a save that changes nothing is no event, as for order) (subject `capacity`) | `saved` (capacity.json, `null` for reset), `mode`, `slots` |
| `order.changed` | order settings are saved or reset (subject `order`) | `saved` (the keys the change named) or `reset: true` |
| `process.swept` | `sweep --live` stops a leftover process (subject `process`, level warn) | `action`, `class`, `kind`, `age`, `processes`, `cmd` |

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
| `lease_unknown` | PIDs for which the detached-process environment check could not be completed. |

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
be interpreted safely in the caller's filesystem.

Consumers that decide whether to signal a process or remove a worktree must
fail closed on query errors and relevant `cwd_unknown` or `lease_unknown`
entries. An empty `leases` list is only the membership visible to this
process: macOS and Linux permissions can hide detached processes'
environments. A negative macOS `ps -wwE` observation therefore remains in
`lease_unknown`, even when ps printed the process's command. Only a positive
lease marker proves detached membership there; readable Linux `/proc` bytes
can also prove the absence of that marker. It is not permission to stop a process. Recheck `(pid, start)`
immediately before any action, because PIDs can be reused after the query.
