# P5: reproduction, minimization and fidelity for extension environments

`ghostlab.replay` is generic over `ghostlab-extensions/1`: no world logic,
no model calls, no network. Pydantic lives only at the capsule-envelope
boundary (`inspect`, `export_v3`); the core (`recorded`, `minimize_ext`,
`fidelity`) is standard library only.

## Three distinct outputs

| Mode | Function | Claim |
|---|---|---|
| Evidence inspection | `inspect_capsule` | read-only envelope facts; never executes |
| Recorded-action replay | `replay_recorded` | the recorded evidence reproduces byte-identical |
| Fresh-agent retest | `retest_fresh` | a new experiment under the same scenario |

A retest result carries `replay_equivalence_claimed: false` always: fresh
inference or a changed script may take a different trajectory. The UI must
render these as three outcomes, never one vague "fixed" badge.

Inspection is checksum-first and registry-free. A capsule that cannot
replay says why: `retest` kind, `truncated: true`, or a named list of
external dependencies. Unknown worlds fail closed at replay resolve time.

## Failure witness

`failure_witness` is the failed invariants plus their cited evidence
(event ids, observed/limit/unit), sorted. `minimize_faults` keeps the SAME
witness: a subset that fails the same invariant with different evidence is
`not_reproduced`, never a reduction. Capsule envelopes carry verdict names
only; live minimization compares full evidence. Semantics mirror
`diagnostics.minimize` (an exhausted trial that still shows the failure
counts as reproduced; only non-matching exhausted trials are inconclusive).

## Fidelity reports

`assess_fidelity` measures repeat determinism (up to 10 repeats),
recorded-action replay, snapshot round-trips (probed only when declared),
and diffs against a developer-supplied reference trace/state. Declared
`tested`/`approximations`/`omissions` lists are echoed verbatim and never
presented as measurements. A P2 qualification report may be attached and is
echoed under `qualification`. Missing evidence is reported, never a pass.

## Envelope boundary

`export_v3` seals pack-world evidence only: the v3 contract names the
frozen `PackVersion` set, so non-pack external worlds (for example
`reservation-world/1`) cannot be enveloped until world versions become
extensible. That is an explicit M00 follow-up; nothing here invents a
world version. `export_refund_run_v3` envelopes refund-world/1 runs via
the lossless v1/v2 mapping; `faults_fired` is required and must come from
the environment's firing detection, never the bare schedule.

World bindings map `WorldScenario` to reset shapes (`world_refund`
`scenario_from_world` for refund); replay takes the reset shape as an
opaque mapping.

## P6 interfaces

All reports serialize via `to_dict()` to JSON: `CapsuleInspection`,
`ReplayResult`, `RetestResult`, `MinimizeReport` (with a `regression` case
for CI), and `FidelityReport`. No live model calls, no secrets and no
credentials are involved anywhere in this package. Model, provider and
endpoint labels are recorded from target info as plain data and never
used to call anything.
