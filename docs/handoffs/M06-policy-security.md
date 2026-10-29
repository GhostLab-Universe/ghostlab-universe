# M06 — Enforce policy, approvals and execution boundaries

Implement only M06 in https://github.com/GhostLab-Universe/ghostlab-universe. Fetch `architecture-and-refund-core` / PR #1 and create `feature/m06-policy-security`. Read AGENTS.md, README.md, architecture.md and docs/handoffs/versions.md. Depend on M00 trusted identity/policy/approval contracts and M04 persistence interfaces. Preserve the existing bound-caller protections and fourteen core tests.

Use the latest approved integration commit containing your prerequisites and record its actual head. architecture-and-refund-core is the current initial integration base. If prerequisites are not integrated, stop implementation and report the missing interfaces rather than inventing them.

Treat docs/handoffs as read-only reference. Shared configuration/schema and root .env.example changes belong to M00; never add credential values.

**Own:** `src/ghostlab/security/`, `src/ghostlab/policy/`, `tests/policy/`, `docs/modules/M06.md`. M07 derives identity from sessions; you evaluate that identity. M05 owns budget accounting; you supply per-role ceilings. M02 owns tool implementations/ExecutionBackend; you define and test their permission boundary. Do not independently change those packages.

Implement deny-by-default tool/resource authorization with project, principal, role and delegation scope. A worker cannot inherit every supervisor permission. Tool arguments, retrieved text and model output cannot establish identity or widen permissions. Define secret-redaction and data-isolation policies. Keep host protection distinct from intentionally vulnerable synthetic business behavior.

Own the approval domain: bind a decision to approver identity, exact action/payload digest, scope, policy version, expiry and single-use consumption. M04 persists atomically; M05 pauses/resumes; M07 exposes routes and M08 displays decisions. A changed action requires a new decision. Consequential patch/policy execution follows explicit approval, never a model's confidence.

Acceptance: cross-project access, caller spoofing, delegated privilege escalation and unknown tools are denied; changed/expired/replayed approvals fail; denial causes no mutation; logs redact secrets; concurrency cannot consume approval twice. Document the trusted-local development boundary and test it cannot be mistaken for production authentication. Unknown code remains disabled until one backend passes network/filesystem/resource/secrets/evidence isolation tests.

NeMo content screening and OpenShell isolation are optional, separately qualified adapters. Their trigger, actual host support and bypass/false-block or enforcement tests must be documented before adoption; neither replaces authorization or the verifier. Return branch/PR, threat/boundary map, policy interfaces, approval lifecycle, commands/results and gaps. No live secrets, security-setting changes, additional guardrail products, merge or deployment.
