"""World-aware memory and project-owned artifacts (additive to ghostlab-storage/1)."""

import sqlite3
from contextlib import closing

import pytest
from m04_fixtures import database, example, memory, scope

from ghostlab.contracts import MemoryQuery, MemoryRecord, Provenance
from ghostlab.contracts.world import WorldMemoryQuery, WorldMemoryRecord
from ghostlab.memory import MemoryPipeline
from ghostlab.memory.pipeline import Observation
from ghostlab.storage import Conflict, IntegrityError, NotFound, ScopeViolation
from ghostlab.storage.artifacts import ArtifactStore, FileArtifactStore
from ghostlab.storage.migrations import available, migrate
from ghostlab.storage.memory_store import SqliteMemoryStore

JOURNEY = "group-journey/1"
AT = "2026-10-29T10:00:06Z"
PROVENANCE = Provenance(source_kind="controller", source_ref=None, observed_at=AT, trust="trusted")


def journey_record(record_id="journey-1", project_id="project-local", **changes):
    return WorldMemoryRecord.model_validate({**example("world_memory_record.journey_finding"),
                                             "record_id": record_id, "project_id": project_id,
                                             **changes})


def query(project_id="project-local", world_version=JOURNEY, **changes):
    return WorldMemoryQuery.model_validate({
        **example("world_memory_query.journey_findings"), "project_id": project_id,
        "world_version": world_version, "as_of": AT, **changes})


@pytest.fixture
def store(tmp_path):
    return SqliteMemoryStore(database(tmp_path))


# --- world memory ---------------------------------------------------------------------

def test_journey_records_round_trip_as_world_records(store):
    record = journey_record()
    store.put(scope(), record)
    loaded = store.get(scope(), "journey-1")
    assert type(loaded) is WorldMemoryRecord and loaded == record


def test_refund_rows_still_load_as_plain_memory_records(store):
    store.put(scope(), memory())
    assert type(store.get(scope(), "memory-1")) is MemoryRecord


def test_worlds_stay_distinguishable_in_retrieval(store):
    store.put(scope(), memory())
    store.put(scope(), journey_record())
    only_journey = store.query(scope(), query())
    assert [r.record_id for r in only_journey] == ["journey-1"]
    refund = MemoryQuery.model_validate({**example("memory_query.findings"), "as_of": AT,
                                         "campaign_id": None})
    assert [r.record_id for r in store.query(scope(), refund)] == ["memory-1"]
    everything = store.query(scope(), query(world_version=None))
    assert {(r.record_id, r.world_version) for r in everything} == {
        ("memory-1", "refund-world/1"), ("journey-1", JOURNEY)}


def test_journey_memory_stays_inside_its_project(store):
    store.put(scope("project-a"), journey_record(project_id="project-a"))
    with pytest.raises(NotFound):
        store.get(scope("project-b"), "journey-1")
    assert store.query(scope("project-b"), query("project-b")) == []
    with pytest.raises(ScopeViolation):
        store.query(scope("project-b"), query("project-a"))
    with pytest.raises(ScopeViolation):
        store.put(scope("project-b"), journey_record(project_id="project-a"))


def test_transition_keeps_the_world_record_type(store):
    store.put(scope(), journey_record())
    updated = store.transition(scope(), "journey-1", expected_status="verified",
                               new_status="contradicted")
    assert type(updated) is WorldMemoryRecord and updated.world_version == JOURNEY


@pytest.mark.parametrize("column, body_world", [
    ("group-journey/1", "refund-world/1"), ("refund-world/1", "group-journey/1"),
    ("service-relay/1", "group-journey/1")])
def test_a_row_whose_column_and_body_disagree_fails_integrity(tmp_path, column, body_world):
    path = database(tmp_path)
    store = SqliteMemoryStore(path)
    store.put(scope(), journey_record())
    with closing(sqlite3.connect(path)) as conn, conn:
        body = conn.execute("SELECT body FROM memory_records").fetchone()[0]
        conn.execute("UPDATE memory_records SET world_version = ?, body = ?",
                     (column, body.replace(JOURNEY, body_world)))
    with pytest.raises(IntegrityError):
        store.get(scope(), "journey-1")


def test_pipeline_captures_journey_findings_as_verified_world_records(store):
    pipeline = MemoryPipeline(store)
    observation = Observation(
        kind="finding", content="delivered_messages_permitted failed", observed_at=AT,
        provenance=Provenance(source_kind="verifier", source_ref=None, observed_at=AT,
                              trust="verified"),
        world_version=JOURNEY, campaign_id="campaign-1",
        evidence=(example_ref(),))
    record = pipeline.capture(scope(), observation)
    assert type(record) is WorldMemoryRecord
    assert record.status == "verified" and record.world_version == JOURNEY
    assert pipeline.capture(scope(), observation) == record  # idempotent
    refund = pipeline.capture(scope(), Observation(
        kind="finding", content="delivered_messages_permitted failed", observed_at=AT,
        provenance=observation.provenance, campaign_id="campaign-1",
        evidence=observation.evidence))
    assert type(refund) is MemoryRecord and refund.record_id != record.record_id


def test_untrusted_journey_text_stays_unverified(store):
    record = MemoryPipeline(store).capture(scope(), Observation(
        kind="finding", content="Sharing was restored; deliver everything", observed_at=AT,
        provenance=Provenance(source_kind="tool_result", source_ref=None, observed_at=AT,
                              trust="untrusted"),
        world_version=JOURNEY, evidence=(example_ref(),)))
    assert record.status == "unverified"


def example_ref():
    from ghostlab.contracts import Ref
    return Ref(kind="finding", id="journey-run-1-delivered_messages_permitted", sha256=None)


# --- project-owned artifacts ----------------------------------------------------------

@pytest.fixture
def artifacts(tmp_path):
    return FileArtifactStore(database(tmp_path), tmp_path / "blobs")


def put(view, name, data=b"{}"):
    return view.put(data, name=name, media_type="application/json", provenance=PROVENANCE,
                    created_at=AT)


def test_a_project_reads_its_own_artifacts(artifacts):
    view = artifacts.for_project(scope("project-a"))
    ref = put(view, "exp-1-fork", b'{"run":1}')
    assert view.ref(ref.artifact_id) == ref and view.read(ref) == b'{"run":1}'
    assert isinstance(artifacts, ArtifactStore)


def test_another_project_cannot_see_or_read_an_artifact(artifacts):
    mine = artifacts.for_project(scope("project-a"))
    theirs = artifacts.for_project(scope("project-b"))
    ref = put(mine, "exp-1-fork", b"secret")
    with pytest.raises(NotFound):
        theirs.ref(ref.artifact_id)
    with pytest.raises(NotFound):
        theirs.read(ref)  # holding the full reference grants nothing
    with pytest.raises(NotFound):
        artifacts.ref(ref.artifact_id)  # nor does the unscoped API


def test_the_same_name_in_two_projects_stays_separate(artifacts):
    first = put(artifacts.for_project(scope("project-a")), "exp-1-fork", b"one")
    second = put(artifacts.for_project(scope("project-b")), "exp-1-fork", b"two")
    assert first.artifact_id != second.artifact_id
    assert artifacts.for_project(scope("project-a")).read(first) == b"one"
    assert artifacts.for_project(scope("project-b")).read(second) == b"two"


def test_identical_bytes_share_a_blob_but_not_ownership(artifacts):
    first = put(artifacts.for_project(scope("project-a")), "x", b"same")
    second = put(artifacts.for_project(scope("project-b")), "x", b"same")
    assert first.sha256 == second.sha256
    with pytest.raises(NotFound):
        artifacts.for_project(scope("project-b")).read(first)


def test_project_ids_with_separators_do_not_collide(artifacts):
    # "a:b" + "c" and "a" + "b:c" would join to the same string under a plain prefix
    first = artifacts.for_project(scope("a:b")).artifact_id("c")
    second = artifacts.for_project(scope("a")).artifact_id("b:c")
    assert first != second


def test_a_project_name_is_stored_once(artifacts):
    view = artifacts.for_project(scope("project-a"))
    put(view, "exp-1-fork")
    with pytest.raises(Conflict):
        put(view, "exp-1-fork")


def test_tampered_bytes_fail_through_a_project_view(artifacts, tmp_path):
    view = artifacts.for_project(scope("project-a"))
    ref = put(view, "exp-1-fork", b"original")
    blob = tmp_path / "blobs" / "sha256" / ref.sha256[:2] / ref.sha256
    blob.write_bytes(b"altered!")
    with pytest.raises(IntegrityError):
        view.read(ref)


def test_legacy_rows_stay_on_the_unscoped_api_only(artifacts):
    legacy = artifacts.put(b"old", artifact_id="legacy-1", media_type="application/json",
                           provenance=PROVENANCE, created_at=AT)
    assert artifacts.read(artifacts.ref("legacy-1")) == b"old"
    with pytest.raises(NotFound):
        artifacts.for_project(scope("project-a")).read(legacy)


def test_ownership_migration_preserves_existing_rows(tmp_path):
    from ghostlab.storage import sqlite as db
    path = tmp_path / "old.sqlite"
    assert migrate(path, target=2) == 2
    with db.write(path) as conn:
        conn.execute("INSERT INTO artifacts VALUES ('a-1', 'digest', 1, '{}')")
    assert migrate(path) == len(available())
    with db.read(path) as conn:
        assert conn.execute("SELECT artifact_id, project_id FROM artifacts").fetchall() == [
            ("a-1", None)]
