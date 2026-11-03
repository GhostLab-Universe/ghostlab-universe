# M02 — Extend the existing executable laboratory

Implement only M02 in https://github.com/GhostLab-Universe/ghostlab-universe. Fetch `architecture-and-refund-core` / PR #1 and create `feature/m02-simulation`. Read AGENTS.md, README.md, architecture.md, docs/handoffs/versions.md and the existing core/tests. Preserve all fourteen baseline tests and their demonstrated refund/replay behavior. Do not rebuild this completed slice.

Use the latest approved integration commit containing your prerequisites and record its actual head. architecture-and-refund-core is the current initial integration base. If prerequisites are not integrated, stop implementation and report the missing interfaces rather than inventing them.

Treat docs/handoffs as read-only reference. Shared configuration/schema and root .env.example changes belong to M00; never add credential values.

**Own:** existing `src/ghostlab/core.py`, `capsule.py`, `cli.py`, `__main__.py`; new `src/ghostlab/worlds/`, `tools/`, `execution/`, `tests/test_core.py`, `tests/simulation/`, `docs/modules/M02.md`. M00 supplies versioned boundaries; M06 supplies authorization interfaces and M05 budget/cancellation handles. Other owners cannot edit these files without agreement.

Adapt the current world, deterministic target adapters, verifier, diff and replay to frozen contracts. Keep the three tools typed and allowlisted: get_order, issue_refund and get_refund_status. The controller binds the caller; tool arguments cannot change identity. Each fork starts from the same immutable fixture in independent state. Emit authoritative ordered events and derive verdicts from ledger/state, never model confidence.

Define ExecutionBackend with a trusted LocalBackend. Unknown-code upload stays disabled. Capsules remain versioned data: fixture, principal, scenario, adapter/world versions, ordered actions/results, state and expected checks. Preserve v1 import or migrate explicitly. Recorded replay makes no inference call; graph resume is a different operation. Intentional business vulnerabilities exist only within the synthetic world and cannot weaken host boundaries.

Acceptance: unchanged baseline/fault/repair results; no state aliasing; spoofing/unknown tools/malformed arguments denied; pre-call budget and cancellation checks; uncertain commits do not duplicate actions; first divergence is evidence-linked; original and nearby cases reproduce; altered/malformed evidence fails cleanly. A future isolated backend must pass network/filesystem/resource/secrets/evidence tests before enabling arbitrary programs.

Return branch/PR, compatibility changes, capsule examples, test output and remaining limitations. Keep the core independent of provider SDKs, Pydantic service types and UI. Do not add live payment connectors, fabricate model results, alter another module's files, merge or deploy.
