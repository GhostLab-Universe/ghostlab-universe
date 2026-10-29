# M04 — Persist state, memory and evidence separately

Implement only M04 in https://github.com/GhostLab-Universe/ghostlab-universe. Fetch `architecture-and-refund-core` / PR #1 and create `feature/m04-memory-persistence`. Read AGENTS.md, README.md, architecture.md and docs/handoffs/versions.md. M00's versioned records and frozen dependencies are prerequisites. Keep the simulation's fourteen tests passing without making it depend on a running database.

Use the latest approved integration commit containing your prerequisites and record its actual head. architecture-and-refund-core is the current initial integration base. If prerequisites are not integrated, stop implementation and report the missing interfaces rather than inventing them.

Treat docs/handoffs as read-only reference. Shared configuration/schema and root .env.example changes belong to M00; never add credential values.

**Own:** `src/ghostlab/storage/`, `src/ghostlab/memory/`, `migrations/`, `tests/storage/`, `docs/modules/M04.md`. M05 consumes checkpoint/state and atomic budget operations; M06 consumes approval persistence; M03 consumes scoped memory queries. You own storage transactions, not approval policy, context formatting or budget decisions.

Separate workflow state/checkpoints, episodic events, structured findings, approved preferences, procedural-version references and artifact bytes. Implement memory capture, filtering, normalization, storage, selective retrieval, update/contradiction handling and expiry. Records carry project/campaign scope, provenance, observed/effective times, confidence or verification status and retention metadata. Untrusted observations never become permanent verified facts automatically.

Use SQLite for one local coordinator with explicit transaction/concurrency behavior. Provide repository interfaces for later Postgres. Store large artifacts by content digest with validated metadata and bounded reads. Provide atomic compare-and-set transitions, approval consume operations and budget reservations as requested by domain owners. Integrate the selected LangGraph saver without conflating graph checkpoints with world snapshots or capsule replay.

Acceptance: restart recovers saved workflow references; no lost updates under tested contention; project isolation blocks unauthorized reads; expired/contradictory memory is handled predictably; approval consumption and budget persistence are atomic; artifact corruption/missing bytes fails cleanly. Retention/deletion rules preserve defined audit consistency. Schema migration tests preserve existing supported data. Tests use temporary local stores, never shared credentials or production records.

Return branch/PR, repository contracts, schema/migration policy, actual storage/concurrency tests and unsupported deployment modes. Each developer retains a local database; never commit or synchronize a live SQLite file. Do not add Redis, vector retrieval or an ORM without a demonstrated requirement and M00 approval. Do not merge or deploy.
