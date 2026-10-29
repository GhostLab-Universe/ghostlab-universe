"""Memory capture, filtering, normalization, retrieval, contradiction and expiry.

Status is decided here from provenance, never by the caller: content from a
tool result, model output or memory itself is stored ``unverified``. Only
verifier, controller or human sources with evidence references start
``verified``. Promoting an unverified record later requires a new record with
trusted provenance; ``transition`` re-validation refuses to verify untrusted
provenance in place.
"""

import hashlib
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field

from ghostlab.contracts import MemoryQuery, MemoryRecord, Provenance, Ref, TrustedScope

from ..storage.interfaces import Conflict
from ..storage.memory_store import SqliteMemoryStore

MAX_CONTENT_CHARS = 8_000
_TRUSTED_SOURCES = {"verifier", "controller", "human"}


@dataclass(frozen=True)
class Observation:
    """Raw input to memory. Content is data, never an instruction."""

    kind: str
    content: str
    provenance: Provenance
    observed_at: str
    world_version: str = "refund-world/1"
    campaign_id: str | None = None
    evidence: tuple[Ref, ...] = ()
    effective_until: str | None = None
    retention_policy: str = "project"
    expires_at: str | None = None
    audit_hold: bool = False
    supersedes: tuple[str, ...] = field(default=())


def normalize(content: str) -> str:
    """NFC, collapse whitespace, strip control characters; reject empty or oversized."""
    text = unicodedata.normalize("NFC", content)
    text = "".join(ch for ch in text if ch in "\n\t" or unicodedata.category(ch)[0] != "C")
    text = " ".join(text.split())
    if not text:
        raise ValueError("Observation content is empty after normalization")
    if len(text) > MAX_CONTENT_CHARS:
        raise ValueError("Observation content exceeds the memory size limit")
    return text


def initial_status(observation: Observation) -> str:
    trusted = (observation.provenance.source_kind in _TRUSTED_SOURCES
               and observation.provenance.trust != "untrusted" and observation.evidence)
    return "verified" if trusted else "unverified"


def record_id_for(scope: TrustedScope, observation: Observation, content: str) -> str:
    """Deterministic id, so capturing the same observation twice is idempotent."""
    key = "\x1f".join([scope.project_id, observation.kind, observation.world_version,
                       observation.campaign_id or "", observation.observed_at, content])
    return "memory-" + hashlib.sha256(key.encode()).hexdigest()[:32]


class MemoryPipeline:
    def __init__(self, store: SqliteMemoryStore):
        self.store = store

    def capture(self, scope: TrustedScope, observation: Observation) -> MemoryRecord:
        """Filter, normalize and store; returns the existing record on a duplicate."""
        content = normalize(observation.content)
        record_id = record_id_for(scope, observation, content)
        record = MemoryRecord.model_validate({
            "schema_version": 1, "record_id": record_id, "project_id": scope.project_id,
            "campaign_id": observation.campaign_id, "world_version": observation.world_version,
            "kind": observation.kind, "content": content,
            "evidence": [ref.model_dump(mode="json") for ref in observation.evidence],
            "provenance": observation.provenance.model_dump(mode="json"),
            "status": initial_status(observation), "observed_at": observation.observed_at,
            "effective_from": observation.observed_at,
            "effective_until": observation.effective_until,
            "supersedes": list(observation.supersedes),
            "retention": {"policy": observation.retention_policy,
                          "expires_at": observation.expires_at,
                          "audit_hold": observation.audit_hold}})
        try:
            self.store.put(scope, record)
        except Conflict:
            return self.store.get(scope, record_id)
        return record

    def retrieve(self, scope: TrustedScope, query: MemoryQuery) -> Sequence[MemoryRecord]:
        return self.store.query(scope, query)

    def supersede(self, scope: TrustedScope, old_record_id: str,
                  observation: Observation) -> MemoryRecord:
        """Store a replacement and retire the old record. Both stay auditable."""
        old = self.store.get(scope, old_record_id)
        replacement = self.capture(scope, Observation(**{
            **observation.__dict__, "supersedes": (*observation.supersedes, old_record_id)}))
        if old.status in {"unverified", "verified"}:
            self.store.transition(scope, old_record_id, expected_status=old.status,
                                  new_status="superseded")
        return replacement

    def contradict(self, scope: TrustedScope, record_id: str) -> MemoryRecord:
        """Mark a record contradicted by new evidence; it remains visible to queries
        that ask for ``contradicted`` so conflicts are not silently dropped."""
        current = self.store.get(scope, record_id)
        if current.status == "contradicted":
            return current
        return self.store.transition(scope, record_id, expected_status=current.status,
                                     new_status="contradicted")

    def expire_due(self, scope: TrustedScope, as_of: str) -> list[MemoryRecord]:
        """Move records past retention to ``expired`` (audit holds are skipped)."""
        expired = []
        for record in self.store.due_for_expiry(scope, as_of):
            try:
                expired.append(self.store.transition(scope, record.record_id,
                                                     expected_status=record.status,
                                                     new_status="expired"))
            except Conflict:
                continue  # changed concurrently; the next sweep sees its new state
        return expired
