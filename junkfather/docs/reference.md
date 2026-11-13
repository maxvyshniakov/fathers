# Reference

## Entries and their states

An entry is one owned process group, `entries/<id>.json` under `$JUNKFATHER_STATE` (default
`~/.local/state/junkfather`):

| Field | Meaning |
| --- | --- |
| `id`, `label`, `argv`, `cwd`, `created` | what was started, where and when |
| `mode` | `attached` (junkfather is the parent), `detached` (a supervisor is), `adopted` (no parent of ours), `resource` (no process: see Resources) |
| `resource` | `resource` entries: `{kind, id, name, ...}` and what its kind needs (`remove`; `pid`, `start` for safari-tp; `alive`, `stop` argv for command) |
| `pid`, `pgid`, `start`, `seen` | the leader: its pid, group, start time (`ps lstart`, C locale) and command as `ps` showed it at launch |
| `group` | whether the leader leads its own group (true for `own`; for `adopt`, only when pid = pgid) |
| `known` | `[pid, start]` of every member seen while it ran: what a reap may signal once the leader is gone |
| `known_seen` | `[pid, start, command]` for the same members: the command each showed when last seen (absent in entries written before it: those members are checked by pid and start only) |
| `ttl`, `expires` | seconds, and the epoch at which the entry is reaped whatever its owner |
| `keep` | `owner`: ends with the agent that started it; `session`: survives a subagent's stop, ends with the session |
| `owner` | `{kind: claude\|codex\|shell, session, agent, agent_type, pid, pid_start, lane}` |
| `handed` | each hand-over: `{at, from, to}` |
| `supervisor`, `log` | the supervising process (`pid`, `start`) and, detached, the output file |
| `state` | `starting`, `running`, `reaping`, then `exited` (code in `exit`), `reaped` (`end: {reason, result, how, pids}`), `gone` (ended unseen) or `failed` |

A state changes only by one of these moves, each made under the state directory's lock and only from the states
listed (otherwise the entry is left as it is): `starting` to `running` (the command started) or `failed`; `running`
to `reaping` (one reaper holds it) and back to `running` (the reap failed, or its reaper died: the sweep releases it
after 5 minutes) or on to `reaped`; `running` to `exited` (the supervisor saw the command end with nothing left) or
`gone` (the sweep found nothing left), and `gone` back to `running` when the supervisor's own command still runs.
Process and resource entries share them (`junkfather/entry.py`).

Finished entries and their logs are pruned after 7 days.

## Events

`events.jsonl` in the state directory follows Godfather's event schema version 1 ([`godfather/SPEC.md`](../../godfather/SPEC.md), Event
schema): `v`, `time` (RFC 3339 UTC, milliseconds), `host`, `source: "junkfather"`, `kind`, `subject`, `actor`,
`details`, `level` when not `info`. One `write` per line on an `O_APPEND` file of mode 0600; past 4 MiB it becomes
`events.jsonl.1` under an flock on `events.lock`. Strings carry no control characters and are cut to 400
characters. An event follows the change it describes; one that cannot be written never fails the action.

| Kind | Subject | When; details |
| --- | --- | --- |
| `entry.owned` | entry | the command started; `argv`, `cwd`, `ttl`, `keep`, `mode`, `pgid`, `log` |
| `entry.adopted` | entry | `cmd`, `ttl`, `keep`, `pgid`, `group` |
| `entry.exited` | entry | the leader exited and nothing of it remains; `exit` |
| `entry.extended` | entry | `ttl`, `expires` |
| `entry.handed` | entry | `from`, `to` (kind, session, agent, lane) |
| `entry.reaped` | entry | `reason`, `result`, `how`, `signals` (`TERM`, `KILL`), `pids` |
| `entry.reap_failed` (error) | entry | processes outlived SIGKILL; the entry keeps running; `left` |
| `resource.owned` | resource | `own --kind` or `own-resource`; `resource`, `argv`, `cwd`, `ttl`, `keep` |
| `resource.stopped` | resource | its stop ran and it is gone (or was gone already); `reason`, `result`, `resource`, `steps` |
| `resource.stop_failed` (error) | resource | a stop command failed or timed out, or it still runs; the entry keeps running; `error`, `steps` |
| `unowned.reported` (warn) | process or resource | a sweep listed it for the first time; a process: `cmd`, `cwd`, `root`, `why`, `start`, `age`; a resource: `kind`, `id`, `name`, `why` |
| `unowned.reaped` | process | `reap --pid --force`; `cmd`, `start`, `result`, `pids` |
| `sweep.finished` | sweep | `reaped`, `failed`, `gone`, `running`, `unowned`, `pruned`, `errors` |
| `hook.failed` (error) | hook | the hook hit an error and let the event pass; `error` |

The subject of an entry is `{"type": "entry", "id", "label", "pid"}`; of a resource `{"type": "resource", "id":
"KIND:ID", "kind", "entry", "label"}`. The actor is the owner (`agent` with
`session`, `agent`, `pid`, `lane`; or `process`) for own and adopt, the caller for reap, hand and extend, and
`{"type": "tool", "name": "junkfather", "via": "sweep" | "hook" | "supervisor"}` for what junkfather does on its own.
A command run with `GODFATHER_ACTOR` set carries `via: "godfather"`.

## Dashboard contract

- `junkfather capabilities --json`: `{v, tool, version, commit, dashboard: {v: 1, tag: "junk", snapshot: {argv:
  ["status", "--json"], versions: [1], poll: 5}, events: {path, versions: [1], kinds}, settings: {versions: [1]},
  actions: ["reap", "extend"], changelog}}`.
- `junkfather status --json`: `{v: 1, host, time, entries: [the list --json rows of running entries], unowned:
  [{pid, start, cmd, cwd, root, why, age, alive}] from the last sweep, unowned_resources: [{kind, id, name, why}]
  from the last sweep, sweep: {time, result}, errors}`. A resource entry's row has `resource: {kind, id, name}` and
  `processes: null`. It changes nothing.
- `junkfather changelog --json [--since VERSION]`: the installed release's CHANGELOG.md for Godfather's what's new
  (`godfather/docs/contracts/changelog.md`): `{v: 1, tool: "junkfather", version, source, entries: [{version, date,
  section, text}]}`, newest first. The Unreleased entries come first, since installs come from main, as `{version:
  null, unreleased: true, date: null, source: <installed commit>, section, text}` (`source` is `null` from a
  checkout); an entry's continuation lines join it and its markdown stays; `--since` keeps only releases newer than
  that version, and the Unreleased entries. `source` is the file read, `null` (with no entries)
  when the release has none. It reads one file and changes nothing.
- Without `--json`, `status`, `capabilities` and `changelog` print the same for people; a dashboard always passes
  `--json`, and these shapes only gain fields.

## Settings

`junkfather settings` follows Godfather's settings contract ([`godfather/SPEC.md`](../../godfather/SPEC.md),
Settings contract), as Slotfather's and Lanefather's do:

```text
junkfather settings [--json]                                  the schema and the current values
junkfather settings check KEY VALUE [--json]                  validate without saving: {v, ok, error, preview}
junkfather settings set KEY VALUE [--revision R] [--json]     save; refused when R no longer matches
junkfather settings reset (KEY | --all) [--revision R] [--json]
```

| Key | Default | Range | Used by |
| --- | --- | --- | --- |
| `own.default_ttl` | 7200 s | 1 s to 7 days | `own`, `own-resource`, `adopt` without `--ttl` |
| `sweep.interval` | 3600 s | 60 s to 1 day, whole seconds | `sweep --plist` without `--interval`; the loaded agent keeps its interval until `install.py --launchd` reloads it |
| `reap.grace` | 10 s | 0 to 600 s | `reap` without `--grace`, the sweep, the supervisor's TTL reap |

Values are durations in seconds (typed as seconds or `90s`, `30m`, `2h`, `1d`), saved in `settings.json` in the state
directory (`{"v": 1, "settings": {KEY: seconds}}`), written atomically and durably under the state lock. Each reader
looks the value up when it runs (`settings.value`), so a change applies to the next command. `JUNKFATHER_STATE`,
`JUNKFATHER_SCRATCH_ROOTS`, `JUNKFATHER_CLAUDE_SESSIONS` and `JUNKFATHER_LANE_RECORDS` are shown as read-only rows
(`env.NAME`, `source: "env NAME"` when set). The reply is `{v, tool: "junkfather", writable, reason, revision, status,
sections: [{id, title, settings: [{key, label, type: "duration", value, default, min, max, step, unit: "s", source,
writable, revision, description}]}]}`; `revision` is the SHA-256 of the file as read, one for all three settings.
`set` and `reset` reply with the document plus `ok` and `error`.

Exit codes: 0 done, 2 invalid, 3 conflict (a stale `--revision`; the reply is the current settings), 5 read-only (an
environment row, or a settings file that is not Junkfather's or not writable, which is never overwritten). 4 is
Slotfather's "another user's queue" (fathers#118) and is not used here.

## Ending an entry

- **Who ends it**: its owner's end (the `SubagentStop` hook for a subagent's entries with `keep: owner`, the
  `SessionEnd` hook for all of a session's), its TTL (the supervisor on the spot, the `Stop` hook at the end of a
  turn, the sweep), the sweep when the owner's session is gone, a signal to an attached junkfather, or
  `junkfather reap`.
- **Which processes**: the leader when its pid still has the recorded start time and command (same command line,
  or the same program, as `seen` or as `known_seen` last recorded it; `--force` skips the command check, never the
  start time); with a verified leader of its own group, the whole group, which the live leader pins; without one,
  only the `known` members whose start time and command (`known_seen`) still match, never the group id alone, since
  a group id can be taken again once its last member ends; then the descendants of all of these. A leader refused
  for its start time or its command is not taken through `known` either, though `known` lists it: its recorded
  members still are. The supervisor records its own child's current command (a launcher such as
  `/usr/bin/python3` execs another program), since a child it has not waited for cannot have had its pid reused. Only
  this user's processes, never pid 1, junkfather or its ancestors, and never a process with a terminal or an agent
  session (Claude Code, Codex), nor anything below one. Up to three rounds: a process forked during the grace
  period is found by the next read.
- **How**: SIGTERM to each target whose start time and command were just read again, up to `--grace` seconds (10),
  SIGKILL to what is left, again only to unchanged identities. Results: `stopped`, `killed`, `gone` (nothing left to
  signal), `failed` (the entry keeps running and records `last_error`; also when `ps` cannot be read, which is never
  taken for "they ended"). Start times are read in UTC, whatever the caller's `TZ`.
- **Identity**: a pid with its start time, and its command as the second factor. `ps lstart` has one-second
  granularity, so a pid that ends and is taken again within the same second has the same start time; only the
  command tells them apart (same command line or same program, so a process that renamed its arguments still
  matches).
- **A failed TTL reap**: the supervisor tries again after 1 s, then 2, 4 and so on up to 60 s, and only the first
  failure of a streak writes `entry.reap_failed`; each failure still updates `last_error`.
- **Never** a process junkfather did not start or adopt, except `reap --pid PID --force` on a pid the last sweep
  listed as unowned, with the same start time and command and still unowned.
- **One policy**: `adopt`, a reap, `reap --pid --force`, the sweep's unowned report and its unowned resources
  decide what they may touch by the same rules (`junkfather/safety.py`).

## Resources

A resource outlives the process that made it. Its entry has `mode: "resource"` and no process group; owner, TTL,
keep, hand-over, `reap` (by id, owner, subagent, lane), the hooks and the sweep treat it like any entry. Each kind
lists what runs (one call per sweep), tells whether one resource runs (unknown never marks it gone nor reaps it), and
stops it with argv run without a shell, with a timeout (`STOP_TIMEOUT`, 120 s, plus the grace):

| Kind | Id | Runs while | Stop |
| --- | --- | --- | --- |
| `simulator` | UDID | `xcrun simctl list -j devices` shows it not `Shutdown` | `xcrun simctl shutdown UDID` |
| `docker` | the full container id (`own-resource` takes a name or short id) | `docker ps` lists it | `docker stop -t GRACE ID`, then `docker rm ID` with `--remove` |
| `tailscale-serve` | `PROTO:PORT[/PATH]` (`https:6475`, `https:443/docs`, `tcp:5432`) | `tailscale serve status --json` has that route | `tailscale serve --PROTO=PORT [--set-path=PATH] off --yes`: that route only |
| `safari-tp` | its main process's pid | that pid runs with its recorded start time | `osascript`: `tell application id "com.apple.SafariTechnologyPreview" to quit`, only while it is the only instance |
| `command` | a name | `--alive` exits 0 (1: gone; anything else: unknown) | `--stop` |

After the stop the resource must be seen gone within the grace (at least 5 s); otherwise, or when a stop command
fails or times out, the result is `failed`, the entry keeps running with `last_error`, and `resource.stop_failed` is
written; a later step that fails (`docker rm` after a stop) makes `resource.stopped` a warning with `warning`. A
reap runs no stop command while whether the resource runs cannot be told: the result is `failed`, and the sweep or
the next reap tries again.

A quit by bundle id cannot tell two Safari Technology Preview instances apart, so the stop runs only while the owned
instance is the only one (same pid and start time); otherwise it fails and every instance keeps running.
`own-resource` refuses `safari-tp`: an instance that already runs cannot be told from a person's own browser. Known
limit: a window a person opens into the agent's own instance (LaunchServices reuses it) closes with that instance.

`own --kind KIND -- CMD` runs CMD (attached, up to 10 minutes), passes its output and a failing exit status through,
and owns what CMD made: what it names (a UDID in its arguments, the id `docker run -d` prints, the route a
`tailscale serve` command sets: `--https=6475`, or `https:443` for a bare `--bg 6475`) once it shows as new or
changed; otherwise exactly one item new or changed in one listing right after CMD (for `safari-tp`, within 15 s, a
main process that started after CMD did). Two unnamed new items, or no listing from before and nothing named, own
nothing: another agent's or a person's resource could be among them. Nothing new, nothing owned. One entry per resource: owning one a live entry holds is refused, as is one that
does not run.

## Owners and liveness

A Claude Code session lives while its recorded process (`CLAUDE_PID`, with its start time) runs, or while Claude
Code's session registry, `~/.claude/sessions/<pid>.json` (`{pid, sessionId, procStart, ...}`), names the session
with a process that runs and started when the record says (a resumed session gets a new process). A Codex session
lives while its `codex` process runs; a shell owner (outside an agent) is the nearest ancestor with a terminal and
lives while it runs; a script or cron job has no such ancestor, so only the TTL ends its entries. When the process
table cannot be read, or an owner has no process to check and the registry does not name it, its liveness is
unknown, and unknown never reaps.

Liveness compares a process's pid and start time only, by design: the owner's recorded process, an entry's
supervisor and the reaper holding an entry in `reaping`. A start time has one-second granularity, so a pid taken
again within that second reads as alive; that only delays a reap (the TTL still ends the entry, the sweep tries
again), while every signal checks the command as well (Ending an entry). Recording each owner's command to compare
it would add an owner field for no safety gain.

The subagent comes from claims: the PreToolUse hook writes `claims/<session>/<tool_use_id>.json` (agent id, agent
type, command) for each Bash call naming junkfather, and `own` takes the newest claim of its session whose command
holds `own` and every one of its arguments, and marks it used for them. No full match (a script that runs
junkfather, a hook not installed), or two agents of the session with a matching command within a minute of each
other, leaves the entry to the session alone, and `own` says so: a session-level entry lives longer, never shorter. Claude Code passes `agent_id` to hooks but not to the Bash
tool's environment, hence the claims.

`own` and `own-resource` take the subagent from `--agent`, `JUNKFATHER_AGENT` or a claim; `adopt` takes it from
`--agent` only, since the process it names was not started by the Bash call that runs it. `hand --to SESSION`
records the session's process when the registry names it live; otherwise the entry waits for that session (a
Codex one when the registry never named it) and ends at its TTL or when swept as orphaned.

## Hooks

| Event | junkfather does |
| --- | --- |
| `PreToolUse` (Bash, command names junkfather) | writes a claim |
| `SubagentStop` | reaps the subagent's entries with `keep: owner` |
| `SessionEnd` | reaps the session's entries (handed ones belong to their new session) |
| `Stop` | the end of a main-agent turn, not of the session: reaps the session's entries past their TTL |

The hook never prints, never blocks and exits 0; a reap runs in a detached `junkfather reap` so the agent never
waits for a grace period. Mastack's `hooks/lifecycle.py` dispatches the events and `bash_guard.py` the PreToolUse
observer (`install.py --mastack`).

## Sweep

Hourly (`com.junkfather.sweep`, `sweep.out` beside the state): reaps entries past their TTL and entries whose owner
is gone, marks entries with no process left `gone`, resets an entry stuck in `reaping` for 5 minutes, prunes old
entries, and reports unowned processes: this user's, outside every running entry, whose working directory or a
path in their command is under `/private/tmp/claude-<uid>/`, `/tmp/claude-<uid>/`, `$JUNKFATHER_SCRATCH_ROOTS`, or a
`.claude/worktrees/` or `.codex/worktrees/` directory. Left out: agent sessions, processes with a terminal, anything
below a live agent session (its shells, MCP and language servers at work), and any process whose tree holds a
terminal's process or an agent session (a tmux server). A browser or server left behind has been adopted by launchd,
so it is reported; one item per tree and process group. It also reports booted simulators, running containers and
`tailscale serve` routes that no live entry owns (not Safari Technology Preview, which is as often a person's own
browser), once each in `unowned.reported`; a kind whose tool is not installed is skipped, and one that cannot be
listed is noted. A resource entry whose kind says it is gone is marked `gone`. The report goes to `unowned.json`
and never stops anything; there is no `--force` for a resource.

## Boundaries

On 2026-11-10 Slotfather's sweep took machine-wide hygiene for processes and disk. junkfather now takes the process
part for everything launched or adopted through it; the rest stays where it is, and nothing is removed from
Slotfather or Lanefather yet.

| Moves to junkfather | Stays |
| --- | --- |
| Ownership of agent-started long-lived processes: who started it, until when | Slotfather sweep's `removed`, `orphan` and `dev-server` classes, as the net for processes started without junkfather |
| Ending them at their owner's end (SubagentStop, SessionEnd) and at their TTL | Slotfather's lease guard, `procs`, containers, memory report and self-matching waits |
| Orphan detection by owner session (registry, recorded process) instead of by process kind and age | Lanefather `done`/`gc` asking `slotfather sweep --pid` (via the project's `process_scan`) before removing a worktree |
| Reporting unowned scratchpad and worktree processes (the Firefox case, which no Slotfather class matched) | Straylight's janitor (scratchpads, disk, Docker listing) and lane-stop hook |

Later, once agents launch through junkfather (tracked in a Slotfather issue): Slotfather can drop its `dev-server`
class and the browser kinds of `orphan` (agent-browser, Chromium, Playwright, Puppeteer), and its sweep must spare
processes in a running junkfather entry (`junkfather list --json`) as it spares leases; Lanefather `done` can call
`junkfather reap --lane <lane>` first.
