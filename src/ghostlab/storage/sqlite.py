"""SQLite connection and transaction discipline shared by M04 stores.

Single local coordinator only. Each operation opens its own connection and
closes it, so stores are safe to share across threads. Writes run inside
``BEGIN IMMEDIATE`` so the write lock is taken before any read used for a
compare-and-set decision; concurrent writers wait up to ``BUSY_TIMEOUT_S`` and
then fail with ``StorageError`` rather than lose an update.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

from pydantic import BaseModel, ValidationError

from ghostlab.contracts.primitives import parse_timestamp

from .interfaces import IntegrityError, StorageError

BUSY_TIMEOUT_S = 10.0
_EPOCH = parse_timestamp("1970-01-01T00:00:00Z")


def _connect(path: Path) -> sqlite3.Connection:
    # isolation_level=None: no implicit transactions; this module issues BEGIN/COMMIT.
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_S, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_S * 1000)}")
    return conn


def enable_wal(path: Path) -> None:
    with closing(_connect(path)) as conn:
        mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
    if mode.lower() != "wal":
        raise StorageError("SQLite WAL mode is unavailable for this path")


@contextmanager
def read(path: Path) -> Iterator[sqlite3.Connection]:
    with closing(_connect(path)) as conn:
        yield conn


@contextmanager
def write(path: Path) -> Iterator[sqlite3.Connection]:
    """One atomic write transaction; rolled back on any exception."""
    with closing(_connect(path)) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as error:
            raise StorageError("Database is busy") from error
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")


def timestamp_us(value: str) -> int:
    """Sortable integer microseconds for an RFC 3339 UTC contract timestamp."""
    delta = parse_timestamp(value) - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def load(model: type[BaseModel], body: str):
    """Re-validate stored JSON; tampered or stale rows fail as IntegrityError."""
    try:
        return model.model_validate_json(body)
    except ValidationError as error:
        raise IntegrityError(f"Stored {model.__name__} failed validation") from error


def revise(model: type[BaseModel], current: BaseModel, **changes):
    """Apply field changes by re-validating a dump, never via model_copy(update=...)."""
    data = current.model_dump(mode="json")
    data.update(changes)
    return model.model_validate(data)
