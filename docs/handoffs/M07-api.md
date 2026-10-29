# M07 — Expose an authenticated experiment control plane

Implement only M07 in https://github.com/GhostLab-Universe/ghostlab-universe. Fetch `architecture-and-refund-core` / PR #1 and create `feature/m07-api`. Read AGENTS.md, README.md, architecture.md and docs/handoffs/versions.md. Consume M00 contracts, M04 repositories, M05 controller, M06 policy and the early approved M09 telemetry interface. M09 final integrated evaluations are not an API prerequisite. Preserve the existing core and fourteen tests; do not reimplement simulation in routes.

Use the latest approved integration commit containing your prerequisites and record its actual head. architecture-and-refund-core is the current initial integration base. If prerequisites are not integrated, stop implementation and report the missing interfaces rather than inventing them.

Treat docs/handoffs as read-only reference. Shared configuration/schema and root .env.example changes belong to M00; never add credential values.

**Own:** `src/ghostlab/api/`, `tests/api/`, `docs/modules/M07.md`. M06 owns authorization/approval semantics; M05 owns scheduling/budgets; M04 owns database transactions. You own HTTP/session adapters and generated OpenAPI. Shared schema/dependency changes go through M00; UI source belongs to M08.

Provide create/status/events/cancel campaign routes; inspect run, compare Reality Diff, export/import/replay capsule and authorized artifact download; expose approval-request status and approval/rejection submission. Derive project/principal from authenticated sessions before calling domain services. Clients cannot select trusted role/scope in a request. Define explicit local development authentication mode and deployment restrictions; never represent it as production authentication.

Use strict Pydantic request/response boundaries and typed error mapping. Enforce request idempotency, bounded input/output, cancellation propagation and authoritative reconnect/status reads. Provider credentials stay server-side. Stream or poll application events without assuming model-token streaming has passed its separate capability gate. Approval endpoints call M06 rather than introducing a second decision mechanism.

Acceptance: endpoint and artifact authorization covers every route; cross-project access and forged scopes fail; retries with the same request identity do not duplicate campaigns/actions; invalid requests fail before mutation; cancel/reconnect preserve correct state; denied/expired approvals cannot resume work. OpenAPI examples and generated frontend types agree with M00. Test error/empty/awaiting-approval cases as well as successful refund/replay flow. No secrets/raw sensitive traces appear in responses.

Return branch/PR, endpoint/OpenAPI index, authentication assumptions, idempotency/cancel behavior, exact tests/results and unresolved deployment gates. Do not create another scheduler, use fake live responses, introduce paid calls without authorization, weaken policy for the demo, merge or deploy.
