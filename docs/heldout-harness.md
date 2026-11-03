# Held-out evaluation harness (runner + result contract)

The generic runner lives in `src/ghostlab/harness/heldout_runner.py`, its
maintained tests in `tests/harness/test_heldout_runner.py`. It consumes a
hidden scenario set it has never seen and emits a versioned result contract.
Standard library only: no inference, no network, no services.

## What this side owns

- The scenario-directory + manifest shape the hidden set must take (§schemas).
- The frozen exploration config + handshake that pins a run.
- The runner: executes the existing deterministic entry points
  (`run_campaign` on the fixed-scenario arm, `assess` over `refund_evidence`)
  against each hidden case with a frozen candidate.
- The versioned result contract JSON (`ghostlab-heldout-result/1`).
- The SYNTHETIC self-test fixture (`src/ghostlab/harness/selftest_fixture/`,
  namespace `synthetic-selftest`): our own dev defect set for exercising the
  verdict pipeline. It is not hidden content and can never be mistaken for
  held-out evidence (§separation).

This side never authors, invents, or bundles hidden evaluation scenarios.

## Schemas

Exploration config (`ghostlab-heldout-config/1`, exact keys, unknown fields
refuse): `config_version`, `namespace` (`heldout` | `synthetic-selftest`),
`seed`, `max_attempts` (1..1000), `action_cap` (1..1000).

Manifest (`ghostlab-heldout-manifest/1`, exact keys): `manifest_version`,
`namespace`, `cases: [{case_id, spec}]` where `spec` is a basename `.json`
file in the same directory. Held-out case ids must NOT use the `SYNTH-`
prefix; synthetic ids MUST, so the namespaces cannot collide.

Case spec (`ghostlab-heldout-spec/1`): required `spec_version`, `world`
(`refund` in runner v1), `scenario` (`timeout_after_commit`,
`max_tool_calls`, validated through `ghostlab.core.Scenario`); optional
`fixture` (validated through `ghostlab.core.Fixture`), `actor_id`, `task_id`.
`candidate_script` is allowed ONLY in the synthetic namespace: held-out specs
that embed candidate behavior are refused, so the frozen candidate -- never
the scenario author -- drives every held-out case.

Result (`ghostlab-heldout-result/1`, exact keys at every level): top-level
`contract_version`, `runner_version`, `namespace`, `freeze`
(`commit` + `rules_version`), `config` + `config_digest` + `seed`, `cases`,
`summary`, `inference_usage`. Per case: `case_id`, `verdict`
(`pass` | `violation` | `unknown`), `rules_fired` (failed rules),
`unknown_rules`, `evidence_ref` (a `sha256-trace-state/1` digest plus counts
-- never scenario content), `inference_usage` (`model_calls` is always 0; the
harness performs no inference).

## Running

Freeze first: `run_heldout(*, scenario_dir, manifest, handshake,
freeze_commit, candidate_factory, checkout)` verifies the handshake digest,
freezes the checkout (dirty trees refuse via the existing `freeze()`), and
compares the live commit to `freeze_commit` BEFORE touching the scenario
directory. Any mismatch refuses to run. The candidate factory receives ONLY
`(case_id, handle)` per case; the handle is opaque (id + seed-derived token,
nothing else) and the task params carry only the case id.

Self-test (no enforcement, informational freeze proof only):

```python
from ghostlab.harness import run_selftest
result = run_selftest()  # SYNTH-PASS-1 -> pass, SYNTH-VIOLATION-1 -> violation, SYNTH-UNKNOWN-1 -> unknown
```

`to_legacy_result()` projects a result onto the existing consumer-side
contract shape (`freeze_commit` + opaque `scenario_id`/`detected` entries) so
`ghostlab.verify.validate_result` still accepts it; `violation` maps to
detected. `unknown_rules` have no legacy spelling and are dropped there --
the full contract stays authoritative.

## Separation argument

1. The candidate's only inputs are the case id, the opaque handle, and live
   world observations during its run; the fault schedule and expectations are
   never handed over (tested: a snooping candidate recovers no scenario
   marker).
2. Namespaces are structural: `run_heldout` refuses synthetic manifests,
   `run_selftest` refuses held-out manifests, and a frozen config cannot
   drive a manifest from the other namespace -- tuning against the synthetic
   fixture cannot leak into hidden results (tested).
3. Results carry verdicts and evidence digests, never scenario content; a
   candidate failure fails that case closed to `unknown` with only the error
   TYPE recorded.
4. A purity test pins the shipped fixture file set and asserts every shipped
   case id is `SYNTH-`-prefixed under the synthetic namespace.

## Explicit limits -- what the other session must still supply

1. THE hidden scenario directory + manifest conforming to the schemas above
   (this branch ships none and must never gain any).
2. The frozen candidate: a `candidate_factory(case_id, handle) -> TargetAdapter`
   plus the frozen candidate commit to pass as `freeze_commit`.
3. Worlds beyond `refund`: runner v1 executes `refund` only (the only world
   with a P1 environment adapter). A journey (or other) world needs a
   published adapter plus a runner version bump -- out of scope here.
4. Contract reconciliation: this runner's result reuses the
   `ghostlab-heldout-result/1` version string with a richer per-case shape
   than the consumer-side `validate_result` shape; `to_legacy_result()`
   bridges them. If the owners prefer a clean split, re-version this shape to
   `ghostlab-heldout-result/2` (tracked-file edit, so not done on this
   branch).
5. Model-driven candidates self-report via `TargetInfo.model`; the harness
   records the report and counts zero harness-side inference. Metered
   model-call accounting is not implemented.
