# Offline failure minimizer

Experimental, additive diagnostic feature. Based on integration `a3e8188`.
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

For disclosure failures, every originally unpermitted recipient must remain a
witness. A failure affecting someone else is not accepted as the same reproducer.
Other invariants are matched by name within the pinned world/fixture.

Supported inputs: refund v1 capsules and journey v2 capsules, using registered
deterministic adapters. Refund v2 wrappers and service-relay are not supported in
this first version. Arbitrary uploaded agents, live endpoints and code are never run.

## Run it

From a checkout containing this branch, with `src` on Python's module search path:

```text
python -m ghostlab.diagnostics path/to/failure.json --invariant delivered_messages_permitted --out artifacts/local/reduced-case
```

`--out` must be a new directory. There is no overwrite or cleanup command. For a
refund capsule choose, for example, `--invariant total_refund_within_payment`.

Defaults: `--max-runs 16`, `--deadline-ms 5000`. Run limits include source replay,
fresh-adapter validation and the exported capsule's confirmation replay. Clock
limits are checked between bounded deterministic executions; this is not process
isolation or a hard runtime kill. An individual run can finish after that deadline.

The command requires only Python's standard library and the existing simulation
modules. It reads no provider credential and sends no model request.

## Outputs

- `original-capsule.json`: exact source bytes.
- `report.json`: all attempted subsets, their outcomes, limits and minimality claim.
- `minimal-capsule.json`: written when a candidate is exported for confirmation.
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
| `run_budget_exhausted` / `deadline_exceeded` | Search/confirmation is incomplete; no confirmed regression case is emitted |

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

## Recorded validation

Windows, Python 3.13.2, existing environment:

- `pytest tests/diagnostics tests/simulation tests/test_core.py`: **270 passed,
  6 subtests passed**. Includes original preservation, checksums, source-adapter
  mismatch, same-recipient matching, budget/deadline stops, baseline failure,
  truncated negative trials, no-overwrite behaviour and both supported worlds.
- The actual CLI ran with `python -S`, with no site packages available.
- The three-fault journey example reduced to `sharing_revoked_while_queued` alone,
  preserving `participant-c` as the witness, in **7 counted local executions**
  including input validation and final replay. Both irrelevant faults were removed.
- The reduced capsule reproduced and the command reported **0 model calls**.

Output for the handoff is outside the checkout at
`outputs/failure-minimizer-demo-2026-11-01/reduced/` in the Codex workspace.
Those example files are generated evidence, not a claimed live-model experiment.
