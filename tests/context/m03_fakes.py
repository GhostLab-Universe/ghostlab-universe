"""In-memory MemoryStore test double honoring ghostlab-storage/1 semantics.

Test fixture only: M04 owns the real SQLite store. Fixtures are synthetic.
"""

from ghostlab.contracts import MemoryRecord, Principal, Provenance, Ref, TrustedScope
from ghostlab.contracts.primitives import parse_timestamp
from ghostlab.storage.interfaces import Conflict, NotFound, ScopeViolation

T0 = "2026-10-29T10:00:00Z"
T1 = "2026-10-29T11:00:00Z"
T2 = "2026-10-29T12:00:00Z"
NOW = "2026-10-29T13:00:00Z"
LATER = "2026-10-30T00:00:00Z"


class FakeMemoryStore:
    def __init__(self, records=()):
        self._records: dict[str, MemoryRecord] = {}
        for record in records:
            self._records[record.record_id] = record

    def put(self, scope, record):
        if record.project_id != scope.project_id:
            raise ScopeViolation("record project differs from scope")
        if record.record_id in self._records:
            raise Conflict("duplicate record_id")
        self._records[record.record_id] = record

    def get(self, scope, record_id):
        record = self._records.get(record_id)
        if record is None or record.project_id != scope.project_id:
            raise NotFound("no such record")
        return record

    def query(self, scope, query):
        if query.project_id != scope.project_id:
            raise ScopeViolation("query project differs from scope")
        as_of = parse_timestamp(query.as_of)

        def visible(r):
            return (r.project_id == scope.project_id and r.kind in query.kinds
                    and r.status in query.statuses
                    and (query.world_version is None or r.world_version == query.world_version)
                    and (query.campaign_id is None or r.campaign_id == query.campaign_id)
                    and parse_timestamp(r.effective_from) <= as_of
                    and (r.effective_until is None or as_of < parse_timestamp(r.effective_until))
                    and (r.retention.expires_at is None
                         or as_of < parse_timestamp(r.retention.expires_at)))

        matches = sorted((r for r in self._records.values() if visible(r)),
                         key=lambda r: r.record_id)
        matches.sort(key=lambda r: parse_timestamp(r.observed_at), reverse=True)
        return matches[:query.limit]

    def transition(self, scope, record_id, *, expected_status, new_status):
        record = self.get(scope, record_id)
        if record.status != expected_status:
            raise Conflict("status changed")
        updated = MemoryRecord.model_validate({**record.model_dump(), "status": new_status})
        self._records[record_id] = updated
        return updated


class LeakyMemoryStore(FakeMemoryStore):
    """Ignores every filter, to prove the compiler excludes bad records itself."""

    def query(self, scope, query):
        return sorted(self._records.values(), key=lambda r: r.record_id)


def scope(role="explorer", project="project-local", tools=()):
    return TrustedScope(
        project_id=project, principal=Principal(principal_id="local-dev-user", kind="local_dev"),
        role=role, delegated_by="supervisor", allowed_tools=list(tools),
        policy_version="policy-1", issued_by="controller")


def record(record_id, *, content="Blind retry after timeout over-refunds the order.",
           project="project-local", campaign="campaign-1", status="verified",
           observed=T1, effective_from=T0, effective_until=None, expires_at=None,
           evidence=("finding-1",), trust="verified", source_kind="verifier", kind="finding"):
    refs = [Ref(kind="finding", id=e, sha256=None) for e in evidence]
    return MemoryRecord(
        schema_version=1, record_id=record_id, project_id=project, campaign_id=campaign,
        world_version="refund-world/1", kind=kind, content=content, evidence=refs,
        provenance=Provenance(source_kind=source_kind, source_ref=refs[0] if refs else None,
                              observed_at=observed, trust=trust),
        status=status, observed_at=observed, effective_from=effective_from,
        effective_until=effective_until, supersedes=[],
        retention={"policy": "project", "expires_at": expires_at, "audit_hold": False})
