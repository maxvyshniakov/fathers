# junkfather

Owns the long-lived processes agents start (preview servers, browsers, drivers, watchers), and the resources that
outlive them (booted simulators, containers, `tailscale serve` routes, Safari Technology Preview), and ends them when
their owner ends or their time is up. It keeps [Slotfather](../slotfather) (machine
capacity) and [Lanefather](../lanefather) (lanes) out of process cleanup.

Why: a subagent started a headless Firefox through Marionette with a profile in its session scratchpad. The
Firefox ran unowned for 11 hours; neither `slotfather sweep` nor `lanefather done` covered it, because it never went
through either tool. junkfather records the owner when the process starts, so the owner's end can end it.

Python 3.9+, standard library only, macOS and Linux.

## How agents launch through junkfather

Copy this into an agent brief:

> Start every process that outlives a single command (a dev or preview server, Storybook, a browser or WebDriver
> session, a file watcher, `tail -f`) through junkfather, with a TTL:
>
> ```sh
> junkfather own --detach --ttl 2h --label storybook -- npm run storybook -- --port 6011
> ```
>
> It prints the entry id and the log file and returns at once. The process ends when you (this agent) stop, when
> its TTL passes, or on `junkfather reap --id <id>`. For a command you wait for (tests, a build), use it attached:
> `junkfather own --ttl 30m -- npm test` (output and exit status pass through). Under `run_in_background` either
> form works; stopping the background task stops the attached form's command.
>
> - Something the orchestrator or the next agent must keep using: add `--keep-for session` (it survives your stop
>   and ends with the session), or hand it over: `junkfather hand <id> --to <session-id>`.
> - Need more time: `junkfather extend <id> --ttl 1h`. See what you own: `junkfather list`.
> - A process you started without junkfather: `junkfather adopt <pid> --ttl 1h`.
> - Never `kill` a process junkfather lists; use `junkfather reap --id <id>`.
>
> Something that outlives the command that made it (a booted simulator, a `docker run -d` container, a
> `tailscale serve --bg` route, a Safari Technology Preview opened with `open -g`): put `--kind` before `--`, and
> junkfather runs the command, then owns what it made and ends it the same way, through that kind's own stop:
>
> ```sh
> junkfather own --kind simulator --ttl 1h -- xcrun simctl boot <udid>
> junkfather own --kind docker --remove -- docker run -d -p 5432:5432 postgres   # --remove: docker rm after stop
> junkfather own --kind tailscale-serve -- tailscale serve --bg --https=6475 6475
> junkfather own --kind safari-tp -- open -g -a "Safari Technology Preview"
> ```
>
> One that already runs: `junkfather own-resource --kind simulator --id <udid> --ttl 1h` (or `--kind docker --id
> <container>`, `--kind tailscale-serve --id https:6475`). `list`, `reap`, `extend`, `hand` and `--keep-for` work
> as for processes.

## Commands

| Command | What it does |
| --- | --- |
| `own [--ttl 2h] [--keep-for owner\|session] [--label L] [--lane L] [--detach [--log F]] -- CMD...` | starts CMD in a process group of its own and records it with its owner, TTL, pgid, command and cwd |
| `own --kind simulator\|docker\|tailscale-serve\|safari-tp [--remove] … -- CMD...` | runs CMD, then owns the resource it made (it outlives CMD) |
| `own-resource --kind KIND --id ID [--remove] […]` | owns a resource that runs now; `--kind command --alive CMD --stop CMD` for any other |
| `adopt PID [--ttl …]` | records a running process; its group when it leads one, else it and its descendants |
| `list [--all] [--json]` | what runs, whose it is, its age, its TTL left, how many processes it has |
| `reap --id ID` / `--owner SESSION\|AGENT [--agent A]` / `--lane L` / `--pid PID [--force]` | ends entries: SIGTERM, a grace period (`--grace`, 10 s), SIGKILL; `--expired`, `--dry-run`, `--json` |
| `hand ID --to SESSION\|self` | gives an entry to another session |
| `extend ID --ttl D` | a new TTL from now |
| `sweep [--dry-run] [--json] [--quiet]` | reaps expired and orphaned entries, reports unowned agent processes and unowned simulators, containers and serve routes |
| `sweep --plist` | the hourly launchd agent |
| `hook` | the Claude Code / Codex lifecycle hook (JSON on stdin) |
| `status --json`, `capabilities --json` | the snapshot and capabilities for [Godfather](../godfather)'s dashboard |

The owner is the Claude Code session (`CLAUDE_CODE_SESSION_ID`, its process `CLAUDE_PID`), the subagent that ran
the command when the PreToolUse hook saw it, and the lane (`LANEFATHER_LANE`, `JUNKFATHER_LANE`, or the lane
records whose worktree holds the cwd). In Codex it is `CODEX_THREAD_ID` and the nearest `codex` process; outside
an agent, the process that ran junkfather. The rules, the state format and the boundaries with Slotfather and
Lanefather are in [docs/reference.md](docs/reference.md).

## Install

```sh
git clone https://github.com/maxvyshniakov/fathers ~/fathers
python3 ~/fathers/install.py junkfather --launchd --mastack
```

`install.py` puts the command in `~/.local/bin/junkfather` (a release under `~/.local/share/junkfather`), and with
`--launchd` loads `com.junkfather.sweep` (hourly and at load). `--mastack` adds junkfather to the `lifecycle`
settings of [Mastack](https://github.com/maxvyshniakov/mastack)'s hooks (`~/.config/mastack/hooks.json`) and runs
Mastack's installer, which registers `SubagentStop`, `SessionEnd` and `Stop`, and the PreToolUse observer that
tells junkfather which subagent ran a command. Without Mastack, register `junkfather hook` for those events in
`~/.claude/settings.json` yourself.

**Codex** (0.165) has the same hook events (`SubagentStop`, `SessionEnd`, `Stop`, `PreToolUse`) and JSON contract,
but each hook must be trusted in `/hooks` before it runs, so the installer does not register it. To use it, add
`junkfather hook` for those events to `~/.codex/hooks.json` and trust it. Without hooks a Codex owner's entries end
at their TTL or when the sweep finds the Codex process gone.

## Develop

```sh
slotfather run -n 1 --priority batch -- python3 -m unittest discover -s tests -v
slotfather run -n 1 --priority batch -- python3 tools/check.py   # Ruff and Pyright, pinned, through uvx
```

Tests use real child processes, a scratch state directory and a fake session registry; they never read the real
`~/.local/state/junkfather` or `~/.claude/sessions`. Changes a user would notice get a `CHANGELOG.md` entry.

MIT License.
