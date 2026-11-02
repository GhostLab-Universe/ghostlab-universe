"""Project-scoped persistence for experiment configs and runs.

SQLite for the serial local coordinator; each developer keeps a local file and
tests use a private temporary database. Live database files are never
committed or shared. Every record carries ``project_id`` and every read takes
the caller's project: cross-project access is refused, and listings only show
the caller's project.

Configs are content-addressed (identical creates are idempotent) and resolved
configs expose no update path: they are immutable once stored. Stored digests
are verified on read so corruption fails cleanly instead of executing.
"""

import contextlib
import json
import sqlite3
from copy import deepcopy
from pathlib import Path

from .configs import (MAX_CONFIG_BYTES, canonical_digest, check_project_id,
                      requested_config_id, scan_for_secrets)

MAX_EVIDENCE_BYTES = 2_000_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS requested_configs (
  config_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  name TEXT NOT NULL,
  config_json TEXT NOT NULL,
  digest TEXT NOT NULL,
  requested_by TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_requested_project
  ON requested_configs (project_id);
CREATE TABLE IF NOT EXISTS resolved_configs (
  resolved_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  requested_id TEXT NOT NULL,
  resolved_json TEXT NOT NULL,
  digest TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_resolved_project
  ON resolved_configs (project_id);
CREATE TABLE IF NOT EXISTS experiment_runs (
  run_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  resolved_id TEXT NOT NULL,
  repetition INTEGER NOT NULL,
  attempt INTEGER NOT NULL,
  run_json TEXT NOT NULL,
  digest TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (resolved_id, repetition, attempt)
);
CREATE INDEX IF NOT EXISTS idx_runs_project ON experiment_runs (project_id);
CREATE TABLE IF NOT EXISTS budget_ledger (
  entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL,
  resolved_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  resource TEXT NOT NULL,
  amount INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_resolved
  ON budget_ledger (project_id, resolved_id);
"""


class CrossProjectError(PermissionError):
    """A record exists but belongs to a different project."""


def _dump(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)


class ExperimentStore:
    """Project-scoped experiment storage; one file per local coordinator."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    @contextlib.contextmanager
    def _connect(self):
        """Yield a connection that is committed/rolled back and closed.

        A bare ``with sqlite3.connect(...)`` block never closes, which keeps
        the file locked on Windows; closing explicitly releases it promptly.
        """
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def save_requested(self, *, project_id: str, body: dict, digest: str,
                       requested_by: str, created_at: str) -> dict:
        """Store a validated requested body; identical creates are idempotent."""
        check_project_id(project_id)
        text = _dump(body)
        if len(text.encode()) > MAX_CONFIG_BYTES:
            raise ValueError("requested config exceeds the size bound")
        scan_for_secrets(body, "requested config")
        config_id = requested_config_id(project_id, body)
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO requested_configs VALUES"
                " (:id, :project, :name, :body, :digest, :by, :at)",
                {"id": config_id, "project": project_id,
                 "name": body["name"], "body": text, "digest": digest,
                 "by": requested_by, "at": created_at})
        return self.get_requested(project_id, config_id)

    def get_requested(self, project_id: str, config_id: str) -> dict:
        check_project_id(project_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM requested_configs WHERE config_id = ?",
                (config_id,)).fetchone()
        if row is None:
            raise LookupError(f"unknown requested config '{config_id}'")
        if row["project_id"] != project_id:
            raise CrossProjectError(
                f"requested config '{config_id}' belongs to a different"
                " project")
        body = json.loads(row["config_json"])
        if canonical_digest(body) != row["digest"]:
            raise ValueError("requested config digest mismatch")
        return {"config_id": row["config_id"], "project_id": row["project_id"],
                "name": row["name"], "config": body, "digest": row["digest"],
                "requested_by": row["requested_by"],
                "created_at": row["created_at"]}

    def list_requested(self, project_id: str) -> list:
        check_project_id(project_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM requested_configs WHERE project_id = ?"
                " ORDER BY created_at, config_id", (project_id,)).fetchall()
        records = []
        for row in rows:
            body = json.loads(row["config_json"])
            if canonical_digest(body) != row["digest"]:
                raise ValueError("requested config digest mismatch")
            records.append({"config_id": row["config_id"],
                            "project_id": row["project_id"],
                            "name": row["name"], "config": body,
                            "digest": row["digest"],
                            "requested_by": row["requested_by"],
                            "created_at": row["created_at"]})
        return records

    def save_resolved(self, *, project_id: str, requested_id: str,
                      body: dict, digest: str, created_at: str) -> dict:
        """Store an immutable resolved body; identical resolves are idempotent."""
        check_project_id(project_id)
        text = _dump(body)
        if len(text.encode()) > MAX_CONFIG_BYTES:
            raise ValueError("resolved config exceeds the size bound")
        scan_for_secrets(body, "resolved config")
        resolved_id = body["resolved_config_id"]
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO resolved_configs VALUES"
                " (:id, :project, :requested, :body, :digest, :at)",
                {"id": resolved_id, "project": project_id,
                 "requested": requested_id, "body": text, "digest": digest,
                 "at": created_at})
        return self.get_resolved(project_id, resolved_id)

    def get_resolved(self, project_id: str, resolved_id: str) -> dict:
        check_project_id(project_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM resolved_configs WHERE resolved_id = ?",
                (resolved_id,)).fetchone()
        if row is None:
            raise LookupError(f"unknown resolved config '{resolved_id}'")
        if row["project_id"] != project_id:
            raise CrossProjectError(
                f"resolved config '{resolved_id}' belongs to a different"
                " project")
        body = json.loads(row["resolved_json"])
        if canonical_digest({k: v for k, v in body.items()
                             if k != "digest"}) != row["digest"]:
            raise ValueError("resolved config digest mismatch")
        return {"resolved_id": row["resolved_id"],
                "project_id": row["project_id"],
                "requested_id": row["requested_id"], "resolved": body,
                "digest": row["digest"], "created_at": row["created_at"]}

    def list_resolved(self, project_id: str) -> list:
        check_project_id(project_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM resolved_configs WHERE project_id = ?"
                " ORDER BY created_at, resolved_id", (project_id,)).fetchall()
        records = []
        for row in rows:
            body = json.loads(row["resolved_json"])
            if canonical_digest({k: v for k, v in body.items()
                                 if k != "digest"}) != row["digest"]:
                raise ValueError("resolved config digest mismatch")
            records.append({"resolved_id": row["resolved_id"],
                            "project_id": row["project_id"],
                            "requested_id": row["requested_id"],
                            "resolved": body, "digest": row["digest"],
                            "created_at": row["created_at"]})
        return records

    def attempt_recorded(self, resolved_id: str, repetition: int,
                         attempt: int) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT run_id FROM experiment_runs WHERE resolved_id = ?"
                " AND repetition = ? AND attempt = ?",
                (resolved_id, repetition, attempt)).fetchone()
        return row is not None

    @staticmethod
    def _insert_run(connection, *, project_id: str, resolved_id: str,
                    repetition: int, attempt: int, body: dict,
                    created_at: str) -> str:
        """Insert one run row on an open connection; returns the run id."""
        cursor = connection.execute(
            "SELECT COALESCE(MAX(CAST(SUBSTR(run_id, 5) AS INTEGER)), 0)"
            " FROM experiment_runs WHERE run_id LIKE 'run-%'",
        )
        run_id = f"run-{(cursor.fetchone()[0] or 0) + 1:06d}"
        body = deepcopy(body)
        body["run_id"] = run_id
        digest = canonical_digest(
            {k: v for k, v in body.items() if k != "digest"})
        body["digest"] = digest
        try:
            connection.execute(
                "INSERT INTO experiment_runs VALUES"
                " (:id, :project, :resolved, :rep, :att, :body, :digest,"
                " :at)",
                {"id": run_id, "project": project_id,
                 "resolved": resolved_id, "rep": repetition,
                 "att": attempt, "body": _dump(body), "digest": digest,
                 "at": created_at})
        except sqlite3.IntegrityError as error:
            raise ValueError(
                f"attempt {attempt} of repetition {repetition} is already"
                " recorded; retries use a new attempt number") from error
        return run_id

    @staticmethod
    def _insert_ledger(connection, *, project_id: str, resolved_id: str,
                       run_id: str, resource: str, amount: int,
                       created_at: str) -> None:
        connection.execute(
            "INSERT INTO budget_ledger (project_id, resolved_id, run_id,"
            " resource, amount, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (project_id, resolved_id, run_id, resource, amount,
             created_at))

    @staticmethod
    def _checked_run_body(body: dict) -> None:
        text = _dump(body)
        if len(text.encode()) > MAX_EVIDENCE_BYTES:
            raise ValueError("run evidence exceeds the size bound")
        scan_for_secrets(body, "run record")

    def save_run(self, *, project_id: str, resolved_id: str, repetition: int,
                 attempt: int, body: dict, created_at: str) -> dict:
        """Store one run record; each (resolved, repetition, attempt) runs once."""
        check_project_id(project_id)
        self._checked_run_body(body)
        with self._connect() as connection:
            run_id = self._insert_run(
                connection, project_id=project_id, resolved_id=resolved_id,
                repetition=repetition, attempt=attempt, body=body,
                created_at=created_at)
        return self.get_run(project_id, run_id)

    def save_run_with_ledger(self, *, project_id: str, resolved_id: str,
                             repetition: int, attempt: int, body: dict,
                             tool_calls: int, wall_clock_ms: int,
                             created_at: str) -> dict:
        """Store one run plus its budget charges in a single transaction.

        A crash can lose the whole attempt or keep it whole, never
        record a run without its charge. Prefer this over save_run plus
        separate add_ledger calls for live execution.
        """
        check_project_id(project_id)
        if type(tool_calls) is not int or tool_calls < 0:
            raise ValueError("invalid ledger amount")
        if type(wall_clock_ms) is not int or wall_clock_ms < 0:
            raise ValueError("invalid ledger amount")
        self._checked_run_body(body)
        with self._connect() as connection:
            run_id = self._insert_run(
                connection, project_id=project_id, resolved_id=resolved_id,
                repetition=repetition, attempt=attempt, body=body,
                created_at=created_at)
            for resource, amount in (("tool_calls", tool_calls),
                                     ("wall_clock_ms", wall_clock_ms)):
                self._insert_ledger(
                    connection, project_id=project_id,
                    resolved_id=resolved_id, run_id=run_id,
                    resource=resource, amount=amount, created_at=created_at)
        return self.get_run(project_id, run_id)
    def get_run(self, project_id: str, run_id: str) -> dict:
        check_project_id(project_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM experiment_runs WHERE run_id = ?",
                (run_id,)).fetchone()
        if row is None:
            raise LookupError(f"unknown run '{run_id}'")
        if row["project_id"] != project_id:
            raise CrossProjectError(
                f"run '{run_id}' belongs to a different project")
        body = json.loads(row["run_json"])
        if canonical_digest({k: v for k, v in body.items()
                             if k != "digest"}) != row["digest"]:
            raise ValueError("run digest mismatch")
        return {"run_id": row["run_id"], "project_id": row["project_id"],
                "resolved_id": row["resolved_id"],
                "repetition": row["repetition"], "attempt": row["attempt"],
                "run": body, "digest": row["digest"],
                "created_at": row["created_at"]}

    def list_runs(self, project_id: str, resolved_id: str | None = None) -> list:
        check_project_id(project_id)
        with self._connect() as connection:
            if resolved_id is None:
                rows = connection.execute(
                    "SELECT * FROM experiment_runs WHERE project_id = ?"
                    " ORDER BY created_at, run_id", (project_id,)).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM experiment_runs WHERE project_id = ?"
                    " AND resolved_id = ? ORDER BY created_at, run_id",
                    (project_id, resolved_id)).fetchall()
        records = []
        for row in rows:
            body = json.loads(row["run_json"])
            if canonical_digest({k: v for k, v in body.items()
                                 if k != "digest"}) != row["digest"]:
                raise ValueError("run digest mismatch")
            records.append({"run_id": row["run_id"],
                            "project_id": row["project_id"],
                            "resolved_id": row["resolved_id"],
                            "repetition": row["repetition"],
                            "attempt": row["attempt"], "run": body,
                            "digest": row["digest"],
                            "created_at": row["created_at"]})
        return records

    def add_ledger(self, *, project_id: str, resolved_id: str, run_id: str,
                   resource: str, amount: int, created_at: str) -> None:
        """Append one budget consumption entry; the ledger only grows."""
        check_project_id(project_id)
        if type(resource) is not str or not resource:
            raise ValueError("invalid ledger resource")
        if type(amount) is not int or amount < 0:
            raise ValueError("invalid ledger amount")
        with self._connect() as connection:
            self._insert_ledger(
                connection, project_id=project_id,
                resolved_id=resolved_id, run_id=run_id,
                resource=resource, amount=amount, created_at=created_at)

    def budget_used(self, project_id: str, resolved_id: str,
                    resource: str = "tool_calls") -> int:
        """Total consumed units for one resolved config; survives recovery."""
        check_project_id(project_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM budget_ledger"
                " WHERE project_id = ? AND resolved_id = ? AND resource = ?",
                (project_id, resolved_id, resource)).fetchone()
        return row[0]
