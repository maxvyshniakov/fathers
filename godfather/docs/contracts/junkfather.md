# What Godfather reads from Junkfather

Godfather shows Junkfather's processes from its commands and its event log: it never imports a Junkfather module and
reads no file in Junkfather's state directory but the event log at the path capabilities give. This page is the
exact shape Godfather's adapter (`src/godfather/adapters/junkfather.py`) decodes. Junkfather owns the contract
(`junkfather/junkfather/dashboard.py`, its docs/reference.md); where they disagree, Junkfather's win.

## `junkfather capabilities --json`

```json
{"v": 1, "tool": "junkfather", "version": "0.4.0",
 "dashboard": {"v": 1, "tag": "junk",
   "snapshot": {"argv": ["status", "--json"], "versions": [2], "poll": 5},
   "events": {"path": "/Users/me/.local/state/junkfather/events.jsonl", "versions": [1], "kinds": ["entry.owned"]},
   "settings": {"versions": [1]},
   "actions": ["reap", "extend"]}}
```

Godfather needs `v == 1`, `dashboard.v == 1` and a snapshot whose `versions` include 1 or 2 (version 2 adds `cpu` and
a worktree's `repo`, `branch`, `age` and `size`, read alike). Without them Junkfather is unsupported: no procs
summary, the Processes page says why. `poll` is clamped to 5–10 s.

## `junkfather status --json`

```text
v                   1 | 2
host, time          str, RFC 3339 UTC
entries             [entry]           the active owned entries (running, starting, reaping)
entry               id, state, owner (described), session, agent, lane, owner_alive (bool | null), age (s),
                    ttl (s | null), ttl_left (s), keep, pid, pgid, processes (int | null), label, mode, cwd, argv,
                    log, end, exit, resource ({kind, id, name} | null: a resource entry)
unowned             [{pid, start, cmd, cwd, root, why, age, alive}]   the last sweep's unowned processes
unowned_resources   [{kind, id, name, why, and for a worktree repo, branch, age, size}]
sweep               {time, result}
cpu                 the last sweep's CPU report (v2), not read
errors              [str]
```

Unknown fields are ignored; a missing one reads as empty. Another `v`, or no `entries` list, leaves the last good
snapshot on screen, dimmed, with `stale since` in the page's border.

### What Godfather computes from it

- An entry's state on the page: `orphan` when `owner_alive` is false, else `expired` when it has a TTL and
  `ttl_left` is not above 0, else `running`.
- The procs summary: the entries, then orphan, expired, and unowned (processes and resources), each only when not
  zero.
- request ↔ entry: the entry's `pid` or `pgid` is the request's holder or in its process tree, or the entry belongs to
  the lane the request joins to.
- lane ↔ entry: the entry's `lane` equals the lane's id and its `cwd` is inside the lane's worktree.
- entry ↔ lease: the request whose process tree holds the entry's `pid` or `pgid`.
