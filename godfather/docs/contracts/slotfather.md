# What Godfather reads from Slotfather

Godfather 0.1.0 renders today's Slotfather dashboard from three Slotfather commands and nothing else: it never
imports a Slotfather module and never reads a file in Slotfather's state directory. This page is the exact shape
Godfather's adapter (`src/godfather/adapters/slotfather.py`) decodes. Slotfather owns the contract
(slotfather#26 SF-1, slotfather#27 SF-2); where this page and Slotfather's own API docs disagree, Slotfather's
docs win and this page and the adapter follow.

## `slotfather capabilities --json`

```json
{"v": 1, "tool": "slotfather", "version": "0.2.0",
 "dashboard": {"v": 1, "tag": "slot",
   "snapshot": {"argv": ["status", "--json", "--full"], "versions": [2], "poll": 1},
   "settings": {"versions": [1]},
   "actions": ["reprioritize"],
   "changelog": "/Users/me/.local/lib/slotfather/releases/<release>/CHANGELOG.md"}}
```

Godfather needs `dashboard.v == 1`, a snapshot whose `versions` include 2 and whose dashboard block `v` is 1.
Missing `settings` hides Settings' Slotfather rows (the panel then shows only Refresh every and Colour theme).

## `slotfather status --json --full`

Today's `status --json` (v 2) with three more top-level fields: `host` (short, lowercase), `time` (RFC 3339 UTC)
and `dashboard`, which is the in-tree `dashboard.dashboard_model()` dict serialised with these rules:

- every `datetime` is an RFC 3339 string in UTC (`2026-11-12T04:25:56.204Z`) or `null`;
- tuples become arrays;
- nothing is added or renamed, except the four policy facts below that the renderer used to read from
  Slotfather's config.

```text
dashboard:
  v                  1
  directory          str          the state directory (STATUS "queue" row, tilde-shortened by Godfather)
  slots, cores       int
  saved              bool
  basis              object       config.capacity_basis() as is (mode, slots, gib_per_slot, memory_slots,
                                  memory_gib, cpu_slots, cores, saved, ...)
  averages           [l1, l5, l15]
  draining           int
  advice             object       query.size(...) as is (unleased_load, jobs, ...)
  holders, waiters   [item]       in the order the dashboard shows them
  used, free         int
  memory_block       str | null
  admission          int | null
  order              "backfill" | "window"
  start, reserved    time | null
  limit              number
  memory             object       memory_facts() as is, plus:
                                    low       bool | null   machine_memory_status() says "memory low"
                                                            (null: unknown)
                                    swap_low  bool          free swap under SLOTFATHER_MIN_FREE_SWAP_MB
  history            [[start, end, slots]]   held_history() steps
  events             [event]      the last 60 described events, oldest first (transitional, until slice 3)
  updated            time
  overtake_window    number       config.overtake_window(), seconds

item:
  key                [pid, since]  since as Slotfather stored it (the raw string)
  kind               "held" | "legacy" | "waiting"
  count, pid, procs, yields       int
  cpu                number        percent
  rss                int           KB
  tree               [[pid, cpu, rss_kb, name]]  sorted by cpu, highest first
  queue, label, cmd, meta, workdir, asked          str (already dashboard_text()-cleaned)
  context            object        config.meta_of(entry)
  priority           str           config.priority_of(entry)
  ticket             str | null    config.ticket_of(entry)
  served, reprioritized            str | null
  yieldable, passable, all         bool
  since, granted, asked_at, idle_since, est_start   time | null

event:
  when               time
  verb               "granted" | "released" | "left" | "reaped" | "priority" | "waiting"
  count, pid, delta  int
  label, detail      str
  state, unsure      optional, as queue_events() sets them
```

Godfather fills in what is presentation: the holder colours (`style`), the lane facts for the selected request
(`lanefather show ID --state-dir DIR --json`, in the background loader as today) and the holder's working
directory (`lsof -a -p PID -d cwd -Fn`, or `/proc/PID/cwd`).

## `slotfather settings`

```text
slotfather settings --json
slotfather settings check KEY VALUE --json
slotfather settings set KEY VALUE [--revision R] --json
slotfather settings reset (KEY | --all) [--revision R] --json
```

Keys Godfather 0.1.0 edits, and the panel row each is shown as:

| Key | Row | Type |
| --- | --- | --- |
| `capacity.mode` | Capacity | choice: `automatic`, `fixed`, `memory`, `lower` |
| `capacity.slots` | Slots (mode `fixed`) | integer, at least 1, at most 4 per core |
| `capacity.gib_per_slot` | GiB per slot (modes `memory`, `lower`) | number, more than 0 |
| `order.order` | Queue order | choice: `backfill`, `window` |
| `order.safety`, `order.fallback`, `order.overrun`, `order.limit`, `order.interactive`, `order.batch`, `order.aging_batch`, `order.aging_normal`, `order.warn_wait`, `order.idle_cpu`, `order.idle_after`, `order.yield_after` | the knob rows | number or duration (seconds) |

`settings --json` replies with SPEC.md's settings object. Godfather reads, per setting: `key`, `value` (the saved
value, the environment ignored), `effective` (the value here when the environment tightens it, else absent or
equal), `default`, `writable`, `source` and `revision`; and at the top level `writable`, `reason`, `status` (rows
of `label`, `value` and an optional `style`), plus `basis`, `config.capacity_basis()` as is, with `memory_slots`
and `memory_gib` filled in by the automatic rule when the saved mode does not compute them (today's panel does that
for Fixed).

- `capacity.mode` set to `fixed` keeps the current slot count; to `memory` or `lower`, the current GiB per slot
  (or the automatic default). `capacity.slots` saves Fixed with that count; `capacity.gib_per_slot` saves the
  current memory mode with that amount. All three share one revision, capacity.json's.
- `check` replies `{"ok": bool, "error": str | null, "preview": str | null}`, plus `"slots": N` for a capacity key:
  the capacity the value would give. `error` is today's panel wording: `Enter a whole number of slots, at least 1.`,
  `At most 48 slots (4 per core).`, `Enter a number.`, `Enter GiB per slot, more than 0.`,
  `Enter a number of at least 1.`.
- `set` and `reset` reply, on success (exit 0), with the whole `settings --json` object; on a stale revision
  (exit 3) with the whole current object and `"conflict": true`; on invalid input (exit 2) or a read-only queue
  (exit 4) with `{"ok": false, "error": "…"}`.
- `reset --all` resets capacity.json and order.json together.

Refresh every and Colour theme are Godfather's own and never go to Slotfather.
