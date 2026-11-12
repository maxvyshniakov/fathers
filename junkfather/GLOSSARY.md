# Glossary

- **Entry**: one owned process group, with its owner, TTL and identity. `junkfather list` shows entries.
- **Leader**: the process an entry started (or adopted); the group it leads is the entry's group.
- **Owner**: the agent session (Claude Code or Codex), subagent and lane that started an entry, or the process that
  ran junkfather outside an agent.
- **Keep**: how long an entry outlives the agent that started it: `owner` (not at all) or `session` (until the
  session ends).
- **Hand-over**: giving an entry to another session, which then owns it.
- **TTL**: the longest an entry may run, whatever its owner; `extend` renews it.
- **Reap**: ending an entry: SIGTERM, a grace period, SIGKILL, to verified identities only.
- **Identity**: a process's pid together with its start time; a reused pid has another identity.
- **Known members**: the identities seen in an entry while it ran; a reap without a live leader signals only these.
- **Claim**: the PreToolUse hook's record of which subagent ran a Bash command naming junkfather.
- **Orphaned**: an entry whose owner session is gone.
- **Unowned process**: a process in an agent's scratchpad or worktree that no entry holds; reported, never reaped.
