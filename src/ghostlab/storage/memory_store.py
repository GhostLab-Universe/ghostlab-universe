"""SQLite MemoryStore (ghostlab-storage/1)."""

import sqlite3
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from ghostlab.contracts import MemoryQuery, MemoryRecord, TrustedScope

from . import sqlite as db
from .interfaces import Conflict, NotFound, ScopeViolation


def _columns(record: MemoryRecord) -> tuple:
    until = record.effective_until
    expires = record.retention.expires_at
    return (record.project_id, record.record_id, record.campaign_id, record.world_version,
            record.kind, record.status, db.timestamp_us(record.observed_at),
            db.timestamp_us(record.effective_from),
            None if until is None else db.timestamp_us(until),
            None if expires is None else db.timestamp_us(expires),
            int(record.retention.audit_hold), record.model_dump_json())


class SqliteMemoryStore:
    def __init__(self, path: Path):
        self.path = path

    def put(self, scope: TrustedScope, record: MemoryRecord) -> None:
        if record.project_id != scope.project_id:
            raise ScopeViolation("Record project does not match the caller's scope")
        try:
            with db.write(self.path) as conn:
                conn.execute("INSERT INTO memory_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                             _columns(record))
        except sqlite3.IntegrityError as error:
            raise Conflict("Memory record id already exists in this project") from error

    def get(self, scope: TrustedScope, record_id: str) -> MemoryRecord:
        with db.read(self.path) as conn:
            row = conn.execute("SELECT body FROM memory_records "
                               "WHERE project_id = ? AND record_id = ?",
                               (scope.project_id, record_id)).fetchone()
        if row is None:
            raise NotFound("Memory record not found")
        return db.load(MemoryRecord, row[0])

    def query(self, scope: TrustedScope, query: MemoryQuery) -> Sequence[MemoryRecord]:
        if query.project_id != scope.project_id:
            raise ScopeViolation("Query project does not match the caller's scope")
        as_of = db.timestamp_us(query.as_of)
        sql = ["SELECT body FROM memory_records WHERE project_id = ?",
               f"AND kind IN ({','.join('?' * len(query.kinds))})",
               f"AND status IN ({','.join('?' * len(query.statuses))})",
               "AND effective_from_us <= ?",
               "AND (effective_until_us IS NULL OR ? < effective_until_us)",
               "AND (expires_us IS NULL OR ? < expires_us)"]
        params = [scope.project_id, *query.kinds, *query.statuses, as_of, as_of, as_of]
        if query.world_version is not None:
            sql.append("AND world_version = ?")
            params.append(query.world_version)
        if query.campaign_id is not None:
            sql.append("AND campaign_id = ?")
            params.append(query.campaign_id)
        sql.append("ORDER BY observed_us DESC, record_id ASC LIMIT ?")
        params.append(query.limit)
        with db.read(self.path) as conn:
            rows = conn.execute(" ".join(sql), params).fetchall()
        return [db.load(MemoryRecord, row[0]) for row in rows]

    def transition(self, scope: TrustedScope, record_id: str, *,
                   expected_status, new_status) -> MemoryRecord:
        with db.write(self.path) as conn:
            row = conn.execute("SELECT body FROM memory_records "
                               "WHERE project_id = ? AND record_id = ?",
                               (scope.project_id, record_id)).fetchone()
            if row is None:
                raise NotFound("Memory record not found")
            current = db.load(MemoryRecord, row[0])
            if current.status != expected_status:
                raise Conflict("Memory record status changed concurrently")
            try:
                updated = db.revise(MemoryRecord, current, status=new_status)
            except ValidationError as error:
                raise Conflict("Transition violates memory record rules") from error
            conn.execute("UPDATE memory_records SET status = ?, body = ? "
                         "WHERE project_id = ? AND record_id = ?",
                         (updated.status, updated.model_dump_json(), scope.project_id,
                          record_id))
        return updated

    # --- M04 operations beyond ghostlab-storage/1 (used by the memory pipeline) ---

    def due_for_expiry(self, scope: TrustedScope, as_of: str) -> list[MemoryRecord]:
        """Records past retention that are not expired yet and not on audit hold."""
        with db.read(self.path) as conn:
            rows = conn.execute(
                "SELECT body FROM memory_records WHERE project_id = ? AND expires_us IS NOT NULL "
                "AND expires_us <= ? AND status != 'expired' AND audit_hold = 0 "
                "ORDER BY record_id", (scope.project_id, db.timestamp_us(as_of))).fetchall()
        return [db.load(MemoryRecord, row[0]) for row in rows]

    def purge_expired(self, scope: TrustedScope) -> int:
        """Delete expired records unless retained for audit or still referenced.

        A record named in another record's ``supersedes`` stays, so the history
        that explains a newer record remains consistent.
        """
        with db.write(self.path) as conn:
            rows = conn.execute("SELECT record_id, body FROM memory_records "
                                "WHERE project_id = ?", (scope.project_id,)).fetchall()
            records = {rid: db.load(MemoryRecord, body) for rid, body in rows}
            referenced = {old for r in records.values() for old in r.supersedes}
            doomed = [rid for rid, r in records.items()
                      if r.status == "expired" and not r.retention.audit_hold
                      and r.retention.policy != "audit" and rid not in referenced]
            conn.executemany("DELETE FROM memory_records WHERE project_id = ? AND record_id = ?",
                             [(scope.project_id, rid) for rid in doomed])
        return len(doomed)
