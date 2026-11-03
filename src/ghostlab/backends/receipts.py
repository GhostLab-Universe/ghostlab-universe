"""Project-scoped job receipts for isolated execution backends.

A receipt is the controller's tamper-evident record of one backend job:
the pinned package, the observed state history, per-message digests and
the observed-vs-claimed separation for everything the backend reported.
Receipts live in their own SQLite file next to the experiment store, so
existing run/config formats are untouched.

Wire shape (frozen ``execution-backend/1`` surface):

- Controller to backend: unframed commands ``{"type", "payload"}`` via
  ``send``; the backend assigns command sequence numbers.
- Backend to controller: full envelopes
  ``{v: 1, job_id, seq, type, payload}`` via ``poll``.

Honesty rules:

- The controller assigns every ``job_id``; the backend never mints
  identity. The registry is keyed ``(project_id, job_id)`` and any
  cross-project job operation is refused with :class:`CrossProjectError`.
- Backend output is untrusted data. Event envelopes are validated
  strictly through the frozen protocol codec (exact keys, ``v == 1``,
  job match, known type, gap-free sequence, 64 KiB cap, depth cap,
  strict JSON types, never pickle/eval); persisted text is scrubbed of
  secret-like markers while digests stay over the raw bytes so
  tampering stays detectable.
- ``observed`` holds controller-observed facts (state transitions the
  controller drove or witnessed, termination, truncation decisions).
  ``claimed`` holds backend-reported facts (status payloads, close
  summaries). The two are never merged.
- :meth:`JobReceiptStore.reconcile` only marks orphaned in-flight jobs
  after a controller restart. It never retries side-effecting work and
  only removes workspace directories that carry a matching job marker.

This module is transport- and backend-agnostic: it records whatever the
controller observed. It makes no model calls and holds no secrets.

The error, state and codec surface is consumed from the frozen
``ghostlab.backends.interface`` and ``ghostlab.backends.protocol``
modules; small fallbacks apply only when the interface module is absent
(a sibling track owns it). ``CrossProjectError`` is reused from the
experiment store so project scoping stays one class.
"""

import contextlib
import hashlib
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ..experiments.store import CrossProjectError
from . import protocol as _protocol

try:  # Frozen B1 surface; fallbacks apply only when it is absent.
    from .interface import (BACKEND_VERSION, STATES, TERMINAL_STATES,
                            BackendError, BackendUnavailableError,
                            CrossJobError, JobSpec, ProtocolError)
except ImportError:  # pragma: no cover - interface owns these names.
    BACKEND_VERSION = "execution-backend/1"
    STATES = ("prepared", "starting", "running", "completed", "failed",
              "cancelled", "timed_out")
    TERMINAL_STATES = frozenset(
        {"completed", "failed", "cancelled", "timed_out"})

    class BackendError(Exception):
        """A backend refused or failed an operation (base class)."""

    class BackendUnavailableError(BackendError):
        """A known backend is unavailable; the caller must not fall back."""

        def __init__(self, message="", *, backend=None, reason=None):
            super().__init__(message or
                             f"backend {backend!r} unavailable: {reason}")
            self.backend = backend
            self.reason = reason

    class ProtocolError(BackendError):
        """A wire message or backend return violates the frozen shape."""

    class CrossJobError(BackendError):
        """A message carried the wrong job identity (forgery/mixup)."""

    from dataclasses import dataclass as _dataclass

    @_dataclass(frozen=True)
    class JobSpec:
        """Controller-assigned job description (frozen shape)."""

        job_id: str
        project_id: str
        package: dict
        adapter_ref: str
        fixture: dict
        limits: dict
        no_network: bool = True

__all__ = [
    "BackendError",
    "BackendUnavailableError",
    "CrossJobError",
    "CrossProjectError",
    "JobHandle",
    "JobReceiptStore",
    "JobSpec",
    "ProtocolError",
    "COMMAND_TYPES",
    "EVENT_TYPES",
    "EXECUTION_BACKEND_VERSION",
    "JOB_STATES",
    "MAX_ENVELOPE_BYTES",
    "MAX_ENVELOPE_DEPTH",
    "MAX_JOB_OUTPUT_BYTES",
    "RECEIPT_SCHEMA_VERSION",
    "TERMINAL_JOB_STATES",
    "canonical_digest",
    "sanitize_untrusted_text",
    "validate_command",
    "validate_envelope",
]

RECEIPT_SCHEMA_VERSION = 1
EXECUTION_BACKEND_VERSION = BACKEND_VERSION

JOB_STATES = STATES
TERMINAL_JOB_STATES = TERMINAL_STATES
COMMAND_TYPES = _protocol.WORKER_COMMANDS
EVENT_TYPES = _protocol.WORKER_EVENTS
MAX_ENVELOPE_BYTES = _protocol.MAX_MESSAGE_BYTES
MAX_ENVELOPE_DEPTH = _protocol.MAX_DEPTH
MAX_JOB_OUTPUT_BYTES = 1_000_000
MAX_CLAIMED_BYTES = 262_144
WORKSPACE_MARKER = ".ghostlab-job.json"

# Mirrors configs._SECRET_MARKERS. Backend output is untrusted, so markers
# are redacted before persistence (digests stay over the raw bytes);
# otherwise a hostile marker could either plant exfiltrate-looking text
# into evidence or break run persistence entirely.
_SCRUB_MARKERS = ("API_KEY", "APIKEY", "PRIVATE KEY", "client_secret",
                  "BEARER ", "xox-")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS backend_jobs (
  project_id TEXT NOT NULL,
  job_id TEXT NOT NULL,
  backend TEXT NOT NULL,
  backend_version TEXT NOT NULL,
  state TEXT NOT NULL,
  package_json TEXT NOT NULL,
  workspace TEXT,
  observed_json TEXT NOT NULL DEFAULT '{}',
  claimed_json TEXT NOT NULL DEFAULT '{}',
  resolution TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (project_id, job_id)
);
CREATE TABLE IF NOT EXISTS job_state_history (
  entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL,
  job_id TEXT NOT NULL,
  state TEXT NOT NULL,
  observed INTEGER NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_job_history_job
  ON job_state_history (project_id, job_id, entry_id);
CREATE TABLE IF NOT EXISTS job_messages (
  entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL,
  job_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  type TEXT NOT NULL,
  digest TEXT NOT NULL,
  claimed INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_job_messages_job
  ON job_messages (project_id, job_id, entry_id);
"""


def _utcnow() -> str:
    """UTC timestamp in the registry's ``...Z`` format (stdlib only)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_digest(obj) -> str:
    """Sha256 over canonical JSON (same construction as configs)."""
    text = json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _check_jsonable(node) -> None:
    """Reject non-JSON types, non-str keys and non-finite floats.

    Depth and size belong to the frozen protocol codec; this only adds
    the key-type strictness a dump/load round-trip would silently blur.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if type(key) is not str:
                raise ProtocolError(
                    "message object keys must be strings")
            _check_jsonable(value)
    elif isinstance(node, list):
        for value in node:
            _check_jsonable(value)
    elif isinstance(node, str):
        return
    elif isinstance(node, bool) or node is None:
        return
    elif isinstance(node, int):
        return
    elif isinstance(node, float):
        if node != node or node in (float("inf"), float("-inf")):
            raise ProtocolError("message floats must be finite")
        return
    else:
        raise ProtocolError(
            f"message carries non-JSON type {type(node).__name__}")


def validate_command(command) -> dict:
    """Validate one controller-side command; return it normalized.

    Commands are unframed ``{"type", "payload"}`` (the backend assigns
    sequence numbers). Unknown fields and unknown command types are
    rejected; a missing payload defaults to ``{}``.
    """
    if type(command) is not dict or "type" not in command \
            or set(command) - {"type", "payload"}:
        raise ProtocolError(
            "command must carry exactly type and optional payload")
    if command["type"] not in COMMAND_TYPES:
        raise ProtocolError(f"unknown command type {command['type']!r}")
    payload = command.get("payload", {})
    if type(payload) is not dict:
        raise ProtocolError("command payload must be an object")
    _check_jsonable(payload)
    return {"type": command["type"], "payload": payload}


def validate_envelope(envelope, *, job_id: str, direction: str,
                      expected_seq=None) -> dict:
    """Validate one wire envelope through the frozen codec.

    ``direction`` is ``"command"`` or ``"event"`` and selects the known
    type set. The envelope must carry exactly
    ``{v, job_id, seq, type, payload}`` with ``v == 1``; size, depth and
    strict-JSON checks run through ``protocol.encode_message`` and
    identity/ordering through ``protocol.decode_message``. A job
    mismatch raises :class:`CrossJobError`; every other violation
    raises :class:`ProtocolError`.
    """
    if direction == "command":
        known = COMMAND_TYPES
    elif direction == "event":
        known = EVENT_TYPES
    else:
        raise ValueError("direction must be 'command' or 'event'")
    if type(envelope) is not dict \
            or set(envelope) != {"v", "job_id", "seq", "type", "payload"}:
        raise ProtocolError(
            "envelope must carry exactly v, job_id, seq, type, payload")
    if type(envelope["v"]) is not int or envelope["v"] != 1:
        raise ProtocolError("envelope v must be 1")
    if type(envelope["job_id"]) is not str or not envelope["job_id"]:
        raise ProtocolError("envelope job_id must be a non-empty string")
    if envelope["job_id"] != job_id:
        raise CrossJobError(
            f"envelope job {envelope['job_id']!r} does not match"
            f" job {job_id!r}")
    if type(envelope["seq"]) is not int or envelope["seq"] < 0:
        raise ProtocolError("envelope seq must be an int >= 0")
    if envelope["type"] not in known:
        raise ProtocolError(
            f"unknown {direction} type {envelope['type']!r}")
    if type(envelope["payload"]) is not dict:
        raise ProtocolError("envelope payload must be an object")
    _check_jsonable(envelope)
    raw = _protocol.encode_message(envelope["job_id"], envelope["seq"],
                                   envelope["type"], envelope["payload"])
    try:
        decoded = _protocol.decode_message(
            raw, expected_job=job_id, expected_seq=expected_seq)
    except ProtocolError as error:
        if "job_id mismatch" in str(error):
            raise CrossJobError(str(error)) from error
        raise
    return decoded


def sanitize_untrusted_text(value, limit: int = 500) -> str:
    """Scrub one backend-supplied string for persistence.

    Non-strings become an explicit marker, secret-like markers are
    redacted (digests over the raw bytes stay in the receipt, so the
    scrubbing is detectable, not silent), NULs are dropped and the
    result is truncated to ``limit`` chars.
    """
    if type(value) is not str:
        return f"<invalid:{type(value).__name__}>"
    text = value.replace("\x00", "")
    for marker in _SCRUB_MARKERS:
        if marker in text:
            text = text.replace(marker, "[filtered]")
    if len(text) > limit:
        text = text[:limit]
    return text


def _check_id(value, what: str) -> str:
    if type(value) is not str or not value or len(value) > 128:
        raise ValueError(f"invalid {what}")
    for char in value:
        if not (char.isalnum() or char in "._:-"):
            raise ValueError(f"invalid {what}")
    if not value[0].isalnum():
        raise ValueError(f"invalid {what}")
    return value


def _dump(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)


class JobReceiptStore:
    """Project-scoped job receipts in a dedicated SQLite file."""

    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    @contextlib.contextmanager
    def _connect(self):
        """Yield a connection that is committed and closed (Windows-safe)."""
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def create_job(self, *, project_id: str, job_id: str, backend: str,
                   backend_version: str, package: dict,
                   workspace=None, created_at=None) -> dict:
        """Open a receipt for one controller-assigned job (state prepared)."""
        _check_id(project_id, "project_id")
        _check_id(job_id, "job_id")
        if type(backend) is not str or not backend or len(backend) > 64:
            raise ValueError("invalid backend")
        if type(backend_version) is not str or not backend_version \
                or len(backend_version) > 100:
            raise ValueError("invalid backend_version")
        if type(package) is not dict or set(package) != {"kind", "identity"}:
            raise ValueError("package must carry exactly kind and identity")
        if type(package["kind"]) is not str or not package["kind"]:
            raise ValueError("invalid package kind")
        if type(package["identity"]) is not str or not package["identity"]:
            raise ValueError("invalid package identity")
        if workspace is not None and (type(workspace) is not str
                                      or not workspace
                                      or len(workspace) > 512):
            raise ValueError("invalid workspace")
        at = created_at or _utcnow()
        with self._connect() as connection:
            try:
                connection.execute(
                    "INSERT INTO backend_jobs (project_id, job_id, backend,"
                    " backend_version, state, package_json, workspace,"
                    " observed_json, claimed_json, resolution, created_at,"
                    " updated_at) VALUES (?, ?, ?, ?, 'prepared', ?, ?,"
                    " '{}', '{}', NULL, ?, ?)",
                    (project_id, job_id, backend, backend_version,
                     _dump(package), workspace, at, at))
                connection.execute(
                    "INSERT INTO job_state_history (project_id, job_id,"
                    " state, observed, note, created_at)"
                    " VALUES (?, ?, 'prepared', 1, 'job created', ?)",
                    (project_id, job_id, at))
            except sqlite3.IntegrityError as error:
                raise ValueError(
                    f"job {job_id!r} is already recorded") from error
        return self.get_receipt(project_id, job_id)

    def require_job(self, project_id: str, job_id: str) -> dict:
        """Return a job row or refuse (unknown vs cross-project)."""
        _check_id(project_id, "project_id")
        _check_id(job_id, "job_id")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM backend_jobs WHERE job_id = ?",
                (job_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown job {job_id!r}")
        if row["project_id"] != project_id:
            raise CrossProjectError(
                f"job {job_id!r} belongs to a different project")
        return dict(row)

    def record_state(self, project_id: str, job_id: str, state: str, *,
                     observed: bool = True, note: str = "",
                     created_at=None) -> dict:
        """Append one state transition; terminal states are final."""
        if state not in JOB_STATES:
            raise ValueError(f"unknown job state {state!r}")
        if type(note) is not str or len(note) > 500:
            raise ValueError("invalid state note")
        current = self.require_job(project_id, job_id)
        if current["state"] in TERMINAL_JOB_STATES and state != current[
                "state"]:
            raise ValueError(
                f"job {job_id!r} is already terminal"
                f" ({current['state']!r}); refusing to reopen")
        at = created_at or _utcnow()
        with self._connect() as connection:
            connection.execute(
                "UPDATE backend_jobs SET state = ?, updated_at = ?"
                " WHERE project_id = ? AND job_id = ?",
                (state, at, project_id, job_id))
            connection.execute(
                "INSERT INTO job_state_history (project_id, job_id, state,"
                " observed, note, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (project_id, job_id, state, 1 if observed else 0,
                 note, at))
        return self.get_receipt(project_id, job_id)

    def record_event(self, project_id: str, job_id: str, envelope: dict,
                     *, expected_seq=None, created_at=None) -> str:
        """Validate one backend event and record its digest."""
        self.require_job(project_id, job_id)
        validate_envelope(envelope, job_id=job_id, direction="event",
                          expected_seq=expected_seq)
        digest = canonical_digest(envelope)
        at = created_at or _utcnow()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO job_messages (project_id, job_id, seq, type,"
                " digest, claimed, created_at)"
                " VALUES (?, ?, ?, ?, ?, 1, ?)",
                (project_id, job_id, envelope["seq"], envelope["type"],
                 digest, at))
            connection.execute(
                "UPDATE backend_jobs SET updated_at = ?"
                " WHERE project_id = ? AND job_id = ?",
                (at, project_id, job_id))
        return digest

    def record_command(self, project_id: str, job_id: str, command: dict,
                       seq: int, created_at=None) -> str:
        """Validate one controller command and record its digest.

        ``seq`` is the controller-side command counter (the backend
        assigns wire sequence numbers itself).
        """
        self.require_job(project_id, job_id)
        normalized = validate_command(command)
        if type(seq) is not int or seq < 0:
            raise ValueError("command seq must be an int >= 0")
        digest = canonical_digest(normalized)
        at = created_at or _utcnow()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO job_messages (project_id, job_id, seq, type,"
                " digest, claimed, created_at)"
                " VALUES (?, ?, ?, ?, ?, 0, ?)",
                (project_id, job_id, seq, normalized["type"], digest, at))
            connection.execute(
                "UPDATE backend_jobs SET updated_at = ?"
                " WHERE project_id = ? AND job_id = ?",
                (at, project_id, job_id))
        return digest

    def _merge_facts(self, project_id: str, job_id: str, column: str,
                     facts: dict) -> dict:
        if type(facts) is not dict:
            raise ValueError("facts must be an object")
        _check_jsonable(facts)
        try:
            text = _dump(facts)
        except ValueError as error:
            raise ValueError(f"facts are not strict JSON: {error}") from error
        if len(text.encode("utf-8")) > MAX_CLAIMED_BYTES:
            raise ProtocolError("facts exceed the size bound")
        row = self.require_job(project_id, job_id)
        merged = json.loads(row[column])
        merged.update(json.loads(text))
        at = _utcnow()
        with self._connect() as connection:
            connection.execute(
                f"UPDATE backend_jobs SET {column} = ?, updated_at = ?"
                " WHERE project_id = ? AND job_id = ?",
                (_dump(merged), at, project_id, job_id))
        return self.get_receipt(project_id, job_id)

    def update_observed(self, project_id: str, job_id: str,
                        facts: dict) -> dict:
        """Merge controller-observed facts (never backend claims)."""
        return self._merge_facts(project_id, job_id, "observed_json", facts)

    def update_claimed(self, project_id: str, job_id: str,
                       facts: dict) -> dict:
        """Merge backend-claimed facts (never presented as observed)."""
        return self._merge_facts(project_id, job_id, "claimed_json", facts)

    def record_workspace(self, project_id: str, job_id: str,
                         workspace: str) -> dict:
        """Record the backend-claimed workspace path for later sweeping."""
        if type(workspace) is not str or not workspace \
                or len(workspace) > 512:
            raise ValueError("invalid workspace")
        self.require_job(project_id, job_id)
        at = _utcnow()
        with self._connect() as connection:
            connection.execute(
                "UPDATE backend_jobs SET workspace = ?, updated_at = ?"
                " WHERE project_id = ? AND job_id = ?",
                (workspace, at, project_id, job_id))
        return self.get_receipt(project_id, job_id)

    def get_receipt(self, project_id: str, job_id: str) -> dict:
        """Return the full receipt: state, observed/claimed, history, digests."""
        row = self.require_job(project_id, job_id)
        with self._connect() as connection:
            history = connection.execute(
                "SELECT state, observed, note, created_at"
                " FROM job_state_history WHERE project_id = ? AND job_id = ?"
                " ORDER BY entry_id",
                (project_id, job_id)).fetchall()
            messages = connection.execute(
                "SELECT seq, type, digest, claimed, created_at"
                " FROM job_messages WHERE project_id = ? AND job_id = ?"
                " ORDER BY entry_id",
                (project_id, job_id)).fetchall()
        entries = [{"seq": item["seq"], "type": item["type"],
                    "digest": item["digest"],
                    "claimed": bool(item["claimed"]),
                    "at": item["created_at"]} for item in messages]
        receipt = {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "project_id": row["project_id"], "job_id": row["job_id"],
            "backend": row["backend"],
            "backend_version": row["backend_version"],
            "package": json.loads(row["package_json"]),
            "workspace": row["workspace"], "state": row["state"],
            "resolution": row["resolution"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "observed": json.loads(row["observed_json"]),
            "claimed": json.loads(row["claimed_json"]),
            "history": [{"state": item["state"],
                         "observed": bool(item["observed"]),
                         "note": item["note"], "at": item["created_at"]}
                        for item in history],
            "message_digests": entries,
            "event_digests": [item for item in entries
                              if item["claimed"]],
        }
        receipt["receipt_digest"] = canonical_digest(
            {key: value for key, value in receipt.items()
             if key != "receipt_digest"})
        return receipt

    def list_receipts(self, project_id: str) -> list:
        """List this project's job summaries (never another project's)."""
        _check_id(project_id, "project_id")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT project_id, job_id, backend, backend_version,"
                " state, resolution, created_at, updated_at"
                " FROM backend_jobs WHERE project_id = ?"
                " ORDER BY created_at, job_id", (project_id,)).fetchall()
        return [dict(row) for row in rows]

    def close_job(self, project_id: str, job_id: str, terminal_state: str,
                  *, note: str = "", created_at=None) -> dict:
        """Record the terminal state; only terminal states close a job."""
        if terminal_state not in TERMINAL_JOB_STATES:
            raise ValueError(
                f"cannot close job {job_id!r} in non-terminal state"
                f" {terminal_state!r}")
        return self.record_state(project_id, job_id, terminal_state,
                                 observed=True, note=note or "job closed",
                                 created_at=created_at)

    def mark_unresolved(self, project_id: str, job_id: str,
                        note: str) -> dict:
        """Mark a job unresolved without changing its last-known state."""
        if type(note) is not str or not note or len(note) > 500:
            raise ValueError("invalid resolution note")
        self.require_job(project_id, job_id)
        at = _utcnow()
        with self._connect() as connection:
            connection.execute(
                "UPDATE backend_jobs SET resolution = ?, updated_at = ?"
                " WHERE project_id = ? AND job_id = ?",
                (note, at, project_id, job_id))
            connection.execute(
                "INSERT INTO job_state_history (project_id, job_id, state,"
                " observed, note, created_at)"
                " SELECT project_id, job_id, state, 1, ?, ?"
                " FROM backend_jobs WHERE project_id = ? AND job_id = ?",
                (note, at, project_id, job_id))
        return self.get_receipt(project_id, job_id)

    def reconcile(self, *, now=None) -> dict:
        """Mark orphaned in-flight jobs after a restart; sweep workspaces.

        Every non-terminal job without a resolution is marked
        ``controller_restarted:unresolved`` with a history note. Nothing
        is retried: side-effecting work is never re-driven by a sweep.
        A recorded workspace is removed only when it still exists and
        carries a marker file naming this exact ``(project_id, job_id)``;
        anything else is left untouched.
        """
        at = now or _utcnow()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT project_id, job_id, state, workspace FROM backend_jobs"
                " WHERE state NOT IN ('completed', 'failed', 'cancelled',"
                " 'timed_out') AND resolution IS NULL"
                " ORDER BY created_at, job_id").fetchall()
        orphaned = []
        swept = []
        for row in rows:
            project_id = row["project_id"]
            job_id = row["job_id"]
            note = ("controller_restarted:unresolved;"
                    " the controller restarted before this job reached a"
                    " terminal state; no auto-retry of side-effecting work")
            with self._connect() as connection:
                connection.execute(
                    "UPDATE backend_jobs SET resolution = ?, updated_at = ?"
                    " WHERE project_id = ? AND job_id = ?",
                    (note, at, project_id, job_id))
                connection.execute(
                    "INSERT INTO job_state_history (project_id, job_id,"
                    " state, observed, note, created_at)"
                    " VALUES (?, ?, ?, 1, ?, ?)",
                    (project_id, job_id, row["state"], note, at))
            orphaned.append({"project_id": project_id, "job_id": job_id,
                             "state": row["state"]})
            workspace = row["workspace"]
            if workspace and self._sweep_workspace(project_id, job_id,
                                                   workspace):
                swept.append(workspace)
        return {"orphaned": orphaned, "swept": swept}

    @staticmethod
    def _sweep_workspace(project_id: str, job_id: str,
                         workspace: str) -> bool:
        """Remove a marker-verified orphan workspace; else leave it."""
        try:
            root = Path(workspace)
            marker = root / WORKSPACE_MARKER
            if not root.is_dir() or not marker.is_file():
                return False
            try:
                claimed = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return False
            if type(claimed) is not dict \
                    or claimed.get("project_id") != project_id \
                    or claimed.get("job_id") != job_id:
                return False
            shutil.rmtree(root)
            return True
        except OSError:
            return False


class JobHandle:
    """Project-scoped handle over one backend job.

    Every operation re-checks that ``job_id`` belongs to ``project_id``
    before touching the backend, so a cross-project call is refused with
    :class:`CrossProjectError` even when the backend itself would answer.
    Commands are unframed ``{"type", "payload"}``; the backend assigns
    wire sequence numbers.
    """

    def __init__(self, store: JobReceiptStore, backend,
                 project_id: str, job_id: str):
        self._store = store
        self._backend = backend
        self._project_id = project_id
        self._job_id = job_id

    @property
    def job_id(self) -> str:
        return self._job_id

    @property
    def project_id(self) -> str:
        return self._project_id

    def _checked(self):
        self._store.require_job(self._project_id, self._job_id)
        return self._backend

    def send(self, command: dict) -> None:
        """Send one validated command to the job."""
        normalized = validate_command(command)
        self._checked().send(self._job_id, normalized)

    def poll(self, timeout_ms: int) -> list:
        """Collect backend events, waiting at most ``timeout_ms``."""
        if type(timeout_ms) is not int or timeout_ms < 0:
            raise ValueError("timeout_ms must be an int >= 0")
        return self._checked().poll(self._job_id, timeout_ms)

    def status(self) -> dict:
        """Return the backend-claimed job status (untrusted data)."""
        return self._checked().status(self._job_id)

    def cancel(self, grace_ms: int = 5000) -> dict:
        """Cancel the job with a bounded grace period."""
        if type(grace_ms) is not int or grace_ms < 0:
            raise ValueError("grace_ms must be an int >= 0")
        return self._checked().cancel(self._job_id, grace_ms)

    def close(self) -> dict:
        """Release the job and return the backend's closing receipts."""
        return self._checked().close(self._job_id)
