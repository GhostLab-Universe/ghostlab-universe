# GhostLab — Technical Handover

Audience: the developer taking over. Everything below is verified against
the tree at the cited commits, not aspirational. Read `AGENTS.md` first;
it is binding project law.

## 1. What GhostLab is

A developer tool that finds and reproduces failures caused by **timing,
retries, and changing permissions in stateful agent workflows**
(e.g. reservation commits, response times out, naive retry duplicates it).

Workflow: connect a qualified external test environment + target agent ->
configure faults/checks/limits -> run -> inspect authoritative evidence ->
minimize/reproduce -> fresh-retest the corrected agent -> save regression.

Authoritative verdicts come only from executable checks over state
evidence. Model text is explanation, never evidence.

Stack: React/Vite/TypeScript UI, FastAPI/Pydantic boundary, LangGraph
StateGraph, SQLite serial MVP (Postgres later), stdlib-first core.

## 2. What was built (branch map)

Start at `feature/isolated-backend` (contains all lines below as ancestors).

| Line | Commits | Content |
|---|---|---|
| Refund core | base ancestors | Synthetic INR 2,500 refund world, timeout-after-commit, vulnerable/repaired agents, independent checks, Reality Diff, deterministic replay, idempotency repair. `src/ghostlab/core.py` |
| Track A | `a5d3e9d` | Environment registration, bounded JSON manifest import, capability discovery, explicit qualification (reset/isolation/schemas/evidence/faults/cancel/reproduction), versioned reports. `src/ghostlab/envs/` |
| Track B | `7e036e0` | Target-agent registration via controlled gateway, limits/cancel, version capture. External example: equipment booking. `src/ghostlab/targets/`, `examples/equipment_booking/` |
| Track C | `643ba16` | Experiment definitions vs immutable resolved configs (digest, pinned versions, provenance), project-scoped persistence, controller, comparison. `src/ghostlab/experiments/` |
| Track D | `927160a` | Guided 7-step UI, developer guide, browser acceptance. `web/src/` |
| Integration | `841b1fe` | Booking world runs through the shared env pipeline (no core edits for the external example) |
| E1-E4 (advantage) | `05157c6`, `dbca3de`, `2f68468`, `267422e`, `f62edb5` | Late fault arming; loopback boundary proxy (real HTTP timeout-after-commit); occurrence triggers, trace minimization, regression export; simulation-fidelity manifests/divergence; live service layer (E4 API) |
| Dashboard fix | `9412e3c`, merged `114b9e2`, `5ea1a4d` | Service-first UI; fixtures dev-only, never fallback; dead-service shows error |
| Testing pipeline | `b855bd7`, `8468113`, `6870c18`, `aee2f8b`, `83df9b3` | Frozen contracts, defective-adapter qualification battery, domain-independent checks/drivers, bounded exploration planner, booking end-to-end proof |
| Isolated backend | `0e26730`, `c40a41f` (+ handover fixes) | Threat model + scope, `ExecutionBackend` interface/protocol, local reference backend, Docker backend, controller/API/UI wiring. `src/ghostlab/backends/` |

Verified at handover: full Python suite 199/199 OK (pre-backend);
backend B1+B2+B3+B5 84/84 OK; stub-lifecycle 18/18 OK;
booking backend acceptance 9/9 PASS (`model_calls=0`);
live browser acceptance 88/88; `tsc` 0 errors.

## 3. Key decisions and why

1. **Contracts first (M00 route), four parallel tracks, one integration
   owner.** Parallel agents inventing interfaces independently produces
   incompatible subsystems. Shared shapes frozen before parallel builds;
   cross-track changes only via the owner.
2. **No competing subsystems.** Reused controller/policy/accounting/API;
   deterministic controller is orchestration (no second agent harness);
   no capsule v3; no second backend; no new formats.
3. **Manifest import is data-only.** No package install, no code exec, no
   arbitrary URLs/paths. Validation never executes the environment;
   qualification is an explicit, visible operation. Reason: import must
   never become a code-execution vector.
4. **Digest identifies, never authenticates.** Canonical config digests pin
   content; qualification binds adapter/manifest identity, rechecked at
   execution start. A changed adapter does not inherit old qualification.
5. **Precise fault control over sleeps.** Deterministic event-based
   triggers (`before` Nth action, after-commit/before-response,
   permission withdrawal) with required/optional + fired/not-reached/
   unsupported accounting. A required fault that never fired = untested,
   never a pass. Wall-clock sleeps are not the race mechanism.
6. **Boundary proxy (E1) instead of reimplementing services.** Real
   forward-then-timeout over loopback HTTP proves timing failures against
   an actual service boundary, not a mock.
7. **Late-arm propagation.** `set_fault` propagates to an already-reset
   world (single-shot) so occurrence triggers work on shipped adapters.
8. **Attempt auto-assign.** Omitted run `attempt` -> next free number;
   explicit collisions fail honestly instead of overwriting runs.
9. **Project-scoped persistence, credentials outside git/manifests/
   exports/logs.** Secrets scanned; browser never sees keys.
10. **Dashboard: real execution or error, never fixture fallback.**
    Fixtures are dev-only with unmistakable labels. This was a verified
    defect (UI showed copied fixture results) and is fixed on real routes.
11. **Backend host-gated strategy.** Interface + unqualified local
    reference first; Docker enforcement claims only from executed
    adversarial evidence on a live daemon; untrusted multi-tenant is
    explicitly OUT. No "secure sandbox" claim from Docker startup alone.
12. **No silent fallbacks anywhere.** Unknown/unavailable backend ->
    refusal (HTTP 503 at API), never in-process substitution. Unsupported
    capability -> structured validation error, never silently ignored.
13. **Deterministic first, live later.** No live inference spending was
    authorized; `model_calls=0` is honest for deterministic runs. Live
    Meta/Baseten compatibility stays behind sanitized access probes with
    the exact documented endpoints/model IDs (see `AGENTS.md`).

## 4. Why each feature exists

- **Env registration + qualification:** a broken adapter (bad reset, shared
  state, lying evidence) must fail *before* it can produce fake passes.
  The defective-adapter battery proves each check detects its defect.
- **External booking example:** proves connect-without-core-edits. The
  naive agent duplicates a reservation after timeout-after-commit; the
  idempotent correction passes a fresh retest. This is the product proof.
- **Config split (definition/resolved/run):** changing a saved definition
  must never mutate history; runs pin versions so results are auditable.
- **Minimizer + regression export:** a failure is only useful if it can be
  shrunk to a witness and re-run in CI after the fix.
- **Fidelity reports:** a reproducible failure in a simulator that the real
  service cannot exhibit is worthless; reports state modeled vs
  approximated vs unsupported behavior. No universal fidelity score.
- **Exploration planner (fixed/random/model-guided):** manual scenarios
  miss timing/state combinations; bounded generation covers them without
  silently substituting strategies.
- **Isolated backend:** reviewed developer code needs a job boundary so
  the controller's evidence store, credentials, and other projects are
  unreachable; job output is untrusted data, controller assigns IDs and
  receipt order.

## 5. Limitations (do not oversell)

- Only **trusted built-in in-process** execution is qualified today.
  Local reference backend is explicitly `unqualified`; Docker backend is
  `unqualified` until daemon-enforced adversarial evidence is recorded
  on the target host.
- Controller `drive_agent` is refund-shaped for service runs; booking and
  proxy execution go through walkthrough/qualify/proof paths, not the
  generic service run endpoint.
- Run-group handles and effective configs live in process memory; a
  restart drops them. Server-issued finding/capsule artifacts, server run
  listing, and `code_supported` list are open.
- Windows: Docker path needs the daemon up; cancel of child processes
  has a documented descendant-survival limitation. Serial jobs only;
  parallelism is rejected unless the environment supports it.
- No head-to-head vs LangSmith/Maxim/tau-bench/AgentDojo. Overlap is
  established; superiority is NOT claimed.
- Live inference: unproven on this branch. Model IDs/endpoints are from
  documentation only; no paid calls were made.

## 6. Yet to build (in priority order)

1. **Daemon-enforced Docker tests failing** in
   [tests/test_backend_docker_b4.py](tests/test_backend_docker_b4.py)
   (`DaemonEnforcedTests`): one known test-side bug (`self.start(...)`
   missing the backend argument in `test_usability_refund_lifecycle`),
   forged-worker-output rejection returns no `failed` event,
   adversarial subtests (canaries/egress/breakout/fork/disk/memory)
   unverified after the pump fix. Start here.
   Reproduce: `python -m unittest tests.test_backend_docker_b4.DaemonEnforcedTests` (daemon up; slow).
2. **Final trust-boundary report** (this doc is the handover record;
   the per-track report `docs/modules/backend-final-report.md` was never
   written). Include host/runtime versions, executed adversarial
   evidence, qualified category.
3. **Independent security review** of [backends](src/ghostlab/backends/)
   before any reviewed-code execution claim.
4. Merge decision: `feature/isolated-backend` -> integration/main needs
   owner review (the BYOE line diverged from the old integration tip at
   `10c3a3b`; do not blind-merge).

## 7. Future scope (deferred, by design)

Hosted developer-side runner (outbound-authenticated, local credentials),
microVM/stronger isolation with host evidence, arbitrary repo upload
(needs package-intake qualification), parameter sweeps (needs bounded
Cartesian display first), hosted multiworker/Postgres deployment,
qualified model broker for live inference inside jobs.

## 8. How to run / reproduce

```powershell
$env:PYTHONPATH='src'; python -m unittest discover -s tests   # full suite
python examples/equipment_booking/walkthrough.py              # failure + fix proof
python examples/equipment_booking/backend_acceptance.py       # 9/9 backend stages
python examples/booking_service/demo.py                       # proxy proof
cd web; npx tsc --noEmit -p tsconfig.app.json; npx vite build # UI (run from web/)
node web/tests/acceptance.mjs --service=http://127.0.0.1:8923 # live acceptance
```

Rules that still bind the next owner: separate temp DBs per test area;
no live inference without an explicit confirmed budget; credentials
outside git; named `git add` only; never touch other sessions'
branches, worktrees, or running processes; update
`docs/architecture-decisions.md` when evidence changes a decision.
