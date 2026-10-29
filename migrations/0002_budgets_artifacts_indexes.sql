-- Budget reservations, artifact metadata and memory retrieval indexes.

CREATE TABLE budget_reservations (
    reservation_id TEXT PRIMARY KEY,
    campaign_id    TEXT NOT NULL,
    resource       TEXT NOT NULL,
    amount         INTEGER NOT NULL CHECK (amount > 0),
    status         TEXT NOT NULL,
    actual         INTEGER,
    body           TEXT NOT NULL
);
CREATE INDEX budget_by_campaign ON budget_reservations (campaign_id, resource, status);

CREATE TABLE artifacts (
    artifact_id TEXT PRIMARY KEY,
    sha256      TEXT NOT NULL,
    size_bytes  INTEGER NOT NULL,
    body        TEXT NOT NULL
);

CREATE INDEX memory_by_scope ON memory_records (project_id, kind, status, observed_us);
