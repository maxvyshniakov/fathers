# Architecture pass: Straylight #667

This pass starts at `feef0de`, the reviewed extraction of Straylight's lane engine. It changes no lane,
receipt, queue or landing-lock representation. Live lanes on older branches keep using those files.

## Survey and deletion test

| Candidate | Strength and decision | Locality and deletion test |
| --- | --- | --- |
| Worktree retention split between `done`, `worktrees` and Straylight's janitor | Strong: move generic collection into `gc.Project.run`; keep the lane completion workflow and its extra review/ticket conditions in `done` | Deleting the collector would copy retention, process verdicts and branch decisions into every project's janitor. Build-cache and scratchpad knowledge stays with the project. |
| Reviewer execution mixed with lane/receipt workflow in `review` | Strong: `reviewer.Reviewer.run` owns model selection, CLI adapters, fallback and answer interpretation; `review` owns receipts, marks and GitHub | The same receipt workflow needs every fallback rule, but none of those rules needs lane records. Deleting the reviewer module puts 300+ lines of process/provider policy back among review-state transitions. |
| Ambient config/state in reusable operations | Strong at the new callers: explicit `Config` in collector/reviewer; `state.Store` binds directory and state lock | New operations can handle two projects in one process. Deleting Store repeats state-path resolution and locking; command wrappers retain the CLI's current-project convention. |
| Whole-package `Config` plumbing and typed records | Explored, not claimed complete: the new modules establish the value interface; existing command migration belongs with the remaining check-contract work | A flag-day rewrite across every command while old state writers run would widen this pass substantially. `Record` remains the existing dictionary representation, not a pretend migration to typed records. |
| `gitops`, `journal`, `errors` | Retained: small does not imply pass-through | Removing them repeats Git refusal context, ledger syntax, or the domain exception across callers. No additional wrapper hierarchy is introduced. |
| Git/clock dependency interfaces everywhere | Rejected: ADR-0001 | The real CLI tests already use real scratch repositories and controlled inputs. A second hypothetical interface would add caller obligations without replacing an actual adapter. |
| `review` ↔ `done` ↔ `lanes` workflow calls | Explored: keep the workflow calls in this pass | Replacing the mutually coordinating commands with an event dispatcher would move, rather than remove, their sequencing contract. The independent reviewer no longer participates in those imports. |

## Design it twice

### Collection

1. A pure plan plus an external executor: easy to inspect, but callers must revalidate the plan, processes and
   Git state in the right order. This exports deletion safety to every caller.
2. A `Project` value with one `run(dry_run)` operation: the collector owns observations, the state lock, a
   second retention check and execution. **Chosen.** JSON reports remain observational output, not reusable
   permission to delete later.
3. A generic hook dispatcher: makes every project describe retention and Git logic again. Rejected for
   duplication; only project cache cleanup is a hook.

Dependencies: `Config`, records and paths are in-process; Git has real local scratch repositories; the owned
slotfather remote surface is its versioned CLI (`procs`, `sweep`), with a local executable stand-in. The older
project process hook remains an explicit adapter while old branches still use it. No process table or lease
inspection is implemented in lanefather. Unknown observations retain a worktree. The new default process adapter
uses slotfather when no project hook is configured.

### Reviewer

1. Move the existing free functions into a new file: reduces file size but leaves ambient configuration and a
   long sequence for the caller to learn.
2. A configured reviewer with `run(label, checkout, Request, output, log) -> Answer`: **chosen**; the request
   contains frozen review context, and the answer preserves selection/fallback evidence even on failure.
3. A provider protocol for every model: duplicates selection and fallback rules in callers. Two actual CLI
   adapters already exist; they remain private to the reviewer.

Dependencies: project/model settings and request data are in-process; the read-only checkout is local; Codex and
Claude are true external processes with executable stand-ins in the contract tests. Lane state, GitHub writes,
receipt finalization and the landing workflow are absent from this module.

## Interfaces before and after

Counts are public top-level functions/classes (names not beginning `_`), measured with Python's AST. A class's
constructor/methods and request fields are stated separately so a class does not hide interface size.

| Module | Before | After |
| --- | ---: | ---: |
| `review` | 44 | 32 |
| `reviewer` | 0 | 3: `Reviewer`, `Request`, `Answer` |
| `state` | 11 | 12: adds `Store` (`directory`, `lanes`, `locked`) |
| `procs` | 6 | 10: adds the default CLI adapter and its process/error values; hook adapter stays compatible |
| `gc` | 0 | 3: `Project`, `Result`, CLI `collect` |

`Reviewer` has constructor/config, `models`, and `run`; `Request` has standards, subject, base, tip;
`Answer` has evidence and error. `Project` exposes only constructor and `run`; its result contains removals,
retained paths/reasons, errors and freed bytes. Existing CLI flags remain unchanged; `gc` adds only `--dry-run`
and `--json`. The reviewer state-owner file shrinks from 847 to 571 lines; the reviewer adds 347 lines, including
its explicit request/result interface and failure evidence. This is a responsibility move, not a line-count claim.

## Slotfather alignment

Both tools keep standard-library packages, immutable source installations, CLI-only integration, versioned JSON,
and PID plus start-time identity. `SlotfatherSource` uses `procs --json --cwd`, checks unknown cwd/lease membership,
keeps command-line-only references, and preserves legacy behavior only when the CLI explicitly declines `procs`.
An inspection failure or unknown response schema retains the worktree. The standalone adapter deliberately refuses
PID-only live stopping: the installed receiving CLI cannot carry an earlier `(pid, start)` identity through its
actual signal boundary. A preflight identity query cannot close that race. This is an external API blocker on
Straylight #667, not a claim that a new stop flag exists. The existing configured process hook remains compatible,
including `stopped`, `killed` and `gone` results. No slotfather source or installed command is modified by this pass.

Collection re-reads the worktree entry and runs the complete retention policy after process cleanup. A termination
handler changing a manual lock or switching to a clean unmerged detached HEAD therefore retains the worktree. The
collector also retains its invoking cwd and process-hook ancestors using a candidate, even though the lower-level
query intentionally omits query ancestry. These cases have real-CLI scratch Git regressions.

## Deployment order

1. Verify and publish the lanefather commit by its owning repository's rules; install that exact source.
2. Verify its real lane cycle in a scratch repository and current Straylight compatibility tests.
3. Only then deliver the Straylight janitor/config delegate. The delegate refuses an absent/outdated `gc` with a
   diagnostic and no deletion. It does not silently fall back to a second implementation.
4. Existing Straylight lanes keep their current engine and hook. Switching the default engine and retiring the
   legacy `tools/lane`/process scan are separate acceptance scopes of Straylight #648, not claimed by this pass.
