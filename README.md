# GhostLab Universe

An agent testing system that runs simulated worlds, introduces controlled failures and turns discoveries into replayable regression tests.

Consider a refund agent handling a synthetic INR 2,500 order. The payment succeeds, but the response times out. Does the agent check transaction status, or issue a second refund? GhostLab records the actions and ledger state so the result can be checked independently.

## Project status

Planning scaffold. The runtime, dashboard and model integrations are not implemented yet. No benchmark results are claimed.

## MVP

- A stateful refund world with synthetic orders, customers and a payment ledger.
- Reality Fork: replay the same initial state under a controlled fault.
- Reality Diff: compare actions and final state between baseline and faulted runs.
- Deterministic checks for duplicate refunds and unauthorized actions.
- Failure Capsules: export the fixture, scenario, trace and results needed for replay.
- A bounded model-driven experiment loop, with proposals validated before execution.

Start with a timeout after payment commit. Add stale data and malicious tool-result text after replay works.

## Planned stack

React interface, FastAPI service, Python simulation engine and SQLite storage. Keep model access behind a provider interface. The earlier project discussion proposed Meta model access through Baseten and optional isolated execution through Modal; provider availability and model identifiers must be verified before integration.

## Documentation

- [Architecture](architecture.md)
- [MVP plan](mvp-plan.md)
- [Working together](CONTRIBUTING.md)
- [Setup status](setup-status.md)

## Team

- Sairishwanth89: requested organization owner.
- Abhiram-0910: requested repository collaborator with Write access.

These are intended roles. Invitation and acceptance status are recorded separately in the setup status document.

## Getting started

Choose a task from the MVP plan and agree on the experiment schema before implementing the engine and interface in parallel. There are no installation or run commands yet because application code has not been added.

Use synthetic data and keep API credentials outside the repository. A successful regression run establishes that the defined checks passed; it does not establish that an agent is safe in every environment.

