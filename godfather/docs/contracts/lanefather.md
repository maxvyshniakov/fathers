# What Godfather reads from Lanefather

Godfather shows Lanefather's lanes from its commands and its event log: it never imports a Lanefather module and
reads no file in Lanefather's state directories but the event log at the path capabilities give. This page is the
exact shape Godfather's adapter (`src/godfather/adapters/lanefather.py`) decodes. Lanefather owns the contract
(`lanefather/lanefather/status.py`, `show.py`, `runtime.py`); where this page and Lanefather's own docs disagree,
Lanefather's win and this page and the adapter follow.

## `lanefather capabilities --json`

```json
{"v": 1, "tool": "lanefather", "version": "0.9.0",
 "dashboard": {"v": 1, "tag": "lane",
   "snapshot": {"argv": ["status", "--json"], "versions": [1], "poll": 5},
   "show": {"versions": [1]},
   "events": {"path": "/Users/me/.local/state/lanefather/events.jsonl", "versions": [1]},
   "settings": {"versions": [1]},
   "actions": ["park"]}}
```

- Godfather needs `v == 1`, `dashboard.v == 1` and a snapshot whose `versions` include 1. Without them Lanefather is
  unsupported: no lanes summary, the Lanes page says why, Settings' STATUS too.
- `poll` is a hint in seconds, clamped to 5–10.
- `show` advertises `lanefather show ID --json` (below). Without it Godfather never runs `show`: a request whose
  lane the status snapshot does not join says `lanefather too old` in DETAILS.
- `actions` lists the row actions Lanefather offers (fathers#88); Godfather offers only those it knows, and never
  land, promote, done, sync, review or sweep. Park (fathers#57, not advertised yet) runs as
  `lanefather park ID --state-dir DIR --reason=R` with the lane's own state directory, never the project Godfather
  runs in; it is offered for an `active` lane and checked again in a fresh `status --json` before it runs.

## `lanefather status --json`

Machine-wide, read-only: every project in Lanefather's registry.

```text
v            1
host         str
time         RFC 3339 UTC
projects     [project]
project      {project, state_dir, config, repository, checkout, cap, cap_source, active, planned, gate, lanes, damaged}
             | {project, state_dir, error}        a project that cannot be read; the rest of the page works
  checkout   str | null                           the main checkout's real path, the project a Spawnfather lane
                                                  owner names (fathers#171); null from an older Lanefather
  gate       {sha, status, at} | null             the newest batch gate; status "red" puts 🔥 in the summary
  lanes      [lane]                               the lanes not done
lane         id            str                    unique within its project only
             status        str                    planned, active, landed, …
             ticket        {number: str | null, url: str | null}
             branch, base, worktree   str | null
             claims, after [str]
             created       RFC 3339 UTC | null
             receipt       {status: str, tree_match: bool | null, path: str | null}
             post_merge    str | null
             review        {status: str, open: int, findings: int}
             logs          {phase: absolute path}  the newest run log of each phase
             outcome       any
```

A lane is identified by its project's `state_dir` and its `id`, never by its id alone (SPEC, Open risks 5). Unknown
fields are ignored; a missing one reads as empty, but for a project's `state_dir` and a lane's `id`, which name
them: without one the snapshot is refused (`a lane has no id; update lanefather`, fathers#142), never shown with the
row left out. That, another `v`, or no `projects` list leaves the last good snapshot on screen, dimmed, with
`stale since` in its border.

### What Godfather computes from it

- The lanes summary: `active` and `planned` summed over readable projects; reviews: lanes whose review status is
  `running` or that have `open` findings; `🔥 gate red` when any project's `gate.status` is `red`.
- request ↔ lane: the request's `meta.lane` in the project whose `state_dir` equals its `meta.lanefather.state`; with
  only `meta.lane`, the lane of that id whose `worktree` holds the request's working directory (else its holder's
  directory). Otherwise none.

## `lanefather show ID (--state-dir DIR | --project-dir DIR) --json`

Asked only when capabilities advertise `show`, and only for a request the status snapshot does not join.

```text
version     1
id          str           must equal ID
project, status, branch, base, worktree, repository   str | null
ticket      {number, url}
checks      {status, current: bool | null, tree_match: bool | null}
review      {status, findings: int, unresolved: int}
claims, after   [str]
```

A non-zero exit, a reply over 64 KiB, another `version` or `id`, or a field of another type is shown as the lane's
error in DETAILS (`lanefather query failed`, `unsupported lane summary`, `invalid lane summary`); a timeout (1.5 s)
as `lanefather unavailable`. Replies are cached for 5 s per state directory and lane.
