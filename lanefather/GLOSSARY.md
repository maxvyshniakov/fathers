# Glossary

Terms for parallel changes and their integration in one repository.
[Naming decisions and compatibility](docs/terminology.md) describe how these
terms appear in existing interfaces. Terms shared with the other tools (owner, reap, orphan, shipped, lifetime
admission, slot, lease) are in the [fathers glossary](../GLOSSARY.md).

| Term | Meaning |
| --- | --- |
| Lane | A tracked unit of repository work, from registration through checks, integration, review and cleanup. |
| Claim | A lane's declared scope of paths it intends to change. |
| Claim overlap | A path covered by the claims of more than one lane. |
| Dependency | Another lane that must land before this lane may land. |
| Stage | A lane's position in its workflow lifecycle: planned, active, landed, released or done. |
| Phase | The kind of execution performed for work, such as check, post-merge or rerun. |
| Lane cap | The maximum number of lanes in the planned or active states at once. |
| Planned lane | A registered lane without an adopted worktree. |
| Active lane | A registered lane with a worktree, before landing. |
| Landed lane | A lane whose changes have been merged into its integration branch. |
| Released lane | A landed lane retained for later cleanup because its agent still locks the worktree. Not a software release: `lanefather shipped <version>` handles those. |
| Done lane | A lane whose cleanup has completed. |
| Ranked queue | An ordered set of proposed lanes awaiting registration. |
| Ready entry | An unregistered queue entry (say queue entry, not entry, in shared docs) with no declared block, unmet dependency or conflicting claim. |
| Brief | The instructions and context supplied to the agent implementing a lane. |
| Ticket | The issue associated with a lane, when one is supplied. |
| Base | The branch or commit from which a lane starts. |
| Integration branch | The branch into which a lane lands. |
| Worktree | A Git checkout with its own working directory and index, sharing repository history. |
| Scratch | Temporary working material and evidence owned by a lane. |
| Check | An individually identified verification item. |
| Suite | A named group of checks. |
| Verification plan | The checks selected for a change and the reasons for that selection. |
| Receipt | Recorded verification evidence tied to the tree and conditions it checked. |
| Green receipt | A receipt that permits progress under the verification policy, including accepted failure classifications. |
| Stale receipt | A receipt that no longer covers the tree being considered. |
| Land | Merge a lane into its integration branch. |
| Landing lock | Exclusive permission to move shared integration or promotion references. |
| Post-merge smoke | Verification of the integrated result after a lane lands. |
| Lane review | An independent review of one lane's changes. |
| Finding | A review issue requiring a recorded fix or rejection with a reason. |
| Fix lane | A follow-up lane that addresses findings from another lane's review. |
| Batch gate | Full verification of a candidate for promotion. |
| Verified-tree stamp | Evidence that a tree passed the full verification required for promotion. _Avoid_: gate stamp. |
| Ledger | The append-only log of every lane action and override (`ledger.log`). _Avoid_: journal. |
| Lifetime admission | The decision whether one more lane may be registered under the lane cap, disk and host load; it reserves no slots. |
| Unchecked path | A changed path no check claims to read, which makes the plan the full gate. Wire: `unowned`. |
| Overall review | An independent review of the changes in a shipping range. |
| Promote | Advance publication branches to a verified candidate and, by default, push them. |
| Ship | Promote a candidate to both dev and main under the additional shipping checks. |
| Sync | Reconcile local publication branches with their remote references. |
| Worker budget | The parallelism budget passed to the project's verification runner. |
| Slot request | An application to Slotfather for machine capacity. |
| Slot priority | A slot request's Slotfather priority (interactive, normal or batch), set per phase: check, post-merge, gate, rerun, perf. _Avoid_: lane priority. |
| Slot | A unit of machine work budget managed by Slotfather. |
| Lease | Permission granted by Slotfather to occupy slots. |
