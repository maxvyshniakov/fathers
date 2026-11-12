# Glossary

The terms the four tools share: the words that appear in more than one package, on Godfather's dashboard or in the
shared event schema. Each package keeps its own terms in its own glossary and links here:
[Slotfather](slotfather/GLOSSARY.md), [Lanefather](lanefather/GLOSSARY.md), [junkfather](junkfather/GLOSSARY.md),
[Godfather](godfather/GLOSSARY.md). Wire spellings (JSON keys, event kinds, settings keys) stay as published even where
the prose word differs; each entry says so.

## The tools

| Term | Meaning |
| --- | --- |
| Tool | A program that feeds the dashboard: Slotfather (machine capacity), Lanefather (lanes) or junkfather (processes agents start). |
| Source | A tool as the dashboard sees it: its capabilities, snapshot, event log, settings and actions. Also the event field naming the tool. |
| Value source | Where a setting's value comes from: saved, environment or project file (`cap_source`, priority source). Not a Source. |
| Capabilities | A tool's answer saying which contracts and versions it supports (`capabilities --json`). |
| Contract | A versioned interface a tool offers the dashboard: capabilities, snapshot, events, settings, actions. |
| Snapshot | One versioned JSON reply describing a tool's current state, read without changing it. |
| Action | A mutation a tool advertises for a row, run by that tool's own CLI. |
| Adapter | A project-supplied command at a fixed seam (Lanefather ADR-0001, Repo-Kit). Godfather's internal per-tool modules are not adapters in this sense. |

## Events

| Term | Meaning |
| --- | --- |
| Event | One line in a tool's event log recording a change that has happened. |
| Event log | A tool's append-only `events.jsonl`, in the shared schema ([godfather/docs/events.md](godfather/docs/events.md)), checked by [common/eventcheck.py](common/eventcheck.py). |
| Event kind | The `noun.verb` name of what happened, from the tool's documented list. The noun names the event family, not necessarily the subject type: `lease.granted` has subject type `request`, `gate.finished` has `commit`. |
| Subject | What an event is about, with a type and an id. |
| Actor | Who caused an event: an agent, a process, a user or a tool. |
| Via | The path through which an actor acted: godfather, sweep, hook, supervisor. |
| Host | The machine an event or snapshot comes from. |

## Agents and owners

| Term | Meaning |
| --- | --- |
| Agent session | One Claude Code or Codex conversation, identified by its session id (`meta.session`, `actor.session`). In prose, "session" means this. Slotfather's sweep says **session process** for a terminal, agent CLI, tmux, sshd or CI runner process. |
| Agent | The main thread or a subagent of an agent session. The event field `agent` is the subagent's id. |
| Subagent | An agent launched by another agent in the same session, with its own agent id. |
| Owner | The agent context an entry or lease belongs to: its agent session, and its subagent where that is known (junkfather adds the lane). Slotfather says **holder** for the process behind a request and **queue user** for the account that owns a queue directory. The person who owns a project is the owner only in "owner queue", "owner question" and "owner decision". |
| Holder | The process that holds or waits for a Slotfather request's lease; its process identity identifies the request. Wire: `pid`, `holders`. |
| Process identity | A pid together with its process start time, so a reused pid is another process. _Avoid_: identity alone. |
| Reap | Finish for a departed owner and free what it held. Slotfather reaps a request (or its lease) whose holder died, returning its slots without signals; junkfather reaps an entry, ending its processes by SIGTERM, a grace period and SIGKILL, also when its TTL runs out. Both appear as `reaped` in the merged feed. |
| Orphan | A running junkfather entry whose owner session is gone. Slotfather's sweep has an **orphan class** (a launchd- or init-adopted development process older than the orphan age); its `orphan` settings key keeps that name. |
| Expired | A running junkfather entry past its TTL that has not been reaped yet. Expired and orphaned are conditions of a running entry, not entry states. |
| Entry | One junkfather-owned process group or resource. Slotfather says **request** for its stored records (`entries` is wire); Lanefather says **queue entry** for a ranked-queue row. |
| Unowned process | A process in an agent's scratch that no junkfather entry holds; reported, never reaped. Lanefather's changed path that no check reads is an **unchecked path** (wire `unowned`). |
| Sweep | A periodic inspection pass. Always name the tool: Slotfather's sweep inspects leftover processes and containers machine-wide and may stop eligible ones; junkfather's sweep reaps expired and orphaned entries and reports unowned processes. |

## Capacity and lanes

| Term | Meaning |
| --- | --- |
| Slot | One unit of the machine's shared scheduling budget, managed by Slotfather. |
| Request | An attempt to acquire slots for some work; the same request while waiting, running or yielding. |
| Lease | A request's permission to use the slots it was granted. |
| Priority | A request's Slotfather scheduling priority: interactive, normal or batch. _Avoid_: class. **Served priority** is the priority it is scheduled at after ageing (wire `effective_priority`). |
| Batch | Two meanings, always qualified: **batch priority** (Slotfather's lowest) and a **batch of lanes** (the lanes landed between two full gates; **batch gate**). |
| Execution admission | Whether Slotfather would grant slots now (admission capacity). |
| Lifetime admission | Whether one more lane may be registered under the lane cap, disk and host load; it reserves no slots (`lane-admission`, Lanefather `docs/admission.md`). |
| Lane | A tracked unit of repository work, from registration through checks, integration, review and cleanup (Lanefather). |
| Stage | A lane's place in its lifecycle: planned, active, landed, released or done. Wire key `status`. |
| Claim | A lane's declared scope of paths. junkfather's internal record of which subagent ran a command is a **hook claim**. |
| Slot priority | The Slotfather priority a lane's phase runs at. _Avoid_: lane priority. |
| Released (lane) | A landed lane kept for later cleanup because its agent still locks the worktree. Not a software release. |
| Shipped | Published in a software release. `lanefather shipped <version>` comments the version on each issue the release fixes (the command was `lanefather released`). |
