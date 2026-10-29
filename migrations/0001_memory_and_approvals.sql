-- ghostlab-storage/1: scoped memory records and approval transitions.
-- Bodies are M00 contract JSON; indexed columns are copies used for filtering.

CREATE TABLE memory_records (
    project_id        TEXT NOT NULL,
    record_id         TEXT NOT NULL,
    campaign_id       TEXT,
    world_version     TEXT NOT NULL,
    kind              TEXT NOT NULL,
    status            TEXT NOT NULL,
    observed_us       INTEGER NOT NULL,
    effective_from_us INTEGER NOT NULL,
    effective_until_us INTEGER,
    expires_us        INTEGER,
    audit_hold        INTEGER NOT NULL CHECK (audit_hold IN (0, 1)),
    body              TEXT NOT NULL,
    PRIMARY KEY (project_id, record_id)
);

CREATE TABLE approvals (
    project_id    TEXT NOT NULL,
    approval_id   TEXT NOT NULL,
    status        TEXT NOT NULL,
    action_digest TEXT NOT NULL,
    body          TEXT NOT NULL,
    decision_body TEXT,
    PRIMARY KEY (project_id, approval_id)
);
