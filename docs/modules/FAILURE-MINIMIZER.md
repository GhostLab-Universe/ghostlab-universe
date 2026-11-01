# Offline failure minimizer

Experimental, additive diagnostic feature. Updated onto integration `96cac2f`.
No API, UI, contracts, provider, world or controller files are changed.

## Why it exists

A failing scenario may contain several faults, but a developer needs a small case
they can understand and keep as a regression. This command tests subsets of the
recorded faults and exports a smaller reproducer for a selected failed invariant.

The method is an application of [delta debugging](https://issta16.cispa.saarland/papers/tse2002/),
not a claim of a new algorithm. This implementation enumerates the finite subset
space by increasing fault count. The current journey world has three supported
fault types, so this is small and does not need a search framework or another model.

## What is fixed

The fixture, world version, adapter/version, actor, guarded mode, original tool cap
and the retained faults' original parameters/timings stay fixed. The original input
is snapshotted once, checksummed by its existing loader and preserved byte-for-byte.
Its evidence must replay, and a fresh run of the checked-in adapter must exactly
match the recorded run before search starts. Recorded actions alone are not proof
that a named adapter produced them.

Failure identity uses a conservative domain witness, not just an invariant name:
- Disclosure preserves every original recipient, message id, template and permission failure.
- Unapproved commitments preserve the offending commitment, approved version and booking.
- Feasibility preserves typed problem keys (late, double, overlap, etc.) plus booking/calendar
  context. A double booking cannot become a late single booking and count as the same failure.
- Refund preserves ledger identities and amounts.
Event positions may change. Extra pinned context can prevent a reduction; this is an
intentional conservative limit. It is not proof that all possible causal context was captured.
The experimental report and regression data formats are now version 2.

Supported inputs: refund v1 capsules and journey v2 capsules, using registered
deterministic adapters. Refund v2 wrappers and service-relay are not supported in
this first version. Arbitrary uploaded agents, live endpoints and code are never run.

## Run it

From a checkout containing this branch, with `src` on Python's module search path:

```text
python -m ghostlab.diagnostics path/to/failure.json --invariant delivered_messages_permitted --out artifacts/local/reduced-case
```

`--out` is atomically claimed as a new directory; a competing creator is refused. There is no overwrite or cleanup command. For a
refund capsule choose, for example, `--invariant total_refund_within_payment`.

Defaults: `--max-runs 16`, `--deadline-ms 5000`. Run limits include source replay,
fresh-adapter validation and the exported capsule's confirmation replay. Each replay
reserves two executions because the merged replay also runs its checked-in adapter. Clock
limits are checked between bounded deterministic executions; this is not process
isolation or a hard runtime kill. An individual run can finish after that deadline.

The command requires only Python's standard library and the existing simulation
modules. It reads no provider credential and sends no model request.

## Outputs

- `original-capsule.json`: exact source bytes.
- `report.json`: all attempted subsets, their outcomes, limits and minimality claim.
- `minimal-capsule.json`: published only after confirmation in temporary storage.
- `regression-case.json`: only written after the capsule reproduces. This is JSON
  data for review, not generated executable code or an automatically installed CI test.

The diagnostic JSON formats are experimental module-local reports. They are not
new canonical M00 contracts or inputs accepted by the controller/API.

## Read the result correctly

| Status | Meaning |
| --- | --- |
| `reduced` | A smaller subset reproduced; all smaller cardinalities were ruled out within this search scope |
| `already_minimal` | The original fault count was minimal among these subsets |
| `baseline_failure` | The selected failure happens with no injected faults; do not blame the injection |
| `smallest_found` | The candidate reproduces, but an inconclusive smaller trial prevents a minimum claim |
| `run_budget_exhausted` / `deadline_exceeded` | Search/confirmation is incomplete; selected_faults stays null and no capsule/regression is published. candidate_faults is explicitly unconfirmed |

A negative truncated run is `inconclusive`, never evidence that a subset is safe.
A positive observed violation remains evidence even if that run later hit its cap.
Minimality means smallest fault count for the pinned schedule and adapter. It does
not prove a unique root cause, smallest event sequence, minimum token budget or
behaviour under other timings/models. Removing a fault can change event ordering;
the retained schedule values are fixed, not reinterpreted as semantic anchors.

This is a new controlled diagnostic experiment, not free-form graph recovery or a
replay of live external services. `model_calls` remains zero by construction.

## Integration handoff

Keep it optional while M07/M08/M09 finish. It can be demonstrated from the CLI
without changing their published interfaces. A future button needs M00/M07/M08
contract review and background execution with the same project authorization as
capsule access. Do not call the file-only function directly on untrusted server
paths, expose output paths to clients or silently attach a reduction to a finding.

Validation results and the example output are recorded after the final test run.

## Review correction

The first draft reduced the privacy example from three faults to one but changed the
message from itinerary-v1 to itinerary-v2. That did not preserve the demonstrated failure.
The stricter witness retains stale memory plus revocation and removes only the delayed
update: three faults become two. The source and reduced witnesses must match exactly.
The report distinguishes unconfirmed candidate_faults from confirmed selected_faults.
Output directory creation is atomic, existing files use exclusive writes, and deeply nested
input produces sanitized CLI exit 2 rather than a traceback. Incomplete search remains exit 1.

Final validation results are recorded in the PR description. No complete external mutation
corpus was available, so no claim is made that every reviewer mutant has been killed.

Final local validation after merging M07 integration 96cac2f: 275 passed and 6 subtests
across diagnostics/simulation/core. The actual python -S command reduced the source from
three faults to two, with identical itinerary-v1 disclosure witness, in 11 counted world
executions (each successful replay counts two passes). The 14 bare core tests also pass.
The generated reviewed report is outside Git at outputs/failure-minimizer-reviewed-2026-11-01.
