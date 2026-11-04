# GhostLab Universe

An agent testing system that runs simulated worlds, introduces controlled failures and turns discoveries into replayable regression tests.

Consider a refund agent handling a synthetic INR 2,500 order. The payment succeeds, but the response times out. Does the agent check transaction status, or issue a second refund? GhostLab records the actions and ledger state so the result can be checked independently.

## Project status

The first executable core is implemented: a trusted synthetic refund world, baseline/fault branches, vulnerable and repaired adapters, deterministic invariant checks, Reality Diff and recorded-action capsule replay. The dashboard, LangGraph workflow, live model adapters, database persistence and hosted isolation remain planned. No benchmark superiority is claimed.

## MVP

- A stateful refund world with synthetic orders, customers and a payment ledger.
- Reality Fork: replay the same initial state under a controlled fault.
- Reality Diff: compare actions and final state between baseline and faulted runs.
- Deterministic checks for duplicate refunds and unauthorized actions.
- Failure Capsules: export the fixture, scenario, trace and results needed for replay.
- A bounded model-driven experiment loop, with proposals validated before execution.

Start with a timeout after payment commit. Add stale data and malicious tool-result text after replay works.

## Planned stack

React, Vite and TypeScript interface; FastAPI/Pydantic service boundary; LangGraph StateGraph with custom provider nodes; dependency-free Python simulation engine; SQLite for serial local work and Postgres for a hosted multiworker service. Direct Meta Responses is the planned primary inference adapter. Baseten Chat Completions is a conditional fallback with a separate adapter. Provider/model access and feature compatibility must pass authenticated probes before live exploration; no live integration is claimed by this core.

## Documentation

- [Architecture](ARCHITECTURE.md)
- [Architecture decisions and verified sources](docs/architecture-decisions.md)
- [MVP plan](mvp-plan.md)
- [Handover](docs/handover/HANDOVER.md)

## Team

- Sairishwanth89: confirmed organization owner.
- Abhiram-0910: repository Write invitation sent; awaiting acceptance.

Invitation and acceptance status are recorded separately in the setup status document.

## Getting started

Run from this repository using Python 3.11 or later. The core uses only the standard library, so no dependency installation or API key is needed.

PowerShell:

```powershell
$env:PYTHONPATH = 'src'
python -m unittest discover -s tests -v
python -m ghostlab demo
python -m ghostlab replay artifacts/local/refund-failure.json
```

Linux/macOS:

```sh
export PYTHONPATH=src
python -m unittest discover -s tests -v
python -m ghostlab demo
python -m ghostlab replay artifacts/local/refund-failure.json
```

The demo executes real state transitions over an explicitly synthetic INR 2,500 order. The baseline refunds 250,000 minor units; a timeout after commit causes the vulnerable retry to refund 500,000; the repaired adapter checks status and refunds 250,000. The first divergent event is event 2. The exported failure capsule reproduces the recorded actions, trace, state and checks with zero model calls. A reproduced failing check remains a reproduced failure, not a passing business outcome.

Fourteen standard-library tests cover authorization, principal spoofing, tool schemas, idempotency, branch independence, call budgets, invariant detection, capsule replay, tampering and malformed capsule rejection. Run them on your checkout before changing the engine.

This core accepts trusted checked-in adapters only. It does not execute uploaded programs. The synthetic payment service deliberately permits cumulative over-refunds so the independent verifier can detect the vulnerable adapter; host/tool authorization remains enforced. The current execution budget limits tool calls; hard CPU, memory and wall-clock isolation belongs to the later worker backend. Capsules contain data only. Their checksums detect accidental changes and do not establish trusted authorship.

The controller binds an actor principal to each world; tool-supplied customer IDs cannot change it. Demo runs default to the synthetic order owner, and capsules retain that principal for replay. This identity binding is not production authentication. The future API must obtain the principal from an authenticated session and enforce project access before accepting experiments.

Next implementation steps are the provider capability probes, LangGraph controller, FastAPI contracts and React Reality Diff view in the architecture decision document. No model outputs are fabricated by this demo.

Use synthetic data and keep API credentials outside the repository. A successful regression run establishes that the defined checks passed; it does not establish that an agent is safe in every environment.

