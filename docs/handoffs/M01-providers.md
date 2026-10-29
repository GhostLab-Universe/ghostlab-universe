# M01 — Implement Meta transport and capability evidence

Implement only M01 in https://github.com/GhostLab-Universe/ghostlab-universe. Fetch `architecture-and-refund-core` / PR #1 and create `feature/m01-providers`. Read AGENTS.md, README.md, architecture.md and docs/handoffs/versions.md. M00's frozen contracts and dependency baseline are prerequisites. Preserve the existing refund engine and fourteen tests.

Use the latest approved integration commit containing your prerequisites and record its actual head. architecture-and-refund-core is the current initial integration base. If prerequisites are not integrated, stop implementation and report the missing interfaces rather than inventing them.

Treat docs/handoffs as read-only reference. Shared configuration/schema and root .env.example changes belong to M00; never add credential values.

**Own:** `src/ghostlab/providers/`, `scripts/probe_provider.py`, `tests/providers/`, `docs/modules/M01.md`. Request manifest/schema changes from M00; do not edit orchestration, simulation, memory or browser code.

Consume ModelRequest and ContextBundle; return normalized text, proposed tool calls, usage, opaque continuation references and typed errors. Build direct Meta Responses first. A conditional Baseten Chat Completions adapter has its own wire serializer and explicit configuration. Direct Meta documentation identifies `https://api.meta.ai/v1` and `muse-spark-1.3`; exact enabled/organizer-permitted model access still needs proof. The Baseten `-1026` suffix remains unverified. Never silently switch models or providers inside an experiment.

Pair tool results with exact provider call identifiers, preserve documented continuation data opaquely, validate responses locally and classify transient versus permanent errors. Take cancellation/deadline/budget handles from M05; never dispatch tools yourself. Keep credentials server-side and redacted. Start without streaming; enable it only after partial-argument/interruption tests pass.

Acceptance: offline transport fixtures test request shape, call/result pairing, invalid output, usage omissions, bounded retry, timeout and cancellation. Mark fixtures as tests, not live inference. An opt-in authenticated probe separately records exact endpoint/model/date, tool roundtrip, structured proposal validation, continuation and usage. No authorized key/model/budget means live status stays untested; never fabricate success. A retry must not silently repeat a world action.

Return branch/PR, adapter interface/version, redacted compatibility report, commands/results and unsupported features. Keep SDK changes behind the provider boundary and submit dependency requests to M00. Do not expose hidden reasoning, add another agent framework, make unapproved paid calls, merge or deploy.
