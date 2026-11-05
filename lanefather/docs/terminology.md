# Terminology

## Boundary with Slotfather

A **lane** tracks repository work. A **claim** declares its intended path scope.
A **slot request** asks Slotfather for machine capacity; a **lease** grants it.
One lane can make several slot requests across checks, reruns and post-merge
verification, and can exist without holding a lease. The association travels in
`meta.lane`; the lane ID is not a request or lease ID.

The **lane cap** limits planned and active lanes. It does not reserve Slotfather
slots. Landing stops a lane from counting toward that cap, even when review or
cleanup remains. Only execution under a lease occupies machine slots.

Claims coordinate changes rather than lock files: declared dependencies allow
some overlaps, and checking a lane widens its claims to cover changed paths.
Configured shared paths are excluded from that widening. These checks do not
prevent an editor from writing a file.

## Canonical names

| Use | Distinguish from |
| --- | --- |
| Lane | An agent, branch, worktree, ticket or slot request: each has its own identity and lifetime. |
| Planned / active | Exact lane states. Say **planned or active** for the group counted by the lane cap. |
| Not done | All retained lane records, including landed and released; the default list shows this group. |
| Ranked queue | Repository work awaiting registration; separate from Slotfather's queue of slot requests. |
| Ready entry | Eligible by queue rules; registration still checks the lane cap, disk and host load. |
| Claim | A declared path scope, rather than an exclusive file lock. |
| Receipt | Evidence of a verification run, rather than the verification plan or a verified-tree stamp. |
| Post-merge smoke | Verification after landing; distinct from the batch gate before promotion. |
| Lane review / overall review | Review of one lane / review of a shipping range. |
| Released lane | Cleanup deferred because the worktree remains locked; unrelated to a software release. |
| Worker budget | Verification parallelism, rather than lane count or process count. |

A green receipt need not mean every first attempt passed: failure classification
can accept pre-existing failures or failures that disappear when rerun alone.
The receipt records that evidence. Likewise, **landed** means merged; it does not
promise green post-merge verification, finished review or completed cleanup.

## Compatibility

Command names, options, environment variables, configuration keys, lane statuses,
record and receipt fields, the queue format and the landing-lock format retain
their existing names. Schema changes follow the
[state compatibility policy](../README.md#state-and-its-versions).

In `list --json`, the historical `active` count includes both planned and active
lanes; the per-lane `status` remains exact. `jobs` fields and `--slots` / `--jobs`
options retain their names for the verification worker budget. Runtime messages
and help use the precise terms above. Historical fixtures remain unchanged.

The dashboard calls a lane's lifecycle **stage**, the verification evidence
**receipt**, and a particular execution's role **phase**. The version-1 summary
retains `status` and `checks` as wire keys; these do not introduce other concepts.
