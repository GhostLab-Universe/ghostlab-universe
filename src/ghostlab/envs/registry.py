"""Explicit registration of reviewed local environments (SQLite, serial local).

Each developer keeps a local database file; live database files are never
committed or shared. Tests pass a temporary path, never the default.
"""

import json
import re
import sqlite3
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from . import STATUS_NOT_QUALIFIED, STATUSES
from .adapters import adapter_code_digest, resolve_adapter_ref

ENV_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
ENV_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+*-]{0,63}$")
TOOL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
PARAM_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
MODE_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,63}$")

DEFAULT_ALLOWED_PREFIXES = ("ghostlab.",)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS env_registrations (
  env_id TEXT PRIMARY KEY,
  env_version TEXT NOT NULL,
  adapter_ref TEXT NOT NULL,
  display_name TEXT NOT NULL,
  declared_tools TEXT NOT NULL,
  declared_modes TEXT NOT NULL,
  code_digest TEXT NOT NULL,
  registered_by TEXT NOT NULL,
  registered_at TEXT NOT NULL,
  status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS env_manifests (
  manifest_digest TEXT PRIMARY KEY,
  env_id TEXT NOT NULL,
  manifest_json TEXT NOT NULL,
  imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS env_reports (
  report_id INTEGER PRIMARY KEY AUTOINCREMENT,
  env_id TEXT NOT NULL,
  env_version TEXT NOT NULL,
  adapter_ref TEXT NOT NULL,
  code_digest TEXT NOT NULL,
  manifest_digest TEXT,
  runner_version TEXT NOT NULL,
  status TEXT NOT NULL,
  checks_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _check_tools(declared_tools) -> list:
    if type(declared_tools) is not list or not 1 <= len(declared_tools) <= 32:
        raise ValueError("declared_tools must list 1-32 tools")
    seen = set()
    for entry in declared_tools:
        if type(entry) is not dict or set(entry) != {"name", "parameters"}:
            raise ValueError("each declared tool needs exactly name and parameters")
        name, parameters = entry["name"], entry["parameters"]
        if type(name) is not str or not TOOL_NAME_PATTERN.match(name) or name in seen:
            raise ValueError(f"invalid or duplicate tool name '{name}'")
        seen.add(name)
        if (type(parameters) is not list or len(parameters) > 16
                or any(type(p) is not str or not PARAM_PATTERN.match(p)
                       for p in parameters)
                or len(set(parameters)) != len(parameters)):
            raise ValueError(f"invalid parameters for tool '{name}'")
    return deepcopy(declared_tools)


def _check_modes(declared_modes) -> list:
    if type(declared_modes) is not list or not 1 <= len(declared_modes) <= 8:
        raise ValueError("declared_modes must list 1-8 reproduction modes")
    if (any(type(m) is not str or not MODE_PATTERN.match(m) for m in declared_modes)
            or len(set(declared_modes)) != len(declared_modes)):
        raise ValueError("invalid or duplicate reproduction mode")
    return list(declared_modes)


class EnvRegistry:
    """Explicit registry; registering resolves code identity, never runs it."""

    def __init__(self, db_path: str | Path,
                 allowed_adapter_prefixes: tuple[str, ...] = DEFAULT_ALLOWED_PREFIXES):
        self.db_path = Path(db_path)
        self.allowed_adapter_prefixes = tuple(allowed_adapter_prefixes)
        if not self.allowed_adapter_prefixes:
            raise ValueError("allowed_adapter_prefixes must not be empty")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    @contextmanager
    def _connect(self):
        # A raw sqlite3 connection used as a context manager commits but never
        # closes; unclosed handles lock the file on Windows. Always close.
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def register(self, *, env_id: str, env_version: str, adapter_ref: str,
                 display_name: str, declared_tools: list, declared_modes: list,
                 registered_by: str = "local-operator") -> dict:
        if type(env_id) is not str or not ENV_ID_PATTERN.match(env_id):
            raise ValueError("invalid env_id")
        if type(env_version) is not str or not ENV_VERSION_PATTERN.match(env_version):
            raise ValueError("invalid env_version")
        if (type(display_name) is not str or not display_name
                or len(display_name) > 200):
            raise ValueError("invalid display_name")
        if (type(registered_by) is not str or not registered_by
                or len(registered_by) > 200):
            raise ValueError("invalid registered_by")
        tools = _check_tools(declared_tools)
        modes = _check_modes(declared_modes)
        adapter_cls = resolve_adapter_ref(adapter_ref, self.allowed_adapter_prefixes)
        record = {
            "env_id": env_id, "env_version": env_version, "adapter_ref": adapter_ref,
            "display_name": display_name, "declared_tools": tools,
            "declared_modes": modes,
            "code_digest": adapter_code_digest(adapter_cls),
            "registered_by": registered_by, "registered_at": utcnow(),
            "status": STATUS_NOT_QUALIFIED,
        }
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO env_registrations VALUES"
                    " (:env_id, :env_version, :adapter_ref, :display_name,"
                    " :tools, :modes, :code_digest, :registered_by,"
                    " :registered_at, :status)",
                    {"env_id": env_id, "env_version": env_version,
                     "adapter_ref": adapter_ref, "display_name": display_name,
                     "tools": json.dumps(tools), "modes": json.dumps(modes),
                     "code_digest": record["code_digest"],
                     "registered_by": registered_by,
                     "registered_at": record["registered_at"],
                     "status": STATUS_NOT_QUALIFIED})
        except sqlite3.IntegrityError as error:
            raise ValueError(f"env_id '{env_id}' is already registered") from error
        return record

    def get(self, env_id: str) -> dict:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM env_registrations WHERE env_id = ?", (env_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown env_id '{env_id}'")
        return self._record(row)

    def list_envs(self) -> list:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM env_registrations ORDER BY env_id").fetchall()
        return [self._record(row) for row in rows]

    def set_status(self, env_id: str, status: str) -> dict:
        if status not in STATUSES or status == STATUS_NOT_QUALIFIED:
            raise ValueError("status must be qualified-for-declared or failed")
        self.get(env_id)
        with self._connect() as connection:
            connection.execute("UPDATE env_registrations SET status = ? WHERE env_id = ?",
                               (status, env_id))
        return self.get(env_id)

    @staticmethod
    def _record(row) -> dict:
        return {
            "env_id": row["env_id"], "env_version": row["env_version"],
            "adapter_ref": row["adapter_ref"], "display_name": row["display_name"],
            "declared_tools": json.loads(row["declared_tools"]),
            "declared_modes": json.loads(row["declared_modes"]),
            "code_digest": row["code_digest"], "registered_by": row["registered_by"],
            "registered_at": row["registered_at"], "status": row["status"],
        }

    def store_manifest(self, env_id: str, manifest_digest: str, manifest_json: str) -> None:
        self.get(env_id)
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO env_manifests VALUES (?, ?, ?, ?)",
                (manifest_digest, env_id, manifest_json, utcnow()))

    def get_manifest(self, manifest_digest: str) -> dict:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM env_manifests WHERE manifest_digest = ?",
                (manifest_digest,)).fetchone()
        if row is None:
            raise ValueError(f"unknown manifest '{manifest_digest}'")
        return {"manifest_digest": row["manifest_digest"], "env_id": row["env_id"],
                "manifest": json.loads(row["manifest_json"]),
                "imported_at": row["imported_at"]}

    def list_manifests(self, env_id: str) -> list:
        self.get(env_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT manifest_digest, imported_at FROM env_manifests"
                " WHERE env_id = ? ORDER BY imported_at",
                (env_id,)).fetchall()
        return [{"manifest_digest": row["manifest_digest"],
                 "imported_at": row["imported_at"]} for row in rows]

    def store_report(self, report: dict) -> dict:
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO env_reports (env_id, env_version, adapter_ref, code_digest,"
                " manifest_digest, runner_version, status, checks_json, created_at)"
                " VALUES (:env_id, :env_version, :adapter_ref, :code_digest,"
                " :manifest_digest, :runner_version, :status, :checks, :created_at)",
                {"env_id": report["env_id"], "env_version": report["env_version"],
                 "adapter_ref": report["adapter_ref"],
                 "code_digest": report["code_digest"],
                 "manifest_digest": report["manifest_digest"],
                 "runner_version": report["runner_version"],
                 "status": report["status"],
                 "checks": json.dumps(report["checks"]),
                 "created_at": report["created_at"]})
            report_id = cursor.lastrowid
        stored = {"report_id": report_id, **report}
        return stored

    def latest_report(self, env_id: str) -> dict | None:
        self.get(env_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM env_reports WHERE env_id = ?"
                " ORDER BY report_id DESC LIMIT 1", (env_id,)).fetchone()
        return None if row is None else self._report(row)

    def list_reports(self, env_id: str) -> list:
        self.get(env_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM env_reports WHERE env_id = ? ORDER BY report_id",
                (env_id,)).fetchall()
        return [self._report(row) for row in rows]

    @staticmethod
    def _report(row) -> dict:
        return {
            "report_id": row["report_id"], "schema_version": 1,
            "env_id": row["env_id"], "env_version": row["env_version"],
            "adapter_ref": row["adapter_ref"], "code_digest": row["code_digest"],
            "manifest_digest": row["manifest_digest"],
            "runner_version": row["runner_version"], "status": row["status"],
            "checks": json.loads(row["checks_json"]),
            "created_at": row["created_at"],
        }
