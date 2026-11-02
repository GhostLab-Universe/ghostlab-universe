"""Project-scoped persistence for candidates, evaluations, promotions.

SQLite for the serial local coordinator; each developer keeps a local
file and tests use a private temporary database. Live database files
are never committed or shared. Every record carries ``project_id`` and
every read takes the caller's project: cross-project access is refused,
and listings only show the caller's project.

Candidates are content-addressed (identical proposes are idempotent).
Status moves through compare-and-set transitions only; stored digests
are verified on read so corruption fails cleanly instead of promoting.
Standard library only.
"""

import contextlib
import json
import sqlite3
from pathlib import Path

from .records import (MAX_RECORD_BYTES, CandidateError, check_project_id,
                      digest_of, scan_for_secrets, validate_candidate,
                      validate_evaluation, validate_promotion)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
  candidate_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  body_json TEXT NOT NULL,
  digest TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_candidates_project
  ON candidates (project_id);
CREATE TABLE IF NOT EXISTS evaluations (
  evaluation_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  candidate_id TEXT NOT NULL,
  body_json TEXT NOT NULL,
  digest TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evaluations_candidate
  ON evaluations (project_id, candidate_id);
CREATE TABLE IF NOT EXISTS promotions (
  promotion_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  candidate_id TEXT NOT NULL,
  body_json TEXT NOT NULL,
  digest TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_promotions_candidate
  ON promotions (project_id, candidate_id);
"""

# Allowed status edges; rejected is terminal for candidates.
_CANDIDATE_EDGES = {
    "proposed": ("validated", "rejected"),
    "validated": ("evaluated", "rejected"),
    "evaluated": ("promotion_requested", "rejected"),
    "promotion_requested": ("promoted", "evaluated", "rejected"),
    "promoted": (),
    "rejected": (),
}
_PROMOTION_EDGES = {
    "pending": ("completed", "rejected", "expired"),
    "completed": (),
    "rejected": (),
    "expired": (),
}


class CrossProjectError(PermissionError):
    """A record exists but belongs to a different project."""


def _dump(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)


class CandidateStore:
    """Project-scoped candidate storage; one file per local coordinator."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    @contextlib.contextmanager
    def _connect(self):
        """Yield a connection that is committed/rolled back and closed.

        A bare ``with sqlite3.connect(...)`` block never closes, which
        keeps the file locked on Windows; closing explicitly releases
        it promptly.
        """
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _checked(body: dict, label: str) -> str:
        text = _dump(body)
        if len(text.encode()) > MAX_RECORD_BYTES:
            raise CandidateError(f"{label} exceeds the size bound")
        scan_for_secrets({k: v for k, v in body.items() if k != "digest"},
                         label)
        return text

    # -- candidates ----------------------------------------------------

    def save_candidate(self, *, project_id: str, body: dict) -> dict:
        """Store a validated candidate; identical proposes are idempotent."""
        check_project_id(project_id)
        clean = validate_candidate(body)
        if clean["project_id"] != project_id:
            raise CrossProjectError("candidate names a different project")
        if digest_of(clean) != clean["digest"]:
            raise CandidateError("candidate digest mismatch")
        text = self._checked(clean, "candidate")
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO candidates VALUES"
                " (:id, :project, :body, :digest, :status, :at)",
                {"id": clean["candidate_id"], "project": project_id,
                 "body": text, "digest": clean["digest"],
                 "status": clean["status"], "at": clean["created_at"]})
        return self.get_candidate(project_id, clean["candidate_id"])

    def get_candidate(self, project_id: str, candidate_id: str) -> dict:
        check_project_id(project_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM candidates WHERE candidate_id = ?",
                (candidate_id,)).fetchone()
        if row is None:
            raise LookupError(f"unknown candidate '{candidate_id}'")
        if row["project_id"] != project_id:
            raise CrossProjectError(
                f"candidate '{candidate_id}' belongs to a different project")
        body = validate_candidate(json.loads(row["body_json"]))
        if digest_of(body) != row["digest"]:
            raise CandidateError("candidate digest mismatch")
        return body

    def list_candidates(self, project_id: str) -> list:
        check_project_id(project_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM candidates WHERE project_id = ?"
                " ORDER BY created_at, candidate_id", (project_id,)).fetchall()
        records = []
        for row in rows:
            body = validate_candidate(json.loads(row["body_json"]))
            if digest_of(body) != row["digest"]:
                raise CandidateError("candidate digest mismatch")
            records.append(body)
        return records

    def transition_candidate(self, project_id: str, candidate_id: str, *,
                             expected: str, new_status: str,
                             rejection=None) -> dict:
        """Move a candidate along one allowed edge (compare-and-set)."""
        check_project_id(project_id)
        if new_status not in _CANDIDATE_EDGES.get(expected, ()):
            raise CandidateError(
                f"candidate cannot move from {expected} to {new_status}")
        current = self.get_candidate(project_id, candidate_id)
        if current["status"] != expected:
            raise CandidateError(
                f"candidate is {current['status']}, not {expected}")
        updated = dict(current)
        updated["status"] = new_status
        updated["rejection"] = rejection
        updated = validate_candidate({**updated, "digest": "pending"})
        updated["digest"] = digest_of(updated)
        text = self._checked(updated, "candidate")
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE candidates SET body_json = :body, digest = :digest,"
                " status = :new WHERE candidate_id = :id AND status = :old",
                {"body": text, "digest": updated["digest"], "new": new_status,
                 "id": candidate_id, "old": expected})
            if cursor.rowcount != 1:
                raise CandidateError("candidate moved concurrently")
        return updated

    # -- evaluations ---------------------------------------------------

    def save_evaluation(self, *, project_id: str, body: dict) -> dict:
        """Store one validated evaluation; ids are unique per project."""
        check_project_id(project_id)
        clean = validate_evaluation(body)
        if clean["project_id"] != project_id:
            raise CrossProjectError("evaluation names a different project")
        if digest_of(clean) != clean["digest"]:
            raise CandidateError("evaluation digest mismatch")
        text = self._checked(clean, "evaluation")
        with self._connect() as connection:
            try:
                connection.execute(
                    "INSERT INTO evaluations VALUES"
                    " (:id, :project, :cand, :body, :digest, :at)",
                    {"id": clean["evaluation_id"], "project": project_id,
                     "cand": clean["candidate_id"], "body": text,
                     "digest": clean["digest"], "at": clean["evaluated_at"]})
            except sqlite3.IntegrityError as error:
                raise CandidateError(
                    f"evaluation '{clean['evaluation_id']}' already exists"
                ) from error
        return self.get_evaluation(project_id, clean["evaluation_id"])

    def get_evaluation(self, project_id: str, evaluation_id: str) -> dict:
        check_project_id(project_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM evaluations WHERE evaluation_id = ?",
                (evaluation_id,)).fetchone()
        if row is None:
            raise LookupError(f"unknown evaluation '{evaluation_id}'")
        if row["project_id"] != project_id:
            raise CrossProjectError(
                f"evaluation '{evaluation_id}' belongs to a different project")
        body = validate_evaluation(json.loads(row["body_json"]))
        if digest_of(body) != row["digest"]:
            raise CandidateError("evaluation digest mismatch")
        return body

    def list_evaluations(self, project_id: str,
                         candidate_id: str | None = None) -> list:
        check_project_id(project_id)
        with self._connect() as connection:
            if candidate_id is None:
                rows = connection.execute(
                    "SELECT * FROM evaluations WHERE project_id = ?"
                    " ORDER BY created_at, evaluation_id",
                    (project_id,)).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM evaluations WHERE project_id = ?"
                    " AND candidate_id = ? ORDER BY created_at, evaluation_id",
                    (project_id, candidate_id)).fetchall()
        records = []
        for row in rows:
            body = validate_evaluation(json.loads(row["body_json"]))
            if digest_of(body) != row["digest"]:
                raise CandidateError("evaluation digest mismatch")
            records.append(body)
        return records

    # -- promotions ----------------------------------------------------

    def save_promotion(self, *, project_id: str, body: dict) -> dict:
        """Store one promotion; one row per (candidate, approval)."""
        check_project_id(project_id)
        clean = validate_promotion(body)
        if clean["project_id"] != project_id:
            raise CrossProjectError("promotion names a different project")
        if digest_of(clean) != clean["digest"]:
            raise CandidateError("promotion digest mismatch")
        text = self._checked(clean, "promotion")
        with self._connect() as connection:
            try:
                connection.execute(
                    "INSERT INTO promotions VALUES"
                    " (:id, :project, :cand, :body, :digest, :status, :at)",
                    {"id": clean["promotion_id"], "project": project_id,
                     "cand": clean["candidate_id"], "body": text,
                     "digest": clean["digest"], "status": clean["status"],
                     "at": clean["decided_at"] or ""})
            except sqlite3.IntegrityError as error:
                raise CandidateError(
                    f"promotion '{clean['promotion_id']}' already exists"
                ) from error
        return self.get_promotion(project_id, clean["promotion_id"])

    def get_promotion(self, project_id: str, promotion_id: str) -> dict:
        check_project_id(project_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM promotions WHERE promotion_id = ?",
                (promotion_id,)).fetchone()
        if row is None:
            raise LookupError(f"unknown promotion '{promotion_id}'")
        if row["project_id"] != project_id:
            raise CrossProjectError(
                f"promotion '{promotion_id}' belongs to a different project")
        body = validate_promotion(json.loads(row["body_json"]))
        if digest_of(body) != row["digest"]:
            raise CandidateError("promotion digest mismatch")
        return body

    def list_promotions(self, project_id: str,
                        candidate_id: str | None = None) -> list:
        check_project_id(project_id)
        with self._connect() as connection:
            if candidate_id is None:
                rows = connection.execute(
                    "SELECT * FROM promotions WHERE project_id = ?"
                    " ORDER BY promotion_id", (project_id,)).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM promotions WHERE project_id = ?"
                    " AND candidate_id = ? ORDER BY promotion_id",
                    (project_id, candidate_id)).fetchall()
        records = []
        for row in rows:
            body = validate_promotion(json.loads(row["body_json"]))
            if digest_of(body) != row["digest"]:
                raise CandidateError("promotion digest mismatch")
            records.append(body)
        return records

    def transition_promotion(self, project_id: str, promotion_id: str, *,
                             new_status: str, decided_by: str,
                             decided_at: str) -> dict:
        """Move a pending promotion to one terminal state (compare-and-set)."""
        check_project_id(project_id)
        if new_status not in _PROMOTION_EDGES["pending"]:
            raise CandidateError(
                f"promotion cannot move to {new_status}")
        current = self.get_promotion(project_id, promotion_id)
        if current["status"] != "pending":
            raise CandidateError(
                f"promotion is {current['status']}, not pending")
        updated = dict(current)
        updated["status"] = new_status
        updated["decided_by"] = decided_by
        updated["decided_at"] = decided_at
        updated = validate_promotion({**updated, "digest": "pending"})
        updated["digest"] = digest_of(updated)
        text = self._checked(updated, "promotion")
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE promotions SET body_json = :body, digest = :digest,"
                " status = :new WHERE promotion_id = :id AND status = 'pending'",
                {"body": text, "digest": updated["digest"],
                 "new": new_status, "id": promotion_id})
            if cursor.rowcount != 1:
                raise CandidateError("promotion moved concurrently")
        return updated


__all__ = ["CandidateStore", "CrossProjectError"]
