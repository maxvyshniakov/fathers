# What Godfather reads from Spawnfather

Godfather shows Spawnfather's spawns from its commands and its event log: it never imports a Spawnfather module and
reads no file in Spawnfather's state directory but the event log at the path capabilities give. While `spawnfather` is
not yet on `PATH`, Godfather falls back to `junkfather` so either upgrade order works, and reads historical
`"source": "junkfather"` lines in `events.jsonl` as Spawnfather events. This page is the exact shape Godfather's
adapter (`src/godfather/adapters/spawnfather.py`) decodes. Spawnfather owns the contract
(`spawnfather/spawnfather/dashboard.py`, its docs/reference.md); where they disagree, Spawnfather's win.

## `spawnfather capabilities --json`

```json
{"v": 1, "tool": "spawnfather", "version": "0.4.0",
 "dashboard": {"v": 1, "tag": "spwn",
   "snapshot": {"argv": ["status", "--json"], "versions": [2], "poll": 5},
   "events": {"path": "/Users/me/.local/state/spawnfather/events.jsonl", "versions": [1], "kinds": ["entry.owned"]},
   "settings": {"versions": [1]},
   "actions": ["reap", "extend"]}}
```

Godfather needs `v == 1`, `dashboard.v == 1` and a snapshot whose `versions` include 1 or 2 (version 2 adds `cpu` and
a worktree's `repo`, `branch`, `age` and `size`, read alike). Without them Spawnfather is unsupported: no spawns
summary, the Spawns page says why. `poll` is clamped to 5–10 s.

## `spawnfather status --json`

```text
v                   1 | 2
host, time          str, RFC 3339 UTC
entries             [entry]           the active owned entries (running, starting, reaping)
entry               id, state, owner (described), session, agent, lane, owner_kind (claude | codex | shell | lane),
                    lane_project (a lane owner's project: its main checkout), owner_alive (bool | null), age (s),
                    ttl (s | null), ttl_left (s), no_ttl (bool), keep, pid, pgid, processes (int | null), label, mode,
                    cwd, argv, log, end, exit, resource ({kind, id, name} | null: a resource entry)
unowned             [{pid, start, cmd, cwd, root, why, age, alive}]   the last sweep's unowned processes
unowned_resources   [{kind, id, name, why, and for a worktree repo, branch, age, size}]
sweep               {time, result}
cpu                 the last sweep's CPU report (v2), not read
errors              [str]
```

Unknown fields are ignored; a missing one reads as empty, but for the fields that name a row: an entry's `id`, an
unowned process's `pid`, an unowned resource's `kind` and `id`. Without one the snapshot is refused (`an entry has
no id; update spawnfather`, fathers#142), never shown with the row left out. That, another `v`, or no `entries` list
leaves the last good snapshot on screen, dimmed, with `stale since` in the page's border.

### What Godfather computes from it

- An entry's state on the page: `orphan` when `owner_alive` is false, else `expired` when it has a TTL (`ttl` set and
  not `no_ttl`) and `ttl_left` is not above 0, else `running`.
- The spawns summary: the entries, then orphan, expired, live unowned processes (`alive` not false), and unowned
  resources, each only when not zero.
- request ↔ entry: the entry's `pid` or `pgid` is the request's holder or in its process tree, or the entry belongs to
  the lane the request joins to.
- lane ↔ entry, by project and id, never by the id alone (fathers#171): a lane owner's entry (`owner_kind` lane: the
  lane's worktree, scratch and build folders) joins when its `lane` is the lane's id and its `lane_project` is the
  lane's project checkout (Lanefather's `checkout`), whatever its `cwd`; beside a Lanefather without `checkout`, when
  the lane's worktree is inside `lane_project` or the entry is that worktree. Any other entry joins when its `lane`
  is the lane's id, its `cwd` is inside the lane's worktree and its `lane_project`, if any, is the lane's checkout.
  LANE DETAILS lists the lane owner's entries first; the `lane` column shows the entry's `lane`.
- entry ↔ lease: the request whose process tree holds the entry's `pid` or `pgid`. The snapshot names no member pids
  beyond the leader and its group (`processes` is a count), so those are what a join can use.

### Row actions (fathers#88)

`reap` and `extend` from `actions`: `spawnfather reap --id ID --reason=godfather: orphan` for an orphan,
`spawnfather reap --id ID --expired --reason=godfather: expired` for an expired entry (Spawnfather refuses it once the
entry was extended), `spawnfather reap --pid PID --force --reason=godfather: unowned` for an unowned process the last
sweep reported that still ran then (`alive` not false), and `spawnfather extend ID --ttl 1h|4h|1d`. Before one runs,
Godfather reads `status --json` again and needs the row still offering the same action; an unowned pid that an owned
entry holds now is refused, since `reap --pid` resolves an owned entry first. `reap --pid` takes no start time today,
so the check is Godfather's and the sweep's (its `--force` path checks the start it recorded).
