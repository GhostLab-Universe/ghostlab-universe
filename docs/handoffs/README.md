# Give Claude one module at a time

GhostLab has ten work packages covering all fifteen capabilities in the supplied production-agent checklist. These are build assignments, not ten new agents that must run continuously. Start with M00, then work through the dependency waves. Keep the existing refund experiment intact.

## Master prompt

Copy this context with exactly one module prompt:

```text
You are implementing one module of GhostLab Universe.
Repository: https://github.com/GhostLab-Universe/ghostlab-universe
Start from architecture-and-refund-core, which is the branch of open PR #1. Do not start from the older main scaffold. Fetch it, record its actual commit, and create a separate feature branch for your assigned module. Do not reset or overwrite another person's changes.

Use the latest approved integration commit containing your prerequisites and record its actual head. architecture-and-refund-core is the current initial integration base. If prerequisites are not integrated, stop implementation and report the missing interfaces rather than inventing them.

Read AGENTS.md, README.md, ARCHITECTURE.md, docs/handoffs/versions.md, then your assigned module prompt. Follow the exclusive path ownership below. Propose shared-schema or dependency changes to M00; do not edit another module's files to make your implementation fit.

Existing code implements a trusted synthetic refund world, baseline/fault/repair runs, deterministic checks, Reality Diff and capsule replay. Fourteen core tests passed previously on Python 3.13.2. Run them on your checkout before changes. Build on this code; do not rebuild it from scratch. Live providers, LangGraph, persistence, API, UI and production authentication remain planned.

Use LangGraph with custom Meta provider nodes, FastAPI/Pydantic and React/Vite/TypeScript. Direct Meta Responses is primary; Baseten uses a separate conditional adapter. Keep simulation and replay independent of model libraries. SQLite is for the single-coordinator MVP; Postgres is the later deployed store.

All fifteen architectural capabilities must have explicit implementation and acceptance coverage. They do not each require a separate product. Typed internal envelopes provide communication initially; MCP is optional for external tools, A2A is deferred until independently hosted agents are required. Vector storage, Redis, NeMo and OpenShell each need a documented trigger and passing gate before adoption.

Do not make live inference calls without an authorized key, exact enabled model and bounded budget. Keep credentials out of code, logs, prompts, capsules and browser bundles. Domain fixtures are intentionally synthetic; ledger transitions and verdicts must execute. Never fabricate live model outputs, compatibility results or benchmark superiority.

Implement only the assigned module. Return its branch/PR, changed paths, interface versions, exact runtime/dependency versions tested, commands and actual results, and remaining gaps. Do not merge, deploy, upgrade the whole stack or claim production readiness after unit tests alone.
```

## Package ownership

| ID | Claude prompt | Main responsibility | Depends on |
| --- | --- | --- | --- |
| M00 | [Foundation](M00-foundation.md) | Contracts, fixtures, toolchain and shared locks | Existing core |
| M01 | [Providers](M01-providers.md) | Meta/Baseten adapters and capability evidence | M00 |
| M02 | [Simulation](M02-simulation.md) | Existing world, tools, verifier, fork/diff/replay | M00, policy interfaces |
| M03 | [Skills and context](M03-skills-context.md) | Versioned procedures and scoped context | M00, memory interface |
| M04 | [Memory and persistence](M04-memory-persistence.md) | State, checkpoints, memory and artifacts | M00 |
| M05 | [Orchestration](M05-orchestration.md) | LangGraph, planning, recovery and budgets | M01–M04, M06 interfaces |
| M06 | [Policy and security](M06-policy-security.md) | Identity policy, approvals and execution boundary | M00, M04 interfaces |
| M07 | [API](M07-api.md) | FastAPI routes and session integration | M00, M04–M06; M09 telemetry |
| M08 | [Interface](M08-interface.md) | React control, Reality Diff and approvals | M00 contracts; M07 for integration |
| M09 | [Quality and operations](M09-quality-operations.md) | Telemetry, evaluations, CI and release gates | M00 first; all modules for final checks |

Every module owns its named test directory and module document. M00 alone owns shared schemas/configuration, root .env.example, root manifests, runtime declarations, Python lock and frontend manifest/lock. Keep docs/handoffs as a read-only task reference; propose handoff corrections to the integration owner. M02 alone edits the existing core and its fourteen-test suite. M04 owns persistence operations. M05 owns budget accounting/reservations; M06 supplies ceilings and authorization policy. M06 owns approval decisions; M05 pauses/resumes, M07 exposes endpoints and M08 displays them. Cross-owner work requires an explicit interface proposal and agreed merge order.

LangGraph can bring `langchain-core` and `langsmith` transitively. These dependencies do not create a second agent harness. Keep trace export disabled unless explicitly configured with redaction and the intended destination; dependency presence is not authorization to transmit data. M09 provides telemetry interfaces; each package owner instruments its own code.

## Build waves

1. **Freeze contracts (M00 phase A):** M00 completes clean dependency installs, import/schema checks and the existing core suite, then publishes strict schemas, examples, generated frontend types and approved interface versions. It does not wait for downstream API/UI/workflow tests.
2. **Build boundaries in parallel:** M01, M02, M03, M04, M06 and M09 implement against frozen interfaces. M09 publishes its approved event/sink interface now; consumers do not wait for its final integration suite. M08 can develop contract-tested components from M00 fixtures before M07 exists, clearly labeling test fixtures.
3. **Integrate the workflow:** M05 connects provider, context, policy, store and engine; M07 exposes it. Authenticated provider probes must pass before live exploration is enabled.
4. **Prove the whole flow (M00 phase B with M09):** After M05 checkpoint/approval flow, M07 API and M08 UI exist, jointly certify the integrated baseline. M08 connects to M07; M09 runs end-to-end, failure, recovery and release checks. This final certification does not block phase-A interface publication.

For two developers, one track can own simulation/storage/policy and the other providers/context/orchestration, then share API/UI integration. These are suggested tracks, not assignments to named people. Give each Claude session one module with its dependencies already available. Do not ask ten sessions to edit one checkout simultaneously.

## Every capability has an owner

| Supplied capability | Implementation coverage |
| --- | --- |
| 1 Reasoning | M01 provider; M05 separate model roles |
| 2 Planning/decomposition | M05 task DAG, dependencies and exit conditions |
| 3 Tools/APIs | M02 typed tools; M06 permission checks; M07 service |
| 4 Skills | M03 versioned procedures and registry |
| 5 Memory | M04 capture/filter/store/retrieve/expire; M03 selection |
| 6 Context engineering | M03 role-scoped bundles, provenance and limits |
| 7 Orchestration | M05 explicit LangGraph/controller transitions |
| 8 Communication | M00 envelopes; M05 validated routing |
| 9 State/persistence | M04 transactions; M05 checkpoints and recovery |
| 10 Verification/evaluation | M02 deterministic verifier; M09 independent evaluations |
| 11 Security/permissions | M06 policy; M07 session identity; M02 execution boundary |
| 12 Human oversight | M06 bound approvals; M05 pause/resume; M07/M08 controls |
| 13 Observability | M09 redacted spans/events and audit metadata |
| 14 Resilience/recovery | M05 recovery; M04 durable state; M01/M02 safe retries |
| 15 Resource management | M05 reservations/concurrency; M06 ceilings; M01 usage |

Controlled improvement is M03 prompt/skill versioning plus M09 measured regressions, review and rollback. The supervisor is trusted deterministic control; WorldSmith proposes specifications, Explorer proposes experiments and Analyst explains evidence. One Meta model can serve these logical roles with isolated contexts. The target agent is the system under test. The verifier remains code, and a reviewer model cannot change its verdict.

## What is already proved

The local core executed baseline INR 2,500, vulnerable fault INR 5,000 and repaired INR 2,500; divergence occurred at event 2. Fourteen tests passed, and capsule replay matched trace, state and checks with zero model calls. This proves the bounded local slice. It does not prove live Meta compatibility, installed framework integration, hosted isolation or production readiness. Use [versions.md](versions.md) for release metadata, resolver evidence and remaining checks.
