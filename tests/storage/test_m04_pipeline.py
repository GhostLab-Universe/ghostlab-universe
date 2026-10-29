"""Memory pipeline: trust-derived status, normalization, contradiction, expiry, retention."""

import pytest
from m04_fixtures import database, query_scope_free_memory_query, scope

from ghostlab.contracts import Provenance, Ref
from ghostlab.memory import MemoryPipeline, Observation, normalize
from ghostlab.storage.memory_store import SqliteMemoryStore

EVIDENCE = (Ref.model_validate({"kind": "finding", "id": "finding-1", "sha256": None}),)


def provenance(source_kind, trust):
    return Provenance.model_validate({"source_kind": source_kind, "source_ref": None,
                                      "observed_at": "2026-10-29T10:00:05Z", "trust": trust})


def observation(content="Blind retry after timeout over-refunds", **changes):
    base = {"kind": "finding", "content": content,
            "provenance": provenance("verifier", "verified"),
            "observed_at": "2026-10-29T10:00:05Z", "campaign_id": "campaign-1",
            "evidence": EVIDENCE}
    return Observation(**{**base, **changes})


@pytest.fixture
def pipeline(tmp_path):
    return MemoryPipeline(SqliteMemoryStore(database(tmp_path)))


def statuses(pipeline):
    return {r.record_id: r.status
            for r in pipeline.retrieve(scope(), query_scope_free_memory_query())}


@pytest.mark.parametrize("source,trust,evidence,expected", [
    ("verifier", "verified", EVIDENCE, "verified"),
    ("human", "trusted", EVIDENCE, "verified"),
    ("verifier", "verified", (), "unverified"),       # no evidence, no verification
    ("tool_result", "untrusted", EVIDENCE, "unverified"),
    ("model_output", "untrusted", EVIDENCE, "unverified"),
    ("memory", "verified", EVIDENCE, "unverified"),   # memory cannot vouch for itself
])
def test_status_comes_from_provenance_not_the_caller(pipeline, source, trust, evidence, expected):
    record = pipeline.capture(scope(), observation(provenance=provenance(source, trust),
                                                   evidence=evidence))
    assert record.status == expected


def test_capture_normalizes_and_is_idempotent(pipeline):
    first = pipeline.capture(scope(), observation("  Blind retry \n\n over-refunds\x07 "))
    again = pipeline.capture(scope(), observation("Blind retry over-refunds"))
    assert first.content == "Blind retry over-refunds"
    assert again == first
    assert len(statuses(pipeline)) == 1


@pytest.mark.parametrize("bad", ["", "   \n\t", "x" * 8_001])
def test_normalize_rejects_empty_and_oversized(bad):
    with pytest.raises(ValueError):
        normalize(bad)


def test_supersede_keeps_history_and_retires_old(pipeline):
    old = pipeline.capture(scope(), observation())
    new = pipeline.supersede(scope(), old.record_id,
                             observation("Status check before retry prevents it",
                                         observed_at="2026-10-29T10:10:00Z"))
    assert new.supersedes == [old.record_id]
    assert statuses(pipeline) == {old.record_id: "superseded", new.record_id: "verified"}


def test_contradiction_stays_visible(pipeline):
    record = pipeline.capture(scope(), observation())
    pipeline.contradict(scope(), record.record_id)
    assert statuses(pipeline)[record.record_id] == "contradicted"
    assert pipeline.contradict(scope(), record.record_id).status == "contradicted"


def test_expiry_respects_audit_hold_and_purge_keeps_references(pipeline):
    store = pipeline.store
    soon = "2026-10-29T11:00:00Z"
    doomed = pipeline.capture(scope(), observation("A", expires_at=soon))
    held = pipeline.capture(scope(), observation("B", expires_at=soon, audit_hold=True))
    referenced = pipeline.capture(scope(), observation("C", expires_at=soon))
    pipeline.capture(scope(), observation("D", supersedes=(referenced.record_id,)))
    expired = {r.record_id for r in pipeline.expire_due(scope(), "2026-10-29T11:00:00Z")}
    assert expired == {doomed.record_id, referenced.record_id}
    assert store.get(scope(), held.record_id).status == "verified"
    assert store.purge_expired(scope()) == 1
    remaining = {r.record_id for r in store.due_for_expiry(scope(), "2026-10-30T00:00:00Z")}
    assert held.record_id not in remaining  # audit hold is never due
    assert store.get(scope(), referenced.record_id).status == "expired"
