# Testing pipeline: base, contracts, ownership (integration owner record)

Base: `feature/byoe-config-experiments @ 5ea1a4d`. Tree clean at freeze.

## Earlier-claims verification (all checked at HEAD)

| Claim | Verdict |
|---|---|
| Qualification checks reset/isolation/schemas/evidence/fault-hooks/cancellation/reproduction | TRUE — `envs/qualification.py`, plus booking + proxy adapters qualify through it |
| Controller is refund-specific (agent + verification) | TRUE — 10 refund refs in `experiments/controller.py`; generalized by T2 below |
| Guided UI uses copied fixtures | FIXED — service-first UI merged (`114b9e2`); 88/88 live + 35/35 dead browser acceptance |
| Branch diverged from integrated API/provider stack | TRUE — 434 files differ from `65185ca`; no LangGraph, provider, replay, verify, or extensions modules on this line |

Deliberate scope decisions: no LangGraph harness is built (the deterministic
controller IS this line's orchestration; a new harness would be a competing
subsystem — the AGENTS.md baseline divergence is recorded, not silently
adopted). No capsule v3 (v1 replay exists; no new format). No held-out runner
(scenario source labels only). No UI edits (dashboard done; proof is CLI-level).

## Frozen contracts (tracks implement these exact shapes)

### Check registry (`experiments/checks.py`, new)

```python
register_check(*, check_id, version, description, params_spec,
               required_evidence, applies_to, temporal, interpret)  # code-only
evaluate(check_id, *, snapshot, trace, params, fixture) -> {
    "verdict": "pass" | "violation" | "unknown" | "not-applicable",
    "detail": str, "evidence_refs": [...]}
```

`params_spec`: `{name: (type, min, max)}`. `required_evidence`: subset of
`{"snapshot","trace"}`. `temporal`: `"stateless"` or `"historical"`.
Unknown check ids and out-of-bounds params are rejected; models and browser
input can never register or redefine checks. Run-record `checks` stay
`{rule: bool}` for backward compatibility (violation/unknown/na map to
`False` with reasons in `checks_meta` ONLY if `store.save_run` permits new
body keys — read it first; else versions ride the registry + resolve pins).

### Driver registry (`experiments/drivers.py` + per-env modules, new)

```python
register_driver(env_id, driver)  # driver = module object
driver.drive(agent, invoke, fixture) -> None            # agent procedure
driver.evidence_complete(evidence, dispatches) -> bool
driver.fault_fired(hook, trace) -> bool
driver.verify_checks(fixture, snapshot) -> {rule: bool}
driver.declared_checks() -> [check_id, ...]             # for applicability
driver.agents() -> ["vulnerable", "repaired", ...]
driver.required_tools(agent) -> [tool, ...]             # owner amendment:
   # resolve pulls per-agent tool needs from the driver (refund values moved
   # verbatim); envs without a driver keep the legacy refund table.
```

Refund driver moves to `experiments/drivers_refund.py` (same outcomes,
byte-identical traces). Booking driver lives in
`examples/equipment_booking/driver_booking.py` (imports `.world`/`.checks`
relatively; core never imports examples). `controller.execute_run` dispatches
by resolved `env_id`; legacy `drive_agent`/`fault_fired`/`evidence_complete`
names stay as thin refund delegates for existing tests. `configs.resolve`
rejects rules outside `declared_checks()` for the env (unsupported-check
rejection) and undiscovered hooks (already does).

Owner amendments (validation generalization, needed for booking resolve):
`required_tools(agent)` is a required driver method and resolve pulls
per-agent tool needs + agent support from the driver (refund values moved
verbatim; driverless envs keep the legacy table). Fixture/rule validation
at create-time dispatches on env: drivers MAY implement
`validate_fixture(fixture)` (booking does; refund falls back to the legacy
shape); allowed rules come from `declared_checks()` (refund message
byte-identical). `FixtureSpec` holds validated key/value pairs so digests
round-trip for any env shape (refund digests unchanged). Shared
`unique_idempotency_keys`: first registration wins, booking skips
already-registered ids (identical interprets).

### Planner (`experiments/plan.py`, new; public `ops` only, no core edits)

Plan: env/fixture/agent/task/checks/fault-schedules/limits/repetitions/
strategy/evidence-reqs. Strategies: `fixed` (requested configs as-is),
`stateful_random` (seeded stdlib expansion over discovered tools/hooks,
bounded count, no LLM), `model_guided` (Explorer `propose(findings) ->
[proposal]` interface; EVERY proposal re-validated as a requested config,
invalid never executes; scripted Explorer only — zero live calls).
`expand_plan()` yields concrete `(requested configured, repetition, attempt)`
units run through `start_run`; planned run count, coverage and unsupported
conditions are reported BEFORE execution. Per-fault records keep
required/optional + trigger/target + applied/not-reached/unsupported/failed.
Scenario records carry source `dev` or `heldout`; heldout execution is
refused (no owner interface exists).

### Fidelity report (`fidelity/report.py`, new)

Assembles manifest + divergence + determinism evidence into one assessed doc:
behaviors modeled / approximated / unsupported ops+faults / determinism
limits / reference comparisons / known mismatches. NO universal score.

### Requalification policy

Qualification binds adapter_ref + code digest + manifest + runner version.
Execution re-verifies status + digest (exists). Any code change requires
re-register + re-qualify + re-resolve; the service/execute path refuses
otherwise. Documented here, enforced by existing gate + T1 tests.

## Ownership (disjoint, shared checkout, no git commands by tracks)

- T1: `tests/test_qualification_defects_t1.py` (new, tests only)
- T2: `experiments/checks.py`, `drivers.py`, `drivers_refund.py`,
  `examples/equipment_booking/driver_booking.py`,
  `tests/test_drivers_checks_t2.py` (new) + EDIT `configs.py`, `controller.py`
- T3: `experiments/plan.py`, `fidelity/report.py`,
  `tests/test_planner_t3.py` (new; no edits to existing files; public ops only)
- T4 (after T1–T3): `examples/equipment_booking/e2e_proof.py`,
  `tests/test_e2e_proof_t4.py` (new; reads T2's driver from tree)

Order: T1+T2+T3 parallel → T4 → synthesis → owner review, commit, report.
Gates: 152 python + 46 node + tsc green; stdlib-first; zero model calls;
no secrets; refund traces byte-identical; no UI edits; no new formats.
