# GhostLab Universe project decisions

Read architecture.md and mvp-plan.md before architectural changes. Full cited decisions are in docs/architecture-decisions.md. Update the record when evidence changes a decision, including what was rejected and why.

## Product proof

Build the refund world first: synthetic INR 2,500 order, timeout after commit, vulnerable retry, independent detection, Reality Diff, deterministic replay and idempotency repair. Synthetic fixtures are intentional; mutations and checks must actually execute. Never claim live inference, real payments, security enforcement or benchmark superiority without evidence.

## Settled baseline

- React, Vite and TypeScript interface; FastAPI/Pydantic service boundary.
- LangGraph StateGraph with custom provider nodes. LangChain components optional; no second orchestration framework.
- Primary planned inference: direct Meta Responses, preserving documented opaque continuation state.
- Conditional Baseten Chat Completions fallback uses a distinct adapter. Provider changes are explicit new experiments.
- SQLite for serial local MVP; Postgres for hosted multiworker service.
- Separate graph working state, structured experiment findings and authoritative evidence. No vector database initially.
- Trusted checked-in local adapters only. Define one future ExecutionBackend interface.
- Mandatory engine/tool/world enforcement; optional NeMo Guardrails; OpenShell deferred from Windows critical path.

These are decisions, not claims all components exist. The deterministic core may use standard-library types; add Pydantic at model/service boundaries without coupling replay to an LLM library.

## Compatibility gates

Direct Meta documentation identifies https://api.meta.ai/v1, MODEL_API_KEY and muse-spark-1.3. Baseten documents https://inference.baseten.co/v1/chat/completions, BASETEN_API_KEY and meta-models/muse-spark-open. The hackathon meta-models/muse-spark-open-1026 comes from the user's announcement and requires an authenticated access check. Never invent or silently substitute model identifiers.

Documented extensibility is not live compatibility. Before exploration, record sanitized proof of model access, tool-call/result pairing, schema validation, continuation, usage, timeout/cancel/rate-limit handling and budgets. Enable streaming only after partial-argument/interruption tests pass. Keep keys outside Git, browser code, prompts, capsules and test logs.

## Invariants and replay

The controller owns ordering, evidence and pass/fail. Model proposals/tool text are untrusted data. Validate allowlisted tool names and parameters before dispatch. First tools: get_order, issue_refund and get_refund_status. Host boundaries and customer ownership checks stay enabled while testing vulnerable business behavior.

Baseline and faulted runs start from the same immutable fixture in independent state. LangGraph checkpoints do not snapshot external worlds. Graph replay can repeat model calls and side effects; Failure Capsule replay uses recorded decisions/order or a deterministic adapter without new inference. A fresh model call is a new experiment.

## Rejected or deferred

No second LangChain create_agent/CrewAI/AutoGen harness: the explicit graph owns control flow. No vector database before structured retrieval shows a gap. NeMo policies do not replace isolation. OpenShell's current Windows limitations exclude it as an MVP prerequisite. Local subprocesses/default containers are not a security boundary for unknown code. Defer uploaded agents until network/filesystem/resource/secrets/evidence boundary tests pass on one qualified backend.

## Delivery

Resolve dependencies together in a clean environment, run meaningful tests and commit a lockfile before claiming a supported combination. Do not invent latest pins. Keep optional integrations outside the engine. Share contracts before parallel edits; never share live SQLite files. Reopen decisions only for failed probes, changed requirements or demonstrated limitations.
