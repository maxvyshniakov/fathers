# Lifetime lane admission

Existing configurations retain `legacy` admission. Opt in only after the installed
Slotfather receiver supports the [v1 lifetime contract](https://github.com/maxvyshniakov/slotfather/blob/3985cc0cf0a430e27df3e9cbeda0797cd9b61ea6/docs/lane-admission.md):

```toml
[admission]
provider = "slotfather"
```

`admission.evaluate` is the shared decision module for registration, CLI list and
`Project.readiness`. The active Slotfather provider owns the CPU/physical-memory
cap formula and load15 threshold. It never falls back to duplicate local policy
or `advise.jobs`; lifetime registrations reserve no execution slots. The legacy
provider alone retains the existing local arithmetic during rollout.

Lanefather counts planned+active once within its configured Store; landed,
released and done do not occupy a place. Actual registration reads strict state
and queries while holding the existing Store state lock, before persisting.
Concurrent registrations cannot both use the final place. Readiness remains a
read-only strict snapshot, without creating locks/state or changing ambient
configuration; it is advice, not a reservation. A later registration samples again.

`Config.lookup` resolves existing aliases/conflicts and passes CAP raw text and
source, plus explicit CORES/MEMORY_GB/LOAD15 samples with provenance. Slotfather
samples physical facts where no override applies. Invalid CPU or nonnumeric
memory overrides retain the legacy sampling fallback; nonfinite/negative samples
fail unavailable. Invalid nonempty CAP retains Slotfather's derived cap and emits
a warning. A conflicting alias is an error. Execution `SLOTFATHER_*` settings and
cgroup budgets are not lifetime overrides.

The installed `slotfather` is found on PATH; there is no production source path,
argv-prefix or test-environment fallback. The bounded command is exactly
`slotfather lane-admission --json`. Lanefather limits request size and combined
stdout/stderr to65,536 bytes while streaming, and query I/O to5 seconds. On failure
it kills only its newly created query process group, including children retaining
pipes; reaping has a separate one-second bound. Unknown versions/policies,
nonzero exit, malformed or duplicate JSON fields, invalid numeric types, mismatched
scope/count/CAP echo, changed sample override values/provenance and inconsistent
decisions are unavailable, not policy permission.

Disk remains Lanefather's existing home-filesystem 150 decimal-GB policy. Claims,
dependencies, lifecycle persistence and override journals also remain here. A force
reason may bypass and audit all known cap/load/disk refusals, never claim conflicts,
unreadable ownership state or an unavailable query. No Slotfather queue, history,
capacity, lease or ordering files are read or changed by lifetime advice.

Two deliberate corrections apply: readiness now uses registration's strict `>`
load comparison (equality allows); registration/list now refuse corrupt registry
records rather than silently undercounting them. Strict reads reject duplicate JSON fields and invalid
record identity/status. The state schema and existing lifecycle fields are unchanged.

## Compatibility and acceptance

This source change does not activate existing projects. At preparation time the
installed Slotfather `49b50258…` accepted legacy and canonical request fields but
emitted status/procs v1; strict source main5f emitted v2 and rejected legacy request
aliases. Adding a lifetime command does not authorize replacing that receiver.
Coordinate caller inventory, old-client cutoff/drain, supported migration and
immutable-release retention separately before opting a registration group in.
Unknown/missing query versions fail closed after activation. Old callers that
still use local policy need a coordinated group rollout; this adapter cannot fence
them. Straylight #648 and Slotfather #2 remain open until actual receiver and
consumer delivery acceptance, including mixed-client evidence.

Always-on tests use external executable stand-ins for transport/contract failures,
concurrent registration, force/claims and the readonly Project interface. Optional
cross-repository acceptance sets `LANEFATHER_TEST_SLOTFATHER_ARGV` to JSON argv for
an immutable private Slotfather source CLI. **Only tests** read that variable and
create a fixture PATH launcher; production code does not. Run it explicitly for
release acceptance and record the exact source identity. Without it, the one
cross-repository test is explicitly skipped, so a standalone package gate must not
be described as full integration proof. Private source execution is test evidence,
not an installed receiver claim or production fallback.
