"""Content-addressed artifact bytes with validated metadata (M04 extension).

Bytes live at ``<root>/sha256/<aa>/<digest>``; metadata is an M00 ArtifactRef
in SQLite. Reads are bounded and verify size and digest, so missing or altered
bytes fail as IntegrityError. Authorization to read an artifact is M06/M07's.
"""

import hashlib
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Protocol, runtime_checkable

from ghostlab.contracts import ArtifactRef, Provenance

from . import sqlite as db
from .interfaces import Conflict, IntegrityError, NotFound, StorageError

ARTIFACT_INTERFACE_VERSION = "ghostlab-storage-artifacts/1"
DEFAULT_MAX_BYTES = 16 * 1024 * 1024


class ArtifactTooLarge(StorageError):
    pass


@runtime_checkable
class ArtifactStore(Protocol):
    def put(self, data: bytes, *, artifact_id: str, media_type: str,
            provenance: Provenance, created_at: str) -> ArtifactRef: ...
    def ref(self, artifact_id: str) -> ArtifactRef: ...
    def read(self, ref: ArtifactRef, *, max_bytes: int = DEFAULT_MAX_BYTES) -> bytes: ...


class FileArtifactStore:
    def __init__(self, db_path: Path, root: Path, *, max_bytes: int = DEFAULT_MAX_BYTES):
        self.db_path, self.root, self.max_bytes = db_path, root, max_bytes

    def _blob(self, digest: str) -> Path:
        return self.root / "sha256" / digest[:2] / digest

    def put(self, data: bytes, *, artifact_id: str, media_type: str,
            provenance: Provenance, created_at: str) -> ArtifactRef:
        if type(data) is not bytes:
            raise TypeError("data must be bytes")
        if len(data) > self.max_bytes:
            raise ArtifactTooLarge("Artifact exceeds the store's size limit")
        digest = hashlib.sha256(data).hexdigest()
        ref = ArtifactRef.model_validate({
            "schema_version": 1, "artifact_id": artifact_id, "sha256": digest,
            "size_bytes": len(data), "media_type": media_type,
            "provenance": provenance.model_dump(mode="json"), "created_at": created_at})
        blob = self._blob(digest)
        if not blob.exists():
            blob.parent.mkdir(parents=True, exist_ok=True)
            fd, temp = tempfile.mkstemp(dir=blob.parent, prefix=".tmp-")
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                os.replace(temp, blob)
            except BaseException:
                Path(temp).unlink(missing_ok=True)
                raise
        try:
            with db.write(self.db_path) as conn:
                conn.execute("INSERT INTO artifacts VALUES (?,?,?,?)",
                             (artifact_id, digest, len(data), ref.model_dump_json()))
        except sqlite3.IntegrityError as error:
            raise Conflict("Artifact id already exists") from error
        return ref

    def ref(self, artifact_id: str) -> ArtifactRef:
        with db.read(self.db_path) as conn:
            row = conn.execute("SELECT body FROM artifacts WHERE artifact_id = ?",
                               (artifact_id,)).fetchone()
        if row is None:
            raise NotFound("Artifact not found")
        return db.load(ArtifactRef, row[0])

    def read(self, ref: ArtifactRef, *, max_bytes: int = DEFAULT_MAX_BYTES) -> bytes:
        stored = self.ref(ref.artifact_id)
        if stored != ref:
            raise IntegrityError("Artifact reference does not match stored metadata")
        if ref.size_bytes > max_bytes:
            raise ArtifactTooLarge("Artifact exceeds the requested read limit")
        blob = self._blob(ref.sha256)
        try:
            with blob.open("rb") as handle:
                data = handle.read(ref.size_bytes + 1)
        except FileNotFoundError as error:
            raise IntegrityError("Artifact bytes are missing") from error
        if len(data) != ref.size_bytes or hashlib.sha256(data).hexdigest() != ref.sha256:
            raise IntegrityError("Artifact bytes do not match their digest")
        return data
