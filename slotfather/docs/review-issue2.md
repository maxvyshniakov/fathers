# Issue 2: lifetime admission integration and delivery acceptance

Reviewed against Slotfather base `f628f4d645fb5cbd0402428168dc02a2340ef708`
and Lanefather base `049d7c5eb42fece0d1e666aa5302cda090856ced` on 2026-11-07.
Slotfather query commit: `c76fef30194fdc015aaba9287171354f97d5fa9f`.
Lanefather consumer including the review correction:
`b93d08995835a5f43e4840b7e5dd8ec54e4c0660`.

The source implementation and private installed acceptance are prepared. Publication,
live installation and Straylight provider activation are separate delivery steps;
this report does not claim them or close the live adoption requirement.

## Independent review

The requested Claude Opus reviewer could not run because its weekly limit was
exhausted. Two fresh OpenAI-family contexts performed separate blind passes over
the fixed diffs, owning standards and contract. This is a same-family fallback,
not cross-family assurance. Neither reviewer edited code or ran gates. There was
one correction round and no second review of that correction.

### Standards pass as returned

No findings. No actionable documented violations or judgment-call findings.
Lanefather has no `docs/architecture.md`; its domain, terminology and compatibility
guidance were reviewed instead.

### Spec pass as returned

One P2: strict registry reads still accepted ambiguous duplicate status fields.
Requirement: "require strict registry parsing to avoid corrupt record undercount";
caller-owned claims must also remain non-bypassable. A record with both
`"status":"active"` and `"status":"done"` was interpreted as done, excluding
it from occupancy and claim checks. Strict reads must reject duplicate fields
before interpreting identity or status.

Disposition: fixed in Lanefather `b93d089`. The new public readiness/registration
regression failed with `LaneError not raised` before the fix, then passed. Strict
reads reject duplicate fields without rewriting the record. The explicit legacy
non-strict reader retains its previous behavior.

Pass totals: standards 0; spec 1, worst P2, fixed.

## Compatible receiver bridge

Current main's canonical execution protocol cannot replace all live legacy
callers merely to obtain the additive query. A private bridge was reconstructed
from the exact installed compatible source with its issue-4 memory hotfix:

- Original source digest:
  `26e4484299e715e4928663a85ae4c2d5a4a0efb4723f6d685ac43c04db5557a2`.
- Candidate source digest:
  `c9cea3332952e3f3b358439b2b316681617650891b740826e88baf57e8752e5b`.
- Runtime delta: only the new `lane_admission.py`, its CLI dispatch and three
  additive host-sampling helpers in `platform.py`.
- Execution parsing, scheduling, state, leases, process identity and status/procs
  v1 code remain byte-for-byte unchanged. The original installer is unchanged.
- Version metadata retains `446575e+dirty`, the memory-hotfix provenance, the
  original payload digest and exact query revision. It does not masquerade as
  clean main.

A separate read-only deployment review identified one concrete gap: preparation
alone checked the live base digest, so a later direct install could replace an
intervening receiver update. Fixed in the deployment wrapper: default invocation
is read-only; an explicit apply revalidates the installed identity, exact runtime
bytes and candidate digest under the existing installation lock before the
original installer can publish. A stale-base acceptance case proves refusal
without changing the entry point or registered requests. No second review of
that correction is claimed.

Private real-process acceptance passed: an old held request and an old waiting
request retained identical registration bytes across installation and both
finished using their original immutable clients. A new canonical request ran;
status/procs remained v1; lifetime advice did not alter request records. No
running job was stopped to force the transition.

## Validation

- Slotfather focused lifetime contract: 10 tests passed.
- Slotfather full macOS suite: 351 tests, OK, 4 expected platform/root skips.
- Slotfather Ruff and strict Pyright: no new diagnostics; 4,828 existing,
  0 resolved; no baseline increase.
- Lanefather focused admission tests, including the real Slotfather CLI: 15 passed.
- Lanefather installation regressions on the committed source: 4 passed.
- Lanefather final full gate on committed `b93d089`: 231 tests in 385.758 seconds,
  OK, no skips. The real Slotfather cross-repository test was explicitly enabled.
- Lanefather Ruff passed; strict Pyright retained its 346-diagnostic baseline.

The first full Lanefather run finished with 231 tests and two failures: the
installer correctly refused the then-uncommitted runtime correction. Those
failures were retained, not waived or counted as green. The committed correction
was followed by the focused installation checks and a new full gate.

Installed-to-installed acceptance used private installation roots and scratch
repositories/registries. It proved actual CLI `start`, `next --start`, `list` and
installed `Project.readiness`, load-threshold equality, planned/active counting,
exclusion of landed/released/done, force auditing, non-bypassable claims and
unsupported-response refusal. The actual Straylight `tools/lane` wrapper was
copied unchanged into the fixture: it refused the old exact-source pin and
accepted the candidate commit/payload pin with the Slotfather provider enabled.

A read-only shadow comparison also used the actual Straylight configuration and
live registry through the privately installed provider. Both providers saw six
planned/active lanes, derived cap six and the same refusal to register another
lane. This is parity evidence from that observation, not live activation or a
reservation. It did not change the registry, configuration or installed commands.

Machine-local artifacts, scripts and full final-gate log live under
`~/.local/state/slotfather-2-delivery-20261107/`. The main records are
`bridge-preparation.json`, `bridge-acceptance.json`, `installed-integration.json`
`live-shadow.json` and `activation-manifest.json`. The activation patch changes only Straylight's
Lanefather pin and `[admission] provider`. It was checked for applicability but
not applied to the live checkout.

No new Linux CI run or real registration-group activation is claimed. Follow the
exact published revisions through CI and retain the issue until authorized live
installation, pin/provider adoption and actual readiness/start/next acceptance
have completed. Keep the older immutable releases for existing clients and
rollback; do not rewrite the live execution queue or stop foreign jobs.
