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
| `daemons` | `[pid, start, command]` of each detached agent-browser daemon its processes started (Sweep); a reap takes them with their trees |
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
| `resource.stopped` | resource | its stop ran and it is gone (or was gone already); `reason`, `result`, `resource`, `steps`; also the sweep's removal of an unowned worktree (no `entry` in the subject) |
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
  ["status", "--json"], versions: [2], poll: 5}, events: {path, versions: [1], kinds}, settings: {versions: [1]},
  actions: ["reap", "extend"], changelog}}`.
- `junkfather status --json`: `{v: 2, host, time, entries: [the list --json rows of running entries], unowned:
  [{pid, start, cmd, cwd, root, why, age, alive}] from the last sweep, unowned_resources: [{kind, id, name, why},
  and for a worktree also repo, branch, age, size] from the last sweep, sweep: {time, result}, cpu, errors}`; `cpu`
  is the last sweep's CPU report (Sweep), `null` before one wrote it. Version 2 adds `cpu` to version 1 and changes
  nothing else. A resource entry's row has `resource: {kind, id, name}` and
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
| `own.default_ttl` | 7200 s | 60 s to 7 days | `own`, `own-resource`, `adopt` without `--ttl` |
| `sweep.interval` | 3600 s | 60 s to 1 day, whole seconds | `sweep --plist` without `--interval`; the loaded agent keeps its interval until `install.py --launchd` reloads it |
| `reap.grace` | 10 s | 1 to 600 s | `reap` without `--grace`, the sweep, the supervisor's TTL reap |

Values are durations in seconds (typed as seconds or `90s`, `30m`, `2h`, `1d`), saved in `settings.json` in the state
directory (`{"v": 1, "settings": {KEY: seconds}}`), written atomically and durably under the state lock. Each reader
looks the value up when it runs (`settings.value`), so a change applies to the next command. `JUNKFATHER_STATE`,
`JUNKFATHER_SCRATCH_ROOTS`, `JUNKFATHER_CLAUDE_SESSIONS` and `JUNKFATHER_LANE_RECORDS` are shown as read-only rows
(`env.NAME`, `source: "env NAME"` when set). The reply is `{v, tool: "junkfather", writable, reason, revision, status,
sections: [{id, title, settings: [{key, label, type: "duration", value, default, min, max, step, unit: "s", source,
writable, revision, description}]}]}`; `revision` is the SHA-256 of the file as read, one for all three settings.
`set` and `reset` reply with the document plus `ok` and `error`.

Exit codes: 0 done, 1 not saved for another reason (a full disk, an I/O error), 2 invalid, 3 conflict (a stale
`--revision`; the reply is the current settings with `"conflict": true`), 5 read-only (an environment row, or a
settings file that is not Junkfather's or not writable, which is never overwritten). A command that uses a setting
while the file cannot be read, or holds a value out of range, says so once on stderr and uses the default. 4 is
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
  a group id can be taken again once its last member ends; the agent-browser daemons its processes started
  (`daemons`, by pid, start time and command, Sweep); then the descendants of all of these. A leader refused
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
| `worktree` | the linked worktree's real path (`resource` also records `repo`, its main checkout, `common`, its repository's common git dir, and `branch`) | the path exists and that repository's `git worktree list --porcelain` lists it (another repository at that path: gone) | `git worktree remove PATH` (never `--force` or `rm -rf`), then `git update-ref -d refs/heads/BRANCH SHA` for the tip that was checked; refused while a guard keeps it |

After the stop the resource must be seen gone within the grace (at least 5 s); otherwise, or when a stop command
fails or times out, the result is `failed`, the entry keeps running with `last_error`, and `resource.stop_failed` is
written; a later step that fails (`docker rm` after a stop) makes `resource.stopped` a warning with `warning`. A
reap runs no stop command while whether the resource runs cannot be told: the result is `failed`, and the sweep or
the next reap tries again.

A worktree's stop is `worktrees.plan`, the one guarded removal the sweep uses too; it reads everything again right
before (the git listing, the repository's common git dir, the process table, the working directories) and runs only
when every guard passes (`junkfather/worktrees.py`; the same observations as Lanefather's gc, copied because
common/ is not installed with a command, fathers#117): listed by the recorded repository, not the main worktree,
not locked, not a symbolic link as git lists it, no `.keep` of any kind, not dev, main, master or integrate/*
checked out, no uncommitted or untracked change, no ignored file outside disposable build output (node_modules,
.venv, dist, ...), HEAD merged into the base (an ancestor of it; or, for a squash or rebase, no merge commit beyond
the base and every patch in it by `git cherry`, which passes over a merge's own conflict resolution; the base is
origin's default branch when there is an origin, else the local main or master), no Git repository or other worktree
inside, no process working in it or naming it and none of this user's still running whose working directory cannot
be read, and junkfather not running inside it. Not seen: a process that started inside and then changed directory,
or one that only holds files open there. The branch goes after it only at the tip that was checked: a branch that
moved meanwhile stays, and the stop is a warning. A refused stop is `resource.stop_failed` with `error: "kept: WHY"`,
and the entry keeps running. At its owner's end the ending session's own process (and at SessionEnd everything
below it) does not count as using it, and the hook's detached reaper runs in `/`, so a clean merged worktree is
removed and any other is kept and reported. `reap --dry-run` runs the guards: `would-fail` and why. `own --kind
worktree -- git worktree add ...` owns the path the command names (also with `git -C DIR`); `own-resource --kind
worktree --id PATH` owns one that exists.

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

**Worktrees** (`worktrees` in the report and `sweep.json`). The repositories are `$JUNKFATHER_REPOS`
(colon-separated; empty: none), else every folder in the home folder holding a `.git` directory; each one's linked
worktrees are looked at, wherever they are. One that a live entry or a Lanefather lane that is not done owns is
left to its owner: the lanes are read from the state directories Lanefather's project registry names
(`projects.json`), and while that registry cannot be read no worktree under `.claude/worktrees/` is removed. Any
other goes through the same guarded removal as an owned one's stop (`worktrees.plan`, Resources), which also asks
for 24 hours of quiet (its folder, HEAD, index and HEAD log unchanged), and a `resource.stopped` event whose subject
has no `entry` follows. This and agent-browser's leftover sessions are the two things junkfather deletes that no
entry owns (owner-approved). The rest are reported in `unowned_resources` (`kind: "worktree"`, `repo`, `branch`,
`age`, `size` in bytes, measured when first reported, and `why`; `status --json` shows them too), once each in
`unowned.reported`. `git worktree prune` then drops the entries whose folder is gone. `removed: [{kind, id, repo,
common, branch, size, delete_branch, result}]`, `pruned`, `notes`; `sweep --dry-run` prints the plan (`would remove worktree ...`, `would prune ...`) and changes nothing.

**CPU report** (report only; `cpu` in `sweep.json` and in `status --json`). The sweep's one `ps` call reads `%cpu`
(100 = one core), and each busy process goes to the first rule, in this order, that claims it or one of its
ancestors: a running entry (its members as a reap finds them); a Slotfather lease from `slotfather status --json`
(the holder, by pid and start time, and the tree Slotfather lists; `$JUNKFATHER_SLOTFATHER` names another command;
missing or not answering, the step is left out and a note says so); an agent session among its ancestors. The rest
is unattributed: `{v: 1, at, cores, attributed: {entry, lease, session: cores}, unattributed, owners: [{kind, id,
label, cores, processes}] (20 busiest), top: [{pid, start, cmd (120 characters), cwd, uid, cpu, processes}] (the 10
busiest unattributed trees, each under its topmost ancestor below launchd), notes}`. `%cpu` is ps's own average,
not a measurement between two sweeps.

**agent-browser** (`agent_browser` in the report and `sweep.json`). agent-browser keeps one set of files per session
at the top of `~/.agent-browser` (`$JUNKFATHER_AGENT_BROWSER`): `NAME.config`, `.target`, `.engine` and, while its
daemon runs, `.pid`, `.sock`, `.stream`, `.version`; NAME often ends in the pid of the process that started it.
- Leftovers: a session's files are removed when no agent-browser program runs as the pid its `.pid` names or holds
  its socket, the pid in its name runs no more, and none of its files changed for a day. Subdirectories (the
  downloaded browsers, saved sessions and auth), dotfiles and symbolic links are never touched; an unreadable
  process table, or a daemon whose session cannot be told (no `.pid` names it and lsof fails), removes nothing. Right
  before a session's files go, the process table having been read again, they are checked again: any of its files
  changed within the day, its `.pid` naming a running agent-browser program or its starting pid running keeps it
  (a daemon that started during the sweep keeps its socket and pid file), with a note.
  `leftovers: {dir, sessions, removed, files, bytes, names (the first 50), notes}`; `sweep --dry-run` lists the same
  and removes nothing.
- Daemons: a daemon detaches and launchd adopts it. It is matched to its session by the `.pid` file that names it,
  else by the session socket it holds, and so to the process that started the session (the pid in the name, started
  no later than the daemon). When that process is an entry's member, the daemon joins the entry's `daemons` (`[pid,
  start, command]`; the sweep, the supervisor's refresh and a reap add it) and is reaped with it (Ending an
  entry). When it is in a Slotfather lease's tree, the CPU report gives the daemon's tree to that lease;
  junkfather never stops anything for a lease. A daemon no entry or lease owns, under 1% CPU with its session
  unchanged for an hour, is listed in `idle: [{pid, start, cmd, session, matched, starter, age, idle, cpu,
  processes}]`, never stopped.

## Command line

The command line follows [docs/cli.md](../../docs/cli.md); `tests/test_cli_convention.py` checks it. A refusal is
one line on stderr, `junkfather: <reason>`, with nothing on stdout (`settings --json` still prints its reply); a
usage error adds `Try 'junkfather [COMMAND] --help'.`. cli.py raises `errors.Refused` (or `errors.Usage`) and
`cli.main` prints it, so no command formats its own refusal. An unexpected exception prints `junkfather: internal
error: TYPE: MESSAGE` and appends its traceback to `logs/junkfather.log` in the state directory.

| Code | When |
| --- | --- |
| 0 | done; `reap` and `sweep` also when there was nothing to do |
| 1 | a reap or a resource's create command failed, the process table could not be read, `own-resource` could not find or name the resource, `--alive` gave no answer |
| 2 | usage: an unknown command, option or value, `own-resource --kind safari-tp` |
| 3 | refused by the current state: `adopt` or `reap --pid` of a pid junkfather may not take, `hand` or `extend` of an entry that ended, a resource another entry owns, a stale settings `--revision` |
| 5 | read-only: a settings row the environment sets, a settings file junkfather may not write |
| 6 | not found: no such entry (`hand`, `extend`, `reap --id`), no such pid (`adopt`, `reap --pid`), `--alive` says the resource is gone |
| 75 | `sweep` while another sweep holds the lock: try again later |

`own` (attached) and `own --kind` pass the exit status of the command they run through, like `slotfather run`.

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
