# ARCHITECTURE.md — GhostLab Universe

GhostLab finds and reproduces failures caused by **timing, retries and changing permissions in stateful
agent workflows**. In the canonical example, a reservation commits, the response times out, and a naive retry duplicates it.

Workflow: connect a qualified external test environment and target agent, then configure faults, checks
and limits, then run, inspect authoritative evidence, minimize and reproduce, fresh-retest the corrected
agent, and save a regression.

Verdicts come only from executable checks over state evidence. Model text is explanation, never evidence.
The cited decision record with sources is `docs/architecture-decisions.md`. The handover record is
`docs/handover/HANDOVER.md`.

## Stack

| Layer | Choice | Why |
|---|---|---|
| Frontend | Vite + TypeScript, React through `createElement` (no JSX plugin) | A guided 7-step UI with no extra dependencies |
| Backend | FastAPI + Pydantic (`src/ghostlab/api/`) | OpenAPI and strict validation at the boundary. The core stays stdlib |
| Orchestration | LangGraph StateGraph, custom provider nodes | Explicit nodes, budgets and verifier feedback with one framework |
| Database | SQLite, serial local MVP | Single coordinator. Postgres only for a hosted multiworker service (cut) |
| Auth | None. Any sign-in must stay optional | A judge demo must never hit a login wall |
| AI/ML | Meta Responses (primary), Baseten Chat (conditional, unused), Ollama Muse Glimmer (local) | Hackathon models. Each provider gets a distinct adapter |
| Deploy | Not chosen yet. It must be a free tier with no card, and it uses `META_API_KEY_PUBLIC` only | No paid services (see AGENTS budget) |

## Structure

```
src/ghostlab/
  core.py          refund world, vulnerable/repaired agents, checks, Reality Diff, capsule replay
  envs/            environment registration, data-only manifest import, capability discovery, qualification
  targets/         target-agent registry + ControlledGateway (allowlist, budget, cancel, action records)
  experiments/     definition -> immutable resolved config -> run; controller; ops (public boundary);
                   plan.py (fixed / stateful_random / model_guided planner); minimize, regress, drivers, checks
  proxy/           loopback boundary proxy: real HTTP timeout-after-commit
  fidelity/        simulation-fidelity manifests and divergence reports
  backends/        ExecutionBackend interface, local reference + Docker backends (both unqualified)
  providers/       Meta Responses + Baseten Chat adapters, https transport, outbound policy (ported from 9f5162d)
  security/        redaction, isolation helpers (ported with providers)
  contracts/       frozen Pydantic contracts (ModelRequest/ModelReply/ExperimentProposal, evidence, ...)
examples/equipment_booking/   external target proving connect-without-core-edits
examples/booking_service/     proxy proof against a real HTTP service
scripts/probe_provider.py     sanitized provider capability probe (dry run by default)
web/src/screens/step1-7.ts    guided UI; serviceClient.ts talks to /v1
```

## Data flow

The UI calls `/v1` on FastAPI, which goes to `experiments.ops`. Ops resolves a requested config into an
immutable resolved config (digest, pinned env/adapter versions). The controller then resets independent
baseline and fault worlds from the same fixture and drives the target through `ControlledGateway`. Faults
fire on deterministic event triggers. Checks read controller-owned evidence and produce a verdict, Reality Diff
and capsule. Minimize and regression export run from the capsule with zero model calls.
The planner sits in front of `ops`. In `model_guided` mode a model proposes requested configs, and ops validates
every one before execution. Invalid proposals are recorded as rejected and never run.

## Decisions

- **Contracts first, four parallel tracks, one integration owner.** Parallel agents that invent interfaces
  independently produce incompatible subsystems. Rejected: each track owning its own shapes. The integration line
  that diverged at `10c3a3b` shows the cost.
- **No competing subsystems.** The controller, policy, accounting and API are reused, and the deterministic controller is
  the orchestration. Rejected: a second agent harness (LangChain `create_agent`/CrewAI/AutoGen), capsule v3,
  a second backend, new formats.
- **Manifest import is data-only.** No package install, code execution or arbitrary URLs/paths, and
  qualification is a separate visible step. Reason: import must never become a code-execution vector.
  Rejected: installable plugin manifests.
- **Digest identifies, never authenticates.** Qualification binds adapter and manifest identity and is rechecked
  at execution start, so a changed adapter does not inherit old qualification. Rejected: treating hashes as trust.
- **Precise fault control instead of sleeps.** Event triggers (before the Nth action, after-commit/before-response,
  permission withdrawal) with required/optional faults and fired/not-reached/unsupported accounting. A required fault that never fired
  means untested. Rejected: wall-clock sleeps as the race mechanism, because they are flaky and not reproducible.
- **Boundary proxy (E1) instead of reimplementing services.** A real forward-then-timeout over loopback HTTP.
  Rejected: mock-only timing, which proves nothing about a real boundary.
- **Late-arm propagation.** `set_fault` reaches an already-reset world (single shot), so occurrence
  triggers work on shipped adapters. Rejected: requiring adapters to pre-arm.
- **Attempt auto-assign.** An omitted `attempt` gets the next free number, and an explicit collision fails.
  Rejected: overwriting runs.
- **Project-scoped persistence, credentials outside git/manifests/exports/logs.** Secrets are scanned, and
  the browser never sees keys.
- **The dashboard shows real execution or an error, never a fixture fallback.** Fixtures are dev-only and labeled. A UI that
  showed copied fixture results was a verified defect and has been fixed.
- **Backend host-gated strategy.** The interface and an unqualified local reference came first. Docker claims come only from
  executed adversarial evidence on a live daemon. Untrusted multi-tenant use is out of scope. Rejected: claiming a "secure sandbox" from
  Docker startup.
- **No silent fallbacks.** An unknown or unavailable backend gets HTTP 503, and an unsupported capability gets a structured error.
  Rejected: in-process substitution.
- **Deterministic first, live later.** `model_calls=0` is honest for deterministic runs. Live Meta goes through
  the sanitized probe with exact model IDs. Rejected: a generic `ChatOpenAI` base-URL shim, which can drop provider fields.
- **LangGraph replay is not capsule replay.** Checkpoints can re-run model calls and side effects and do not
  snapshot worlds. A capsule replays recorded decisions against reset state. A fresh model call is a new experiment.
- **Phase 0 branch: handover line + ported providers, PR branches not merged.** `feature/isolated-backend`
  holds the handover work. PRs #49/#24/#22/#1 sit on a line that diverged at `10c3a3b`, have 2–11 conflicts
  each and duplicate api/experiments/web. Only the new-path `providers/` and `security/` were taken. Rejected: a blind merge,
  and #49's sign-in/GitHub inspection, which is tied to that line's API and views.

## Why each feature exists

- **Env qualification:** a broken adapter (bad reset, shared state, lying evidence) must fail before it can
  produce fake passes. The defective-adapter battery proves each check catches its defect.
- **External booking example:** proves connect-without-core-edits. A naive agent duplicates after
  timeout-after-commit, and the idempotent fix passes a fresh retest.
- **Definition/resolved/run split:** editing a definition never mutates history.
- **Minimizer + regression export:** a failure is useful only once it is shrunk to a witness and re-runnable in CI.
- **Fidelity reports:** they state modeled, approximated and unsupported behaviour. There is no universal fidelity score.
- **Exploration planner:** manual scenarios miss timing and state combinations, and bounded generation covers them.
- **Isolated backend:** a job boundary for reviewed developer code. Job output is untrusted.

## Technical debt

- [ ] Only trusted built-in in-process execution is qualified. The local and Docker backends are `unqualified`.
- [ ] `drive_agent` is refund-shaped. Booking and proxy runs bypass the generic run endpoint.
- [ ] Run-group handles and effective configs live in process memory and are lost on restart.
- [ ] Two local-backend tests fail on Linux (see TODO Bugs).
- [ ] There is no head-to-head comparison with LangSmith, Maxim, tau-bench or AgentDojo. Overlap exists, and superiority is not claimed.

---

## Standards

This project follows the `engineering-standards` skill, which loads on demand.

**Overrides for this project:**

- Solo build: `main` plus one working branch (`hackathon/final`), not the four-branch chain.
- No CODEOWNERS. There is a single maintainer and no approval counts.
- The 300-line file limit is not retrofitted to existing modules during the hackathon. New code follows it.
- No SemVer release or tagging before submission. The version stays `0.1.0`.
