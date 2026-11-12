# Reference

## Entries and their states

An entry is one owned process group, `entries/<id>.json` under `$JUNKFATHER_STATE` (default
`~/.local/state/junkfather`):

| Field | Meaning |
| --- | --- |
| `id`, `label`, `argv`, `cwd`, `created` | what was started, where and when |
| `mode` | `attached` (junkfather is the parent), `detached` (a supervisor is), `adopted` (no parent of ours) |
| `pid`, `pgid`, `start`, `seen` | the leader: its pid, group, start time (`ps lstart`, C locale) and command as `ps` showed it at launch |
| `group` | whether the leader leads its own group (true for `own`; for `adopt`, only when pid = pgid) |
| `known` | `[pid, start]` of every member seen while it ran: what a reap may signal once the leader is gone |
| `ttl`, `expires` | seconds, and the epoch at which the entry is reaped whatever its owner |
| `keep` | `owner`: ends with the agent that started it; `session`: survives a subagent's stop, ends with the session |
| `owner` | `{kind: claude\|codex\|shell, session, agent, agent_type, pid, pid_start, lane}` |
| `handed` | each hand-over: `{at, from, to}` |
| `supervisor`, `log` | the supervising process (`pid`, `start`) and, detached, the output file |
| `state` | `starting`, `running`, `reaping`, then `exited` (code in `exit`), `reaped` (`end: {reason, result, how, pids}`), `gone` (ended unseen) or `failed` |

Finished entries and their logs are pruned after 7 days.

## Events

`events.jsonl` in the state directory follows Godfather's event schema version 1 (`~/godfather/SPEC.md`, Event
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
| `unowned.reported` (warn) | process | a sweep listed it for the first time; `cmd`, `cwd`, `root`, `why`, `start`, `age` |
| `unowned.reaped` | process | `reap --pid --force`; `cmd`, `start`, `result`, `pids` |
| `sweep.finished` | sweep | `reaped`, `failed`, `gone`, `running`, `unowned`, `pruned`, `errors` |
| `hook.failed` (error) | hook | the hook hit an error and let the event pass; `error` |

The subject of an entry is `{"type": "entry", "id", "label", "pid"}`. The actor is the owner (`agent` with
`session`, `agent`, `pid`, `lane`; or `process`) for own and adopt, the caller for reap, hand and extend, and
`{"type": "tool", "name": "junkfather", "via": "sweep" | "hook" | "supervisor"}` for what junkfather does on its own.
A command run with `GODFATHER_ACTOR` set carries `via: "godfather"`.

## Dashboard contract

- `junkfather capabilities --json`: `{v, tool, version, commit, dashboard: {v: 1, tag: "junk", snapshot: {argv:
  ["status", "--json"], versions: [1], poll: 5}, events: {path, versions: [1], kinds}, actions: ["reap", "extend"],
  changelog}}`. No `settings` block yet: settings commands are a later release.
- `junkfather status --json`: `{v: 1, host, time, entries: [the list --json rows of running entries], unowned:
  [{pid, start, cmd, cwd, root, why, age, alive}] from the last sweep, sweep: {time, result}, errors}`. It changes
  nothing.

## Ending an entry

- **Who ends it**: its owner's end (the `SubagentStop` hook for a subagent's entries with `keep: owner`, the
  `SessionEnd` hook for all of a session's), its TTL (the supervisor on the spot, the `Stop` hook at the end of a
  turn, the sweep), the sweep when the owner's session is gone, a signal to an attached junkfather, or
  `junkfather reap`.
- **Which processes**: the leader when its pid still has the recorded start time and command (same command line,
  or the same program; `--force` skips the command check, never the start time); with a verified leader of its own
  group, the whole group, which the live leader pins; without one, only the `known` members, never the group id
  alone, since a group id can be taken again once its last member ends; then the descendants of all of these. Only
  this user's processes, never pid 1, junkfather or its ancestors.
- **How**: SIGTERM to each target whose start time was just read again, up to `--grace` seconds (10), SIGKILL to
  what is left, again only to unchanged identities. Results: `stopped`, `killed`, `gone` (nothing left to signal),
  `failed` (the entry keeps running and records `last_error`).
- **Never** a process junkfather did not start or adopt, except `reap --pid PID --force` on a pid the last sweep
  listed as unowned, with the same start time and still unowned.

## Owners and liveness

A Claude Code session lives while its recorded process (`CLAUDE_PID`, with its start time) runs, or while Claude
Code's session registry, `~/.claude/sessions/<pid>.json` (`{pid, sessionId, procStart, ...}`), names the session
with a process that runs and started when the record says (a resumed session gets a new process). A Codex session
lives while its `codex` process runs; a shell owner while that process runs. When the process table cannot be
read, or an owner has no process to check, its liveness is unknown, and unknown never reaps.

The subagent comes from claims: the PreToolUse hook writes `claims/<session>/<tool_use_id>.json` (agent id, agent
type, command) for each Bash call naming junkfather, and `own` takes the newest claim of its session whose command
holds its arguments. When two agents of the session ran a matching command within a minute of each other, the
entry belongs to the session alone and `own` says so. Claude Code passes `agent_id` to hooks but not to the Bash
tool's environment, hence the claims.

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
`.claude/worktrees/` or `.codex/worktrees/` directory. Agent sessions, processes with a terminal and processes under
a live agent session younger than 6 h are left out; one item per tree and process group. The report goes to
`unowned.json` and never stops anything.

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
