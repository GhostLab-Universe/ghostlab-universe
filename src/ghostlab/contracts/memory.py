"""Scoped memory records and queries (M04 stores, M03 selects)."""

from typing import Literal

from pydantic import Field, model_validator

from .common import Provenance, Ref
from .primitives import (Identifier, LongText, SchemaVersion, StrictModel, UtcTimestamp,
                         WorldVersion, parse_timestamp, require_unique)

MemoryKind = Literal["episodic_event", "finding", "preference", "procedure_ref"]
MemoryStatus = Literal["unverified", "verified", "contradicted", "superseded", "expired"]


class RetentionPolicy(StrictModel):
    policy: Literal["campaign", "project", "audit"]
    expires_at: UtcTimestamp | None
    audit_hold: bool


class MemoryRecord(StrictModel):
    schema_version: SchemaVersion
    record_id: Identifier
    project_id: Identifier
    campaign_id: Identifier | None
    world_version: WorldVersion
    kind: MemoryKind
    content: LongText
    evidence: list[Ref] = Field(max_length=64)
    provenance: Provenance
    status: MemoryStatus
    observed_at: UtcTimestamp
    effective_from: UtcTimestamp
    effective_until: UtcTimestamp | None
    supersedes: list[Identifier] = Field(max_length=32)
    retention: RetentionPolicy

    @model_validator(mode="after")
    def _check(self):
        require_unique(self.supersedes, "supersedes")
        if self.record_id in self.supersedes:
            raise ValueError("A record cannot supersede itself")
        if (self.effective_until is not None
                and parse_timestamp(self.effective_until) <= parse_timestamp(self.effective_from)):
            raise ValueError("effective_until must follow effective_from")
        # Untrusted observations never become verified facts automatically.
        if self.status == "verified" and (self.provenance.trust == "untrusted"
                                          or not self.evidence):
            raise ValueError("Verified memory needs trusted provenance and evidence")
        return self


class MemoryQuery(StrictModel):
    """project_id is filled by the controller from TrustedScope, not by a model."""

    schema_version: SchemaVersion
    project_id: Identifier
    campaign_id: Identifier | None
    world_version: WorldVersion | None
    kinds: list[MemoryKind] = Field(min_length=1, max_length=4)
    statuses: list[MemoryStatus] = Field(min_length=1, max_length=5)
    as_of: UtcTimestamp
    limit: int = Field(ge=1, le=200)

    @model_validator(mode="after")
    def _check(self):
        require_unique(self.kinds, "kinds")
        require_unique(self.statuses, "statuses")
        return self
