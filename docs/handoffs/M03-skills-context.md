# M03 — Add versioned skills and scoped context

Implement only M03 in https://github.com/GhostLab-Universe/ghostlab-universe. Fetch `architecture-and-refund-core` / PR #1 and create `feature/m03-skills-context`. Read AGENTS.md, README.md, architecture.md and docs/handoffs/versions.md. Depend on M00 SkillSpec/ContextBundle/ArtifactRef contracts and M04's scoped memory-query interface. Preserve the existing core and fourteen tests.

Use the latest approved integration commit containing your prerequisites and record its actual head. architecture-and-refund-core is the current initial integration base. If prerequisites are not integrated, stop implementation and report the missing interfaces rather than inventing them.

Treat docs/handoffs as read-only reference. Shared configuration/schema and root .env.example changes belong to M00; never add credential values.

**Own:** `src/ghostlab/skills/`, `src/ghostlab/context/`, root `skills/`, `prompts/`, `tests/context/`, `docs/modules/M03.md`. Do not change memory storage, model transport, graph control, shared schemas or dependency locks. Request interface changes from their owners.

Build a versioned skill registry. Each procedure states purpose, preconditions, allowed capabilities, steps, output schema and stop conditions. Initial procedures cover proposing a refund-world scenario, interpreting evidence and suggesting an idempotency repair. Procedures cannot grant permissions or turn generated code into trusted execution. Record skill and prompt versions for every invocation.

Compile ContextBundles per role: objective/success criteria, trusted role/scope, prerequisite result references, selected fresh findings, relevant skill, schemas, recent tool observations and unresolved questions. M04 owns retrieval/storage; you select and format its authorized results. Retain provenance, timestamps and trust labels. Keep large outputs as artifact references, and bound token/context size before calling M01. Summarize completed work with evidence references; do not store or reconstruct hidden chain-of-thought.

Acceptance: Explorer receives relevant experiment evidence rather than the full project history; the context budget is enforced; stale/cross-project findings are excluded; source conflicts stay visible; malicious instructions in tool results cannot modify trusted instructions or capability scope. Version changes are reviewable and deterministic fixture tests validate bundle shape and selection. Test that referenced evidence remains retrievable after summary generation.

Return branch/PR, skill/context schemas consumed, initial procedures, selection rules, commands/results and gaps. Do not add a vector database, implicit global memory, live provider calls, self-modifying prompts or a second orchestrator. Submit dependency/schema requests through M00; do not merge or deploy.
