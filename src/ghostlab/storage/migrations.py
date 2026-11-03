"""Forward-only, checksummed SQL migrations from the repository ``migrations/`` folder.

Files are named ``NNNN_description.sql``. Each runs in one transaction together
with its ``schema_migrations`` row. An applied file whose content later changes,
or a database newer than the code, fails as ``IntegrityError``. Downgrades are
not supported; add a new migration instead.
"""

import hashlib
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from .interfaces import IntegrityError
from .sqlite import _connect

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations"
_NAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


def available(directory: Path = MIGRATIONS_DIR) -> list[tuple[int, str, str, str]]:
    """(version, name, sha256, sql) for each migration file, in version order."""
    found = []
    for path in sorted(directory.glob("*.sql")):
        match = _NAME.match(path.name)
        if not match:
            raise IntegrityError(f"Unexpected migration file name: {path.name}")
        sql = path.read_text(encoding="utf-8").replace("\r\n", "\n")
        found.append((int(match.group(1)), match.group(2),
                      hashlib.sha256(sql.encode()).hexdigest(), sql))
    versions = [item[0] for item in found]
    if versions != list(range(1, len(versions) + 1)):
        raise IntegrityError("Migration versions must be contiguous from 0001")
    return found


def current_version(path: Path) -> int:
    with closing(_connect(path)) as conn:
        return _applied(conn)[0]


def _applied(conn: sqlite3.Connection) -> tuple[int, dict[int, str]]:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations ("
                 "version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
                 "sha256 TEXT NOT NULL, applied_at TEXT NOT NULL)")
    rows = dict(conn.execute("SELECT version, sha256 FROM schema_migrations").fetchall())
    return (max(rows) if rows else 0), rows


def migrate(path: Path, directory: Path = MIGRATIONS_DIR, *, target: int | None = None) -> int:
    """Apply pending migrations up to ``target`` (default: all). Returns the version."""
    migrations = available(directory)
    target = len(migrations) if target is None else target
    with closing(_connect(path)) as conn:
        version, applied = _applied(conn)
        if version > len(migrations):
            raise IntegrityError("Database schema is newer than this code")
        for number, _, digest, _ in migrations[:version]:
            if applied.get(number) != digest:
                raise IntegrityError(f"Applied migration {number:04d} was modified")
        for number, name, digest, sql in migrations[version:target]:
            stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            # executescript cannot take parameters; every value here is validated
            # (int version, [a-z0-9_] name, hex digest, fixed-format timestamp).
            script = (f"BEGIN IMMEDIATE;\n{sql}\n;INSERT INTO schema_migrations "
                      f"VALUES ({number}, '{name}', '{digest}', '{stamp}');\nCOMMIT;")
            try:
                conn.executescript(script)
            except sqlite3.Error:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                if _applied(conn)[1].get(number) != digest:  # another process got here first
                    raise
            version = number
    return version
