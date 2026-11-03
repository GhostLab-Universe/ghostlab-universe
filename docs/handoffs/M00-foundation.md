# M00 — Freeze contracts and dependency ownership

Implement only M00 in https://github.com/GhostLab-Universe/ghostlab-universe. Start from `architecture-and-refund-core` / open PR #1, record the fetched commit and create `feature/m00-foundation`. Read AGENTS.md, README.md, architecture.md and docs/handoffs/versions.md. Preserve the working refund core and its fourteen tests. This is an additive foundation task, not a framework rewrite.

Use the latest approved integration commit containing your prerequisites and record its actual head. architecture-and-refund-core is the current initial integration base. If prerequisites are not integrated, stop implementation and report the missing interfaces rather than inventing them.

Treat docs/handoffs as read-only reference. Shared configuration/schema and root .env.example changes belong to M00; never add credential values.

**Own:** `src/ghostlab/contracts/`, `schemas/`, `examples/contracts/`, `tests/contracts/`, `docs/modules/M00.md`; root `.env.example`, `pyproject.toml`, `uv.lock`, runtime declarations and eventual `web/package.json` / `web/package-lock.json`. Other owners submit dependency requests to you. Do not edit simulation, providers or UI source.

Publish strict versioned boundaries for campaigns/tasks/envelopes/context, provider requests/replies, tools, events/results, invariant findings, diffs/capsules, memory, approvals, budgets, errors and artifacts. AgentEnvelope carries correlation/task/trace IDs, roles, objective, input references, expected schema, status and evidence/errors. Trusted principal and scope originate in the controller, never model-supplied claims. Use integer minor currency units; artifacts carry digest, size, media type and provenance. Keep internal simulation dataclasses model-library independent.

Phase A: resolve the source-linked candidates together in a clean environment. Candidate resolution files are evidence, not production locks. Record actual host/runtime versions; complete dependency installation, imports, schema checks and the existing core suite, then publish frozen interfaces and phase-A locks so downstream work can start. Generate frontend contract types from the canonical schemas rather than defining a second source of truth. Preserve v1 capsule loading or provide explicit migration tests.

Phase-A acceptance: baseline core tests stay green; unknown fields, invalid statuses, boolean amounts and malformed references fail; shared examples and schemas/types agree; clean frozen installs work. Phase B, with M09 after M05/M07/M08 exist: certify checkpoint/approval resume, API contracts, frontend typecheck/build and integrated behavior. Do not hold phase-A interfaces until those downstream tests exist. Report integration failures honestly. Do not treat dependency resolution as live Meta compatibility, and do not invent unverified version pins.

Return branch/PR, contract index and versions, owned files changed, actual commands/results, dependency decisions and unresolved gates. Publish the frozen interfaces before downstream modules begin implementation. Do not merge, deploy, make live inference calls or upgrade unrelated packages.
