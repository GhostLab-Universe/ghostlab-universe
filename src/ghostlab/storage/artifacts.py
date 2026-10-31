"""Content-addressed artifact bytes with validated metadata (M04 extension).

Bytes live at ``<root>/sha256/<aa>/<digest>``; metadata is an M00 ArtifactRef
in SQLite. Reads are bounded and verify size and digest, so missing or altered
bytes fail as IntegrityError. Authorization to read an artifact is M06/M07's.

Ownership (``ghostlab-storage-artifacts/2``, additive): ``for_project`` returns a
view bound to a controller-issued ``TrustedScope``. It stores the owning project
in its own column and namespaces ids, and every read filters on that column, so
another project's artifact reads as NotFound. The namespace only keeps ids apart;
the column is what enforces ownership. The unscoped methods see rows with no
owner (written before ownership existed) and never see a project's artifacts.
"""

import hashlib
import os
import re
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Protocol, runtime_checkable

from ghostlab.contracts import ArtifactRef, Provenance, TrustedScope

from . import sqlite as db
from .interfaces import Conflict, IntegrityError, NotFound, ScopeViolation, StorageError

ARTIFACT_INTERFACE_VERSION = "ghostlab-storage-artifacts/2"
DEFAULT_MAX_BYTES = 16 * 1024 * 1024
_PROJECT_ID = re.compile(r"[0-9a-f]{16}\.")  # the namespace a project view writes under
_BLOB_ATTEMPTS = 8  # Windows refuses a file another handle has open; these clear in moments


class ArtifactTooLarge(StorageError):
    pass


@runtime_checkable
class ArtifactStore(Protocol):
    def put(self, data: bytes, *, artifact_id: str, media_type: str,
            provenance: Provenance, created_at: str) -> ArtifactRef: ...
    def ref(self, artifact_id: str) -> ArtifactRef: ...
    def read(self, ref: ArtifactRef, *, max_bytes: int = DEFAULT_MAX_BYTES) -> bytes: ...
    def for_project(self, scope: TrustedScope) -> "ProjectArtifacts": ...


class ProjectArtifacts:
    """One project's artifacts. The project comes from a trusted scope, never a caller id."""

    def __init__(self, store: "FileArtifactStore", project_id: str):
        self.store, self.project_id = store, project_id

    def artifact_id(self, name: str) -> str:
        # A digest, not the project id: identifiers may contain ":" and "."
        return f"{hashlib.sha256(self.project_id.encode()).hexdigest()[:16]}.{name}"

    def put(self, data: bytes, *, name: str, media_type: str, provenance: Provenance,
            created_at: str) -> ArtifactRef:
        return self.store._put(data, artifact_id=self.artifact_id(name), media_type=media_type,
                               provenance=provenance, created_at=created_at,
                               project_id=self.project_id)

    def ref(self, artifact_id: str) -> ArtifactRef:
        stored = self.store._ref(artifact_id, self.project_id)
        if not artifact_id.startswith(self.artifact_id("")):
            raise IntegrityError("Artifact owner does not match its id")  # a tampered row
        return stored

    def read(self, ref: ArtifactRef, *, max_bytes: int = DEFAULT_MAX_BYTES) -> bytes:
        self.ref(ref.artifact_id)
        return self.store._read(ref, self.project_id, max_bytes)


class FileArtifactStore:
    def __init__(self, db_path: Path, root: Path, *, max_bytes: int = DEFAULT_MAX_BYTES):
        self.db_path, self.root, self.max_bytes = db_path, root, max_bytes

    def _blob(self, digest: str) -> Path:
        return self.root / "sha256" / digest[:2] / digest

    def for_project(self, scope: TrustedScope) -> ProjectArtifacts:
        return ProjectArtifacts(self, scope.project_id)

    def put(self, data: bytes, *, artifact_id: str, media_type: str,
            provenance: Provenance, created_at: str) -> ArtifactRef:
        if _PROJECT_ID.match(artifact_id):  # can't squat a project's name or probe for it
            raise ScopeViolation("Project artifacts are written through a project view")
        return self._put(data, artifact_id=artifact_id, media_type=media_type,
                         provenance=provenance, created_at=created_at, project_id=None)

    def ref(self, artifact_id: str) -> ArtifactRef:
        if _PROJECT_ID.match(artifact_id):
            raise NotFound("Artifact not found")  # a tampered owner can't expose it here
        return self._ref(artifact_id, None)

    def read(self, ref: ArtifactRef, *, max_bytes: int = DEFAULT_MAX_BYTES) -> bytes:
        self.ref(ref.artifact_id)
        return self._read(ref, None, max_bytes)

    def _put(self, data: bytes, *, artifact_id: str, media_type: str, provenance: Provenance,
             created_at: str, project_id: str | None) -> ArtifactRef:
        if type(data) is not bytes:
            raise TypeError("data must be bytes")
        if len(data) > self.max_bytes:
            raise ArtifactTooLarge("Artifact exceeds the store's size limit")
        digest = hashlib.sha256(data).hexdigest()
        ref = ArtifactRef.model_validate({
            "schema_version": 1, "artifact_id": artifact_id, "sha256": digest,
            "size_bytes": len(data), "media_type": media_type,
            "provenance": provenance.model_dump(mode="json"), "created_at": created_at})
        self._store_blob(self._blob(digest), data, digest)
        try:
            with db.write(self.db_path) as conn:
                conn.execute("INSERT INTO artifacts (artifact_id, sha256, size_bytes, body, "
                             "project_id) VALUES (?,?,?,?,?)",
                             (artifact_id, digest, len(data), ref.model_dump_json(), project_id))
        except sqlite3.IntegrityError as error:
            raise Conflict("Artifact id already exists") from error
        return ref

    def _store_blob(self, blob: Path, data: bytes, digest: str) -> None:
        """Leave intact bytes at ``blob``, whoever wrote them, or raise.

        Blobs are shared, so a damaged one is rewritten, not inherited. A concurrent
        reader or writer can refuse either step on Windows, so nothing counts as
        stored until the bytes have been read back.
        """
        for attempt in range(_BLOB_ATTEMPTS):
            try:
                if blob.exists() and hashlib.sha256(blob.read_bytes()).hexdigest() == digest:
                    return
                blob.parent.mkdir(parents=True, exist_ok=True)
                fd, temp = tempfile.mkstemp(dir=blob.parent, prefix=".tmp-")
                try:
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(data)
                    os.replace(temp, blob)
                finally:
                    Path(temp).unlink(missing_ok=True)
            except PermissionError:
                time.sleep(0.005 * 2 ** attempt)
        raise StorageError("Artifact bytes could not be stored")

    def _read_blob(self, blob: Path, limit: int) -> bytes:
        for attempt in range(_BLOB_ATTEMPTS):
            try:
                with blob.open("rb") as handle:
                    return handle.read(limit)
            except FileNotFoundError as error:
                raise IntegrityError("Artifact bytes are missing") from error
            except PermissionError:  # another writer is replacing this shared blob
                time.sleep(0.005 * 2 ** attempt)
        raise StorageError("Artifact bytes could not be read")

    def _ref(self, artifact_id: str, project_id: str | None) -> ArtifactRef:
        with db.read(self.db_path) as conn:
            row = conn.execute("SELECT sha256, size_bytes, body FROM artifacts "
                               "WHERE artifact_id = ? AND project_id IS ?",
                               (artifact_id, project_id)).fetchone()
        if row is None:
            raise NotFound("Artifact not found")  # also another project's: not disclosed
        ref = db.load(ArtifactRef, row[2])
        if (ref.artifact_id, ref.sha256, ref.size_bytes) != (artifact_id, row[0], row[1]):
            raise IntegrityError("Stored artifact metadata disagrees with its row")
        return ref

    def _read(self, ref: ArtifactRef, project_id: str | None, max_bytes: int) -> bytes:
        stored = self._ref(ref.artifact_id, project_id)
        if stored != ref:
            raise IntegrityError("Artifact reference does not match stored metadata")
        if ref.size_bytes > max_bytes:
            raise ArtifactTooLarge("Artifact exceeds the requested read limit")
        data = self._read_blob(self._blob(ref.sha256), ref.size_bytes + 1)
        if len(data) != ref.size_bytes or hashlib.sha256(data).hexdigest() != ref.sha256:
            raise IntegrityError("Artifact bytes do not match their digest")
        return data
