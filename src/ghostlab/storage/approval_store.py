"""SQLite ApprovalStore (ghostlab-storage/1). Records transitions; M06 decides."""

import sqlite3
from pathlib import Path

from ghostlab.contracts import ApprovalDecision, ApprovalRequest

from . import sqlite as db
from .interfaces import Conflict, NotFound


class SqliteApprovalStore:
    def __init__(self, path: Path):
        self.path = path

    def create(self, request: ApprovalRequest) -> None:
        if request.status != "pending":
            raise Conflict("Only pending approval requests can be created")
        try:
            with db.write(self.path) as conn:
                conn.execute("INSERT INTO approvals VALUES (?,?,?,?,?,NULL)",
                             (request.scope.project_id, request.approval_id, request.status,
                              request.action_digest, request.model_dump_json()))
        except sqlite3.IntegrityError as error:
            raise Conflict("Approval id already exists in this project") from error

    def get(self, project_id: str, approval_id: str) -> ApprovalRequest:
        with db.read(self.path) as conn:
            return self._load(conn, project_id, approval_id)

    def list_pending(self, project_id: str, *, after: str = "", limit: int = 50):
        """Bounded keyset page; authorization belongs to the API/policy layer."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("approval page limit must be 1..100")
        with db.read(self.path) as conn:
            rows = conn.execute(
                "SELECT body FROM approvals WHERE project_id = ? AND status = 'pending' "
                "AND approval_id > ? ORDER BY approval_id LIMIT ?",
                (project_id, after, limit)).fetchall()
        return [db.load(ApprovalRequest, row[0]) for row in rows]

    def get_decision(self, project_id: str, approval_id: str) -> ApprovalDecision | None:
        with db.read(self.path) as conn:
            row = self._row(conn, project_id, approval_id)
        return None if row[1] is None else db.load(ApprovalDecision, row[1])

    def record_decision(self, project_id: str, decision: ApprovalDecision, *,
                        new_status) -> ApprovalRequest:
        expected = "approved" if decision.decision == "approve" else "rejected"
        if new_status != expected:
            raise Conflict("Status does not match the decision")
        with db.write(self.path) as conn:
            current = self._load(conn, project_id, decision.approval_id)
            if current.status != "pending":
                raise Conflict("Approval is no longer pending")
            if decision.action_digest != current.action_digest:
                raise Conflict("Decision is bound to a different action")
            updated = db.revise(ApprovalRequest, current, status=new_status)
            conn.execute("UPDATE approvals SET status = ?, body = ?, decision_body = ? "
                         "WHERE project_id = ? AND approval_id = ? AND status = 'pending'",
                         (new_status, updated.model_dump_json(), decision.model_dump_json(),
                          project_id, decision.approval_id))
        return updated

    def mark_expired(self, project_id: str, approval_id: str) -> ApprovalRequest:
        return self._move(project_id, approval_id, "pending", "expired")

    def consume(self, project_id: str, approval_id: str, action_digest: str) -> ApprovalRequest:
        return self._move(project_id, approval_id, "approved", "consumed",
                          action_digest=action_digest)

    def _move(self, project_id, approval_id, source, target, *, action_digest=None):
        with db.write(self.path) as conn:
            current = self._load(conn, project_id, approval_id)
            if current.status != source:
                raise Conflict(f"Approval is not {source}")
            if action_digest is not None and action_digest != current.action_digest:
                raise Conflict("Approval is bound to a different action")
            updated = db.revise(ApprovalRequest, current, status=target)
            conn.execute("UPDATE approvals SET status = ?, body = ? "
                         "WHERE project_id = ? AND approval_id = ? AND status = ?",
                         (target, updated.model_dump_json(), project_id, approval_id, source))
        return updated

    @staticmethod
    def _row(conn, project_id, approval_id):
        row = conn.execute("SELECT body, decision_body FROM approvals "
                           "WHERE project_id = ? AND approval_id = ?",
                           (project_id, approval_id)).fetchone()
        if row is None:
            raise NotFound("Approval not found")
        return row

    def _load(self, conn, project_id, approval_id) -> ApprovalRequest:
        return db.load(ApprovalRequest, self._row(conn, project_id, approval_id)[0])
