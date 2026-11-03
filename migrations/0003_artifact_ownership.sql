-- Artifact ownership. NULL marks a row written before ownership existed: it stays
-- readable through the unscoped API and is never visible to a project view.

ALTER TABLE artifacts ADD COLUMN project_id TEXT;

CREATE INDEX artifacts_by_project ON artifacts (project_id, artifact_id);
