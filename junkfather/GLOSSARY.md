# Glossary

Terms shared with the other tools (owner, reap, orphan, sweep, process identity) are in the
[fathers glossary](../GLOSSARY.md).

- **Entry**: one owned process group or resource, with its owner, TTL and identity. `junkfather list` shows entries.
- **Resource**: something that outlives the process that made it (a booted simulator, a container, a `tailscale serve`
  route, a Safari Technology Preview instance, a git worktree); its kind's own command checks and stops it.
- **Leader**: the process an entry started (or adopted); the group it leads is the entry's group.
- **Owner**: the agent session (Claude Code or Codex), subagent and lane that started an entry, or the process that
  ran junkfather outside an agent.
- **Keep**: how long an entry outlives the agent that started it: `owner` (until the owning agent stops) or `session`
  (until the session ends).
- **Hand-over**: giving an entry to another session, which then owns it.
- **TTL**: the longest an entry may run, whatever its owner; `extend` renews it.
- **Reap**: ending an entry: SIGTERM, a grace period, SIGKILL, to verified identities only.
- **Process identity**: a process's pid together with its start time; a reused pid has another identity. _Avoid_:
  identity alone.
- **Known members**: the identities seen in an entry while it ran; a reap without a live leader signals only these.
- **Hook claim**: the PreToolUse hook's record of which subagent ran a Bash command naming junkfather.
- **Orphaned**: a running entry whose owner session is gone. Like **expired** (past its TTL, not reaped yet), it is a
  condition of a running entry, not an entry state.
- **Unowned process**: a process in an agent's scratchpad or worktree that no entry holds; reported, never reaped.
- **Unowned resource**: a booted simulator, running container, serve route or git worktree that no entry owns;
  reported, never stopped, except a worktree that is clean, merged by content, idle for a day and empty of
  processes, which the sweep removes (owner-approved, fathers#112).
- **Daemon** (agent-browser's): a browser daemon that detached and that launchd adopted; it belongs to the entry
  whose process started its session, and is reaped with it.
- **Leftover session**: an agent-browser session's files whose daemon and starting process are gone and that did not
  change for a day; the sweep removes them (owner-approved, fathers#115).
