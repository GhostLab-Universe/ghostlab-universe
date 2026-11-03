# MVP delivery plan

The baseline is React/Vite/TypeScript, FastAPI/Pydantic, LangGraph with custom Meta provider nodes and local SQLite. The detailed decisions and citations are in [architecture.md](architecture.md). The first executable slice is a real stateful simulation with synthetic data and deterministic adapters; live Meta, graph, service and interface follow their acceptance gates.

| Stage | Deliverable | Acceptance evidence |
| --- | --- | --- |
| 1 | Versioned contracts | Scenario, action, event, run, invariant, diff and capsule types have explicit versions |
| 2 | Refund world | Baseline refunds 250000 paise once; unauthorized customer attempts cannot mutate the ledger |
| 3 | Controlled fault | Refund commits, response times out, vulnerable adapter retries and actually creates a duplicate |
| 4 | Verifier and repair | Independent checks detect duplicate/over-refund; stable idempotency repair leaves one refund |
| 5 | Reality Diff and replay | Compute first divergence and ledger deltas; capsule reproduces actions, final state and checks without inference |
| 6 | Meta capability gate | Redacted report proves exact model access, tool roundtrip, schema validation, continuation and bounded failure handling |
| 7 | LangGraph planner | Propose, validate, execute, verify, remember and stop within controller-owned budgets |
| 8 | FastAPI service | Create, inspect, replay, compare and cancel runs using shared contracts |
| 9 | React interface | Display actual run status, trace, Reality Diff and replay results |
| 10 | End-to-end demo | Live model proposes experiments; executed evidence supports findings; reviewed repair passes regressions |

Stages 2-5 need no keys or network. Synthetic fixtures are intentional test-world data. State transitions and verdicts must execute; no fake model output, real-payment claim or invented benchmark result is acceptable. This plan describes acceptance gates, not completion status.

## Parallel work

Freeze event/capsule contracts together. Track A owns world, engine, verifier, diff, replay and regression checks. Track B owns provider adapter, capability probes, graph, service and interface. These are proposed tracks rather than assignments to named collaborators. Both use the same versioned JSON examples and review shared schema changes together.

Use a separate branch per task and review pull requests. Each developer keeps a local database/environment file. Share code and sanitized fixtures through GitHub; never commit live databases or credentials.

## Required checks

Core checks cover baseline, timeout-after-commit, duplicate detection, repaired idempotency, unauthorized access, isolated run state, invalid/unknown tool rejection, budget exhaustion, capsule validation and replay consistency. Actual test output determines which are implemented.

Provider probes record endpoint, model, timestamp and dependency versions. Check exact model access, tool identifiers/results, structured output, local semantic validation, continuation, usage, bounded retries and cancellation. Streaming stays disabled until partial tool arguments and interrupted streams are tested. Baseten's -1026 model requires a separate access check. Fallback never silently changes the provider or model.

Measure discovered seeded failures, executions required, replay success and false positives under equal budgets before comparing random, one-pass and adaptive exploration. Report all runs and sample sizes.

## Deferred

WorldSmith generation, a second world, vector memory, arbitrary code upload, production traces, public registry and automatic patch execution follow the refund demo. NeMo Guardrails is optional after engine enforcement works. OpenShell or another isolated backend must pass boundary tests on its actual host before unknown code executes.
