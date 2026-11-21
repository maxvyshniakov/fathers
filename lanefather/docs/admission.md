# Lifetime lane admission

Existing configurations retain `legacy` admission. Opt in only after the installed
Slotfather receiver supports the [v1 lifetime contract](https://github.com/maxvyshniakov/fathers/blob/5c6939f/slotfather/docs/lane-admission.md):

```toml
[admission]
provider = "slotfather"
```

`admission.evaluate` is the shared decision module for registration, CLI list and
`Project.readiness`. The active Slotfather provider owns the CPU/physical-memory
cap formula and reports a load15 threshold. Lanefather validates the full v1
answer and treats its load verdict as advice (fathers#172). The legacy provider
computes the threshold locally. Both providers report high load after successful
registration. Lifetime registration reserves no execution slots.

`start --create` runs `git worktree add`. Dependency installation and builds belong
to the project's check adapter. Its checks, gate and post-merge smoke use
Slotfather where configured. The execution adapter sizes workers for load where
it uses `slotfather advise` and waits for capacity at lease acquisition.
`LANEFATHER_LOAD_THROTTLE=1` adds Lanefather's optional load15 wait before a
verification run.

Lanefather counts planned+active once within its configured Store, among the
lanes of one repository (its Git common directory, fathers#149): a state
directory several projects share does not let one fill another's cap.
Registration counts the registering lane's repository; readiness and `list`
count the repository the command runs in, else the one the configuration's file
is in, else every lane. A record that names no repository counts for every one.
The optional `LANEFATHER_GLOBAL_CAP` ceiling counts every project's lanes,
locally, for both providers. Landed, released and done do not occupy a place. A
fix lane (`review --fix-lane`) for a landed, kept parent is admitted outside the
cap and the ceiling, in its parent's place. The disk floor and claims still apply.
That holds at registration only: once registered the fix lane counts like any
planned or active lane, while its landed parent did not (part (b) of fathers#45
changes this). Actual registration reads strict state and queries while holding
the existing Store state lock, before persisting. Concurrent registrations
cannot both use the final place. Readiness remains a read-only strict snapshot,
without creating locks/state or changing ambient configuration; it is advice,
not a reservation. A later registration samples again.

`Config.lookup` resolves existing aliases/conflicts. The cap's setting, first
set wins: `LANEFATHER_CAP`, then the project's `[admission] cap`, then the
derived cap. An exported `LANEFATHER_CAP` overrides every project's cap.
Lanefather passes the CAP raw text and source as the query's cap override, or
the project's `[admission] cap` with source `.lanefather.toml [admission] cap`,
plus explicit CORES/MEMORY_GB/LOAD15 samples with provenance. Slotfather samples
physical facts where no override applies. Invalid CPU or nonnumeric memory
overrides retain the legacy sampling fallback; nonfinite/negative samples fail
unavailable. An invalid nonempty CAP falls back to the project's `[admission]
cap` when it has one (Lanefather warns and sends that instead); without one,
Slotfather keeps its derived cap and emits a warning. A conflicting alias is an
error. Execution `SLOTFATHER_*` settings and cgroup budgets are not lifetime
overrides.

The installed `slotfather` is found on PATH; there is no production source path,
argv-prefix or test-environment fallback. The bounded command is exactly
`slotfather lane-admission --json`. Lanefather limits request size and combined
stdout/stderr to 65,536 bytes while streaming, and query I/O to 5 seconds. On failure
it kills only its newly created query process group, including children retaining
pipes; reaping has a separate one-second bound. Unknown versions/policies,
nonzero exit, malformed or duplicate JSON fields, invalid numeric types, mismatched
scope/count/CAP echo, changed sample override values/provenance and inconsistent
decisions are unavailable, not policy permission.

Lanefather owns the [home-filesystem disk floor](reference.md#finishing-lanes), claims,
dependencies and lifecycle. A force reason may bypass and audit a cap or disk
refusal. Claim conflicts, unreadable ownership state and unavailable queries
prevent registration. The lifetime query reads host facts and carries no lease.

Registration, list and readiness use strict reads that reject duplicate JSON
fields and invalid record identity or status.

## Compatibility and acceptance

Installing Lanefather does not activate the Slotfather provider for existing projects. Verify the installed
receiver's lifetime query and its independent execution/status protocols before opting a registration group in.
A compatible receiver can add the lifetime query without changing its execution clients. Preserve existing lane
records and in-flight immutable releases, coordinate old callers, then exercise actual readiness/start/next.
An unavailable or unknown query fails closed after activation; do not substitute execution job advice.

The first consumer's rollout is complete; it is not a prerequisite for a new consumer. Each new registration
group still owns its own activation.

Always-on tests use external executable stand-ins for transport/contract failures,
concurrent registration, force/claims and the readonly Project interface. Optional
cross-repository acceptance sets `LANEFATHER_TEST_SLOTFATHER_ARGV` to JSON argv for
the CLI of an immutable Slotfather source checkout. **Only tests** read that variable and
create a fixture PATH launcher; production code does not. Run it explicitly for
release acceptance and record the exact source identity. Without it, the one
cross-repository test is explicitly skipped, so a standalone package gate must not
be described as full integration proof. Source-checkout execution is test evidence,
not an installed receiver claim or production fallback.
