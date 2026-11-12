# Lifetime lane admission query (v1)

`slotfather lane-admission --json` reads one JSON object from stdin (at most
65,536 characters, then EOF). It samples the host and returns one JSON object.
It never reads or creates Slotfather state, takes queue locks, reaps leases,
reserves slots, or writes history, estimates, ordering or capacity settings.
This is a lifetime-policy decision, independent of `advise`'s worker budget
and execution admission. Existing `status`/`advise` query paths are not suitable
implementations because they can reap queue entries.

## Request

```json
{"v":1,"kind":"lane-lifetime","scope":"/path/to/registry","occupied":5,
 "cap_override":null,"sample_overrides":{}}
```

- Required: integer `v:1`, exact `kind:"lane-lifetime"`, nonempty `scope`
  (at most 1,024 characters), nonnegative integer `occupied`. Booleans are
  not integers. `scope` is an opaque caller identity, echoed without reading it.
- `cap_override` defaults to null. Otherwise it is exactly
  `{"value":"7","source":"LANEFATHER_CAP"}`: raw text up to 256 characters,
  and nonempty provenance text up to 200. Positive whole-number text overrides
  the derived cap; empty text leaves it derived; other nonempty text returns
  warning `invalid_cap_override` and keeps the derived cap. This preserves
  the caller's existing typo-tolerant CAP semantics.
- `sample_overrides` defaults to `{}`. Optional keys `cores`, `memory_gib`,
  `load15` each take exactly `{"value":12,"source":"caller-resolved override"}`.
  Cores are integers 1 through 2,147,483,647; memory/load are finite nonnegative
  numbers. Provenance is nonempty text up to 200 characters. These explicit
  inputs support callers' existing configuration and deterministic acceptance;
  they are not authenticated host observations. Production callers own their
  override authorization. Unknown fields, duplicate keys, nonfinite numbers,
  invalid types and unsupported versions are technical errors.

No Slotfather execution environment settings affect these facts. In particular,
`SLOTFATHER_CORES`, `SLOTFATHER_MEMORY_GIB`, `SLOTFATHER_LOADAVG`, cgroup limits
and saved execution capacity do not change lifetime admission. Caller adapters
resolve their own aliases/conflicts and pass normalized values/provenance;
Slotfather does not learn Lanefather environment names.

## Response and policy

For 12 logical cores, 32 physical GiB, load15=18 and occupied=5:

```json
{"v":1,"kind":"lane-lifetime","policy":"lane-lifetime-v1","available":true,
 "scope":"/path/to/registry","occupied":5,"allowed":true,"refusals":[],
 "cap":6,"derived_cap":6,"cap_source":"derived","cap_override":null,
 "warnings":[],"load_limit":18.0,"sampled_at":1793923200.0,
 "host":{"cores":{"value":12,"source":"physical"},
         "memory_gib":{"value":32.0,"source":"physical"},
         "load15":{"value":18.0,"source":"physical"}}}
```

`sampled_at` is Unix wall-clock seconds for this query, not a reservation or
validity promise. `host` facts include explicit override provenance, `physical`,
or `unknown_fallback`. `cap_source` is `derived` or the supplied override source.
The echoed `cap_override`, derived cap and warnings retain the decision inputs.
Callers must check version, kind, policy, availability and echoed request
identity/count before trusting a response. Additive response fields may be ignored.

Policy `lane-lifetime-v1` preserves:

```text
derived_cap = max(1, min(logical_cores // 2, floor((physical_GiB - 8) / 4)))
refuse cap when occupied >= effective_cap
refuse load15 when raw_load15 > 1.5 * logical_cores
```

Equality at the load threshold allows admission. All applicable refusal codes
(`cap`, `load15`, in that order) are returned; `allowed` is true only when none
apply. A valid policy refusal returns **exit 0**, `available:true`, `allowed:false`.
There is no job budget, queue capacity, lease, cross-project registry or force
permission in the response.

Host CPU uses `os.cpu_count`; unknown falls back to1. Physical memory uses
`SC_PHYS_PAGES * SC_PAGE_SIZE / 2**30` independently of cgroups; unavailable
memory falls back to0GiB, preserving conservative minimum cap1. Both fallbacks
are identified as `unknown_fallback`. Load15 is unrounded `os.getloadavg()[2]`;
failed or invalid load inspection is unavailable, not zero load.

Technical failures return **exit 75**, e.g.:

```json
{"v":1,"kind":"lane-lifetime","available":false,
 "error":{"code":"host_unavailable","message":"host facts unavailable"}}
```

Other codes: `invalid_request`, `unsupported_version`. There is no `allowed`
field on error. Incorrect CLI options return exit2 on stderr. A missing command,
caller timeout, invalid/unknown response or nonzero status is unavailable; never
interpret it as policy permission or substitute `advise.jobs`. Callers must
bound subprocess runtime; the reader bounds size, not a producer's EOF delay.

## Caller ownership and rollout

Lanefather retains planned-plus-active counting in its configured Store,
registration locking, workflow/dependency/claim checks, lifecycle records and
force audit. Count and query immediately before persistence under the same
state lock; the stateless answer alone cannot serialize competing starts.
Planned→active counts once; landed/released/done do not count. Independent
Stores retain independent count scope. Broader machine-wide atomic lifetime
registration would require a different durable broker contract.

Use one adapter for registration, start/next--start and Project.readiness.
Lanefather's opt-in provider uses this contract for all three, including registration's
strict `>` load comparison at readiness. Strict registry parsing must reject ambiguous
duplicate fields as well as invalid identities/statuses to avoid corrupt record undercount.
The consumer retains its legacy provider until explicitly activated for its registration group.
Disk remains project policy: existing home-filesystem 150 decimal-GB guard is
not moved or changed. A force reason can audit/bypass known cap/load/disk
refusals according to the caller's rules, never claim overlap or an unavailable
query. This query does not authorize changing that ownership.

This additive command does **not** make current main a safe drop-in upgrade for
all live receivers. At readiness inspection, main `5f76d1b` required canonical
priority/ticket fields and emitted status/procs v2, while installed release
`49b50258…` (reported commit446575e, dirty build provenance) accepted legacy and
canonical request forms and emitted v1. Receiver rollout requires separately
approved caller inventory/cutoff, draining live old-format clients with PID/start
identity validation, supported history conversion and retention of immutable
releases. An empty queue snapshot is not a future-caller cutoff. Do not install
strict main merely to obtain this command.

An additive bridge can avoid a breaking receiver cutover: prepare a new immutable
copy of the current compatible payload with only this command, its dispatch and host
sampling helpers added. Preserve its existing execution CLI, status/procs versions,
lease identity and memory hotfixes. Record both the original payload digest and the
query revision in the new source identity; never label the hybrid as clean main.
Before publishing entry points, prove in a private installation that old held and
waiting clients retain identical records and can finish, canonical clients still
work, and the query leaves execution state unchanged. Keep the original release for
running clients and rollback. This is a separately authorized installation, not a
reason to relax current main's canonical request contract or rewrite the live queue.

First compare private exact-version results in shadow mode with existing local
guards authoritative. Activate the negotiated query for the whole registration
group only after parity and receiver compatibility evidence; then remove duplicate
local cap/load arithmetic. Activated callers fail closed on unavailable/unknown
versions. Existing guards and Straylight #648 remain until installed receiver,
consumer integration and live-state compatibility acceptance are delivered.
