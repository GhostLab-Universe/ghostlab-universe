# M09 — Measure behavior and control releases

Implement only M09 in https://github.com/GhostLab-Universe/ghostlab-universe. Fetch `architecture-and-refund-core` / PR #1 and create `feature/m09-quality-operations`. Read AGENTS.md, README.md, architecture.md and docs/handoffs/versions.md. Phase A publishes the event/schema/sink interface after M00 contracts so other modules can instrument against it. Phase B runs final evaluations after the workflow, API and UI exist; certify the integrated baseline with M00. Consumers do not wait for phase-B results to start. Preserve the existing fourteen core tests and let their M02 owner maintain them.

Use the latest approved integration commit containing your prerequisites and record its actual head. architecture-and-refund-core is the current initial integration base. If prerequisites are not integrated, stop implementation and report the missing interfaces rather than inventing them.

Treat docs/handoffs as read-only reference. Shared configuration/schema and root .env.example changes belong to M00; never add credential values.

**Own:** `src/ghostlab/observability/`, `evals/`, `tests/integration/`, `.github/workflows/`, `deploy/`, `docs/modules/M09.md`. Package-specific unit tests stay with their owners. Do not edit business logic to make evaluations pass, change shared locks or bypass security gates.

Provide injectable redacted structured events/spans with campaign/task/parent trace IDs, role/provider/model/prompt/skill/workflow versions, latency, usage, retries, cancellation, validation failures, approvals and outcomes. Keep local trace evidence useful without a hosted account. Define an OpenTelemetry-compatible export boundary; one optional viewer can follow an actual operational need. Unknown usage/cost remains unknown; logs have access/retention controls.

Build deterministic, adversarial and end-to-end evaluations for tool success, handoff failures, recovery, human correction, latency and cost per successful task. Compare random, one-pass and adaptive exploration only under equal budgets with reported sample sizes. Record actual measurements, not claimed superiority. Test prompt-injection data, caller spoofing, malformed capsules, exhausted budgets, provider errors, crash/restart and uncertain commits.

Own controlled improvement/release evidence: recurring failure → reproducible case → reviewed prompt/skill/code version → regression results → controlled release → monitoring/rollback. Define CI gates, deployment configuration and recovery drills without auto-deploying this task. Model reviews can add commentary but cannot replace deterministic numerical/security checks.

Acceptance: a complete campaign is traceable without secret leakage; evaluations derive results from artifacts; regression failures block promotion; runtime/dependency/live-capability statuses remain distinct; release and rollback targets are explicit. Boundary tests are required before unknown-code execution. Return branch/PR, event/evaluation schemas consumed, actual CI/evaluation output, release checklist and gaps. Do not fabricate live calls, enable optional infrastructure without a trigger, merge or deploy.
