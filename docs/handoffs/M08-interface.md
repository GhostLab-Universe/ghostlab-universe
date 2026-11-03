# M08 — Show real experiments and reviewable approvals

Implement only M08 in https://github.com/GhostLab-Universe/ghostlab-universe. Fetch `architecture-and-refund-core` / PR #1 and create `feature/m08-interface`. Read AGENTS.md, README.md, architecture.md and docs/handoffs/versions.md. Consume M00-generated contracts and M07 OpenAPI/service routes; request frontend dependency/lock updates from M00. Preserve the existing core and fourteen tests.

Use the latest approved integration commit containing your prerequisites and record its actual head. architecture-and-refund-core is the current initial integration base. For contract-only component development, approved M00 schemas/fixtures are sufficient even when M07 is not integrated. Stop only backend-dependent integration/live acceptance and report the missing API; never invent its contract or claim fixture results are live.

Treat docs/handoffs as read-only reference. Shared configuration/schema and root .env.example changes belong to M00; never add credential values.

**Own:** `web/src/`, `web/public/`, `web/tests/`, `web/vite.config.ts`, `web/tsconfig*.json`, `docs/modules/M08.md`. Do not edit backend packages, canonical schemas, root manifests or lockfiles independently. Contract fixtures may support UI tests before M07 is ready, but must be visibly distinct from actual experiment results and excluded from live-result claims.

Build a campaign view with objective, budget/status and task/dependency timeline; an evidence view with ordered tools/events and linked findings; side-by-side baseline/fork Reality Diff with first divergence and ledger totals; capsule export/replay controls; and exact approval/rejection views. Display baseline/fault/repair results obtained from the service. Financial verdicts come from the verifier, not frontend arithmetic or model prose.

Support loading, empty, validation-error, denied, failed, cancelled, reconnecting and awaiting-approval states. On reconnect, fetch authoritative state before resuming event rendering. Approval UI shows the exact action, scope and material risk; submit only the bound decision through M07. Do not let hidden buttons or client role fields substitute for server authorization. Never put model credentials or unrestricted provider calls in the browser.

Acceptance: type checking and production build pass using the M00-supported set; meaningful browser tests exercise the refund/diff/replay flow and error/approval states; stale events do not overwrite newer status; denied actions remain denied; users can identify the first divergence and inspect its evidence. Test keyboard access and narrow screens. Record API-dependent tests separately if the backend is unavailable; do not report fixture tests as integrated success.

Return branch/PR, view and contract index, build/browser commands with actual results, screenshots and remaining integration gaps. No invented charts/benchmark results, extra UI framework without need, schema forks, dependency upgrades, merge or deployment.
