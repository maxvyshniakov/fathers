# Results by tree and plan

A check result belongs to a **tree and a plan**, not to a branch or a commit (owner rule, fathers#91). When the
same tree has already passed the same plan anywhere on this machine, Lanefather does not run it again: `check`,
the post-merge smoke of `land` and the batch gate of `promote` look the key up before they run, and each reused
result names where it came from.

This page is the interface other tools build on: Repo-Kit's pre-push gate and CI templates (repo-kit#14) call
`lanefather results`; nothing else in Lanefather's state is an interface.

## The key

| Part | Value |
| --- | --- |
| tree | `git rev-parse <commit>^{tree}` |
| env | `sha256` of `{"toolchain": T, "v": 1}` as canonical JSON (sorted keys, no spaces); `T` is the resolution of the project's `toolchain` command, or `null` when the project pins none |
| plan | `sha256` of `{"checks": C, "env": E, "v": 1}` as canonical JSON; `C` is the sorted list of check ids, or the string `"full"` for the full gate |

The tree holds every file a check reads, its configuration (`.lanefather.toml`, the verify adapter, lockfiles)
included, so a changed configuration is a changed tree. `env` covers what the tree does not hold. Consequences:

- same tree, same plan: reused;
- same tree, a plan with a check that never ran there: that check runs (`lookup` answers not verified);
- same tree, another toolchain: nothing is reused;
- another tree: nothing is reused by key. (`check` still reuses within a lane, and a smoke reuses a check whose
  inputs the merge did not change; both are the plan's own reasoning, recorded as such.)

A check's newest result on the tree decides: a pass recorded after a failure counts, and a failure recorded
after a pass hides that pass. A result a run took from another tree (an earlier tree of the lane whose change the
check does not read, or the lane's receipt standing for part of a smoke) is recorded as inferred, with its origin:
Lanefather's own runs reuse it, a result the check produced on the tree always wins over it, and `lookup` never
counts it. The full gate is verified only by a green full-gate entry, because the gate's check list is the tree's
own.

The post-merge smoke also reuses a pass of the previous tip's tree for a smoke check the merged diff's plan does not
select (fathers#94): the merge adds only the lane's changes, so that check reads the same files on both trees. Such a
result is inferred (`reused_by: "previous-tip"`); a full merged plan, or a tree whose plan names whole suites,
reuses nothing this way.

`promote` skips the batch gate by the tree's stamp, as before: the project's pre-push hook requires the stamp, which
only the gate writes. The store records each gate's result, and a promote of a verified tree names the gate that
passed it.

## Commands

All commands run inside the repository and resolve `REV` (a commit, branch or tree) there. For a project with a
toolchain pin, the checkout must pin what `REV` pins; otherwise the command refuses. The store is the
project's state directory, `<state>/results/<tree>.json`.

```text
lanefather results key    REV (--full | --checks A,B) [--json]
lanefather results lookup REV (--full | --checks A,B) [--json]
lanefather results record REV (--full --status green|red | --checks A=passed,B=failed) --source NAME
                          [--receipt PATH-OR-URL] [--json]
```

`key` prints the key and touches no state. `lookup` exits 0 when the plan is verified on the tree, 1 when it
is not, and 1 with a `lane:` message for an unknown revision. Its `--json` answer, version 1:

```json
{"v": 1, "tree": "<40 hex>", "plan": "<64 hex>", "env": "<64 hex>", "verified": true,
 "reason": "the full gate passed on this tree (gate, 2026-11-12T10:00:00Z)",
 "results": {"full": {"checks": {}, "full": true, "status": "green", "source": "gate", "sha": "...",
                      "receipt": "/path/to/promote-1234567890.gate.json", "at": "..."}}}
```

`results` names, for each check, the result that stands for it (`status`, `source`, `at`, and where known
`lane`, `sha`, `receipt`), or under `full` the full-gate entry. `record` stores a result a caller outside
Lanefather produced, under source `external:NAME`; Lanefather's own sources are `check`, `post-merge` and `gate`.

## For Repo-Kit

- **Pre-push gate (local).** Before the heavy suites: `lanefather results lookup <sha> --full --json`; on exit 0
  skip them and print the answer's `reason` and receipt. After a green full run:
  `lanefather results record <sha> --full --status green --source repo-kit --receipt <report path>`. Record a
  red run too (`--status red`): a newer red result keeps a stale green one from standing.
- **CI (GitHub-hosted runners)** has no local store. Its equivalent is the commit status or check run of any commit
  with the same tree: a green run posts its status, and a later run on a commit whose tree matches skips its heavy
  jobs with a link to it. To compare plans across both worlds, use the same plan string: `--full` is `"full"`,
  and the toolchain is `null` for a project without a `toolchain` command, so a runner without Lanefather can
  compute `plan` with `sha256` alone.
- A plan change (another check selection, another toolchain) gives another `plan`; never reuse across plans.

## Compatibility

New after Lanefather 0.1.0 (fathers#91). Older releases neither read nor write `<state>/results/`; a project
pinned to one keeps running every check, and the two can share a state directory. Receipts gain keys
(`reused_by`, `reused_source`, `reused_receipt`, `reused_lane` and `reused_at` on reused checks; `skipped` on a
post-merge receipt), none removed. The `v` field changes only with a migration of the store in place.
