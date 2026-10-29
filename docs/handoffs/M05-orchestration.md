# M05 — Coordinate roles, recovery and resource budgets

Implement only M05 in https://github.com/GhostLab-Universe/ghostlab-universe. Fetch `architecture-and-refund-core` / PR #1 and create `feature/m05-orchestration`. Read AGENTS.md, README.md, architecture.md and docs/handoffs/versions.md. Depend on M00 contracts, M01 provider, M02 engine, M03 context, M04 stores and M06 policy interfaces. Preserve all fourteen core tests.

Use the latest approved integration commit containing your prerequisites and record its actual head. architecture-and-refund-core is the current initial integration base. If prerequisites are not integrated, stop implementation and report the missing interfaces rather than inventing them.

Treat docs/handoffs as read-only reference. Shared configuration/schema and root .env.example changes belong to M00; never add credential values.

**Own:** `src/ghostlab/orchestration/`, `src/ghostlab/budgets/`, `tests/orchestration/`, `docs/modules/M05.md`. You alone own scheduling and token/cost/step/time budget accounting. M06 supplies policy ceilings; M04 supplies atomic persistence. Do not edit those packages or shared manifests.

Implement an explicit LangGraph loop: form hypothesis, propose, validate, execute, verify, remember, choose next experiment or stop. Separate WorldSmith, Explorer and Analyst contexts while permitting one configured Meta model to serve them. The supervisor's transitions and deterministic verifier remain trusted code. WorldSmith proposes reviewed specifications, not arbitrary executable programs. Route AgentEnvelopes through legal task states and dependency order; parallelize independent runs only within campaign limits.

Reserve resources before model/tool execution, reconcile actual usage and record unknown cost as unknown. Propagate cancellation/deadlines; classify retries by error and side-effect state. Checkpoint progress and idempotency identities so restart never blindly repeats a committed action. M06 owns approval decisions: persist awaiting_approval and resume only through its valid single-use decision. Model roles cannot approve themselves or override a failed invariant.

Acceptance: invalid proposals never execute; dependent tasks wait/fail correctly; concurrent work cannot overspend; exhaustion/cancellation terminates cleanly; restart preserves completed experiment IDs; approval resumes once; injected provider errors have bounded recovery. Live exploration remains disabled until M01's exact-model capability gate passes. A graph replay may call models again and must never be labeled deterministic capsule replay.

Return branch/PR, graph/state diagram, routing/budget rules, recovery/approval tests, actual versions/results and gaps. Do not add another LangChain/CrewAI/AutoGen loop or external task engine, invent model results, change provider silently, merge or deploy.
