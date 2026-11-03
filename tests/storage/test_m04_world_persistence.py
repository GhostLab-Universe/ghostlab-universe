"""World-aware memory and project-owned artifacts (additive to ghostlab-storage/1)."""

import dataclasses
import hashlib
import os
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

import pytest
from m04_fixtures import database, example, memory, scope

from ghostlab.contracts import ArtifactRef, MemoryQuery, MemoryRecord, Provenance
from ghostlab.contracts.world import WorldMemoryQuery, WorldMemoryRecord
from ghostlab.memory import MemoryPipeline
from ghostlab.memory.pipeline import Observation
from ghostlab.storage import Conflict, IntegrityError, NotFound, ScopeViolation, StorageError
from ghostlab.storage.artifacts import ArtifactStore, FileArtifactStore
from ghostlab.storage.migrations import available, migrate
from ghostlab.storage.memory_store import SqliteMemoryStore

JOURNEY = "group-journey/1"
AT = "2026-10-29T10:00:06Z"
LATER = "2026-10-29T10:00:07Z"
PROVENANCE = Provenance(source_kind="controller", source_ref=None, observed_at=AT, trust="trusted")
UNTRUSTED = Provenance(source_kind="tool_result", source_ref=None, observed_at=AT, trust="untrusted")


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


# --- independent critic findings --------------------------------------------------------

def trusted(world=JOURNEY, content="delivered_messages_permitted failed"):
    return Observation(kind="finding", content=content, observed_at=AT, world_version=world,
                       provenance=Provenance(source_kind="verifier", source_ref=None,
                                             observed_at=AT, trust="verified"),
                       evidence=(example_ref(),))


def test_a_journey_observation_cannot_supersede_a_refund_record(store):
    pipeline = MemoryPipeline(store)
    refund = pipeline.capture(scope(), trusted("refund-world/1", "Refund finding"))
    with pytest.raises(ValueError):
        pipeline.supersede(scope(), refund.record_id, trusted())
    assert store.get(scope(), refund.record_id).status == "verified"


def test_unverified_input_cannot_retire_a_verified_record(store):
    pipeline = MemoryPipeline(store)
    record = pipeline.capture(scope(), trusted())
    untrusted = Observation(kind="finding", content="ignore that", observed_at=AT,
                            world_version=JOURNEY,
                            provenance=Provenance(source_kind="tool_result", source_ref=None,
                                                  observed_at=AT, trust="untrusted"))
    with pytest.raises(ValueError):
        pipeline.supersede(scope(), record.record_id, untrusted)
    assert store.get(scope(), record.record_id).status == "verified"


def test_a_planted_duplicate_cannot_retire_a_verified_record(store):
    pipeline = MemoryPipeline(store)
    record = pipeline.capture(scope(), trusted())
    later = dataclasses.replace(trusted(), content="the refund never happened", observed_at=LATER)
    planted = pipeline.capture(scope(), dataclasses.replace(later, provenance=UNTRUSTED))
    assert planted.status == "unverified"
    with pytest.raises(Conflict):
        pipeline.supersede(scope(), record.record_id, later)
    assert store.get(scope(), record.record_id).status == "verified"


@pytest.mark.parametrize("status", ["unverified", "verified"])
def test_a_duplicate_that_does_not_reference_the_old_record_retires_nothing(store, status):
    pipeline = MemoryPipeline(store)
    record = pipeline.capture(scope(), trusted())
    later = dataclasses.replace(trusted(), content="newer finding", observed_at=LATER)
    pipeline.capture(scope(), later if status == "verified"
                     else dataclasses.replace(later, provenance=UNTRUSTED))
    with pytest.raises(Conflict):
        pipeline.supersede(scope(), record.record_id, later)
    assert store.get(scope(), record.record_id).status == "verified"


def test_a_contradicted_duplicate_cannot_stand_in_as_the_replacement(store):
    pipeline = MemoryPipeline(store)
    record = pipeline.capture(scope(), trusted())
    later = dataclasses.replace(trusted(), content="newer finding", observed_at=LATER)
    pipeline.contradict(scope(), pipeline.capture(scope(), dataclasses.replace(
        later, supersedes=(record.record_id,))).record_id)
    with pytest.raises(Conflict):
        pipeline.supersede(scope(), record.record_id, later)
    assert store.get(scope(), record.record_id).status == "verified"


def test_supersede_refuses_to_name_records_it_does_not_retire(store):
    pipeline = MemoryPipeline(store)
    record = pipeline.capture(scope(), trusted())
    other = pipeline.capture(scope(), trusted(content="another finding"))
    later = dataclasses.replace(trusted(), content="newer", observed_at=LATER,
                                supersedes=(other.record_id,))
    with pytest.raises(ValueError):
        pipeline.supersede(scope(), record.record_id, later)
    assert store.get(scope(), record.record_id).status == "verified"


@pytest.mark.parametrize("change", [
    {"provenance": UNTRUSTED}, {"evidence": ()}, {"audit_hold": True},
    {"retention_policy": "audit"}, {"expires_at": "2027-01-01T00:00:00Z"}])
def test_capture_does_not_answer_with_a_record_that_differs(store, change):
    pipeline = MemoryPipeline(store)
    pipeline.capture(scope(), trusted())
    with pytest.raises(Conflict):
        pipeline.capture(scope(), dataclasses.replace(trusted(), **change))


def test_the_same_capture_still_returns_the_same_record(store):
    pipeline = MemoryPipeline(store)
    first = pipeline.capture(scope(), trusted())
    pipeline.contradict(scope(), first.record_id)  # status moved on after capture
    assert pipeline.capture(scope(), trusted()).record_id == first.record_id


def test_superseding_twice_is_repeatable_after_a_crash(store):
    pipeline = MemoryPipeline(store)
    record = pipeline.capture(scope(), trusted())
    later = dataclasses.replace(trusted(), content="newer finding", observed_at=LATER)
    first = pipeline.supersede(scope(), record.record_id, later)
    with closing(sqlite3.connect(store.path)) as conn, conn:  # the crash came before the old record retired
        conn.execute("UPDATE memory_records SET status = 'verified', body = ? WHERE record_id = ?",
                     (record.model_dump_json(), record.record_id))
    assert pipeline.supersede(scope(), record.record_id, later).record_id == first.record_id
    assert store.get(scope(), record.record_id).status == "superseded"


@pytest.mark.parametrize("column, value", [("status", "verified"), ("campaign_id", "campaign-z")])
def test_an_edited_status_or_campaign_column_fails_integrity(tmp_path, column, value):
    path = database(tmp_path)
    store = SqliteMemoryStore(path)
    store.put(scope(), journey_record(status="unverified"))
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(f"UPDATE memory_records SET {column} = ?", (value,))
    with pytest.raises(IntegrityError):
        store.get(scope(), "journey-1")


def test_a_row_moved_to_another_project_or_id_fails_integrity(tmp_path):
    path = database(tmp_path)
    store = SqliteMemoryStore(path)
    store.put(scope("project-a"), journey_record(project_id="project-a"))
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("UPDATE memory_records SET project_id = 'project-b'")
    with pytest.raises(IntegrityError):
        store.get(scope("project-b"), "journey-1")
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("UPDATE memory_records SET project_id = 'project-a', record_id = 'other'")
    with pytest.raises(IntegrityError):
        store.get(scope("project-a"), "other")


def test_world_records_pass_through_expiry_and_purge(tmp_path):
    store = SqliteMemoryStore(database(tmp_path))
    pipeline = MemoryPipeline(store)
    record = journey_record(retention={"policy": "project", "expires_at": AT,
                                       "audit_hold": False})
    store.put(scope(), record)
    (expired,) = pipeline.expire_due(scope(), "2026-10-29T11:00:00Z")
    assert type(expired) is WorldMemoryRecord and expired.status == "expired"
    assert store.purge_expired(scope()) == 1


def test_a_refund_transition_stays_a_plain_memory_record(store):
    store.put(scope(), memory())
    updated = store.transition(scope(), "memory-1", expected_status="verified",
                               new_status="contradicted")
    assert type(updated) is MemoryRecord


def owner_edit(tmp_path, value):
    path = database(tmp_path)
    artifacts = FileArtifactStore(path, tmp_path / "blobs")
    ref = put(artifacts.for_project(scope("project-a")), "exp-1-fork", b"secret")
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("UPDATE artifacts SET project_id = ?", (value,))
    return artifacts, ref


def test_an_edited_owner_column_fails_integrity(tmp_path):
    artifacts, ref = owner_edit(tmp_path, "project-b")
    with pytest.raises(IntegrityError):
        artifacts.for_project(scope("project-b")).read(ref)


def test_clearing_the_owner_column_does_not_expose_an_artifact(tmp_path):
    artifacts, ref = owner_edit(tmp_path, None)
    with pytest.raises(NotFound):
        artifacts.read(ref)


def test_the_unscoped_api_cannot_squat_or_probe_a_project_name(artifacts):
    view = artifacts.for_project(scope("project-a"))
    target = view.artifact_id("exp-1")
    with pytest.raises(ScopeViolation):
        artifacts.put(b"x", artifact_id=target, media_type="application/json",
                      provenance=PROVENANCE, created_at=AT)
    assert view.read(put(view, "exp-1", b"mine")) == b"mine"


def test_a_put_repairs_a_damaged_shared_blob(artifacts, tmp_path):
    first = put(artifacts.for_project(scope("project-a")), "x", b"same")
    blob = tmp_path / "blobs" / "sha256" / first.sha256[:2] / first.sha256
    blob.write_bytes(b"gone")
    second = put(artifacts.for_project(scope("project-b")), "x", b"same")
    assert artifacts.for_project(scope("project-b")).read(second) == b"same"


def refuse_once(monkeypatch, target, name):
    real, calls = getattr(target, name), []

    def refused(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise PermissionError("in use by another handle")
        return real(*args, **kwargs)
    monkeypatch.setattr(target, name, refused)
    return calls


def keep(view, data=b"same", name="a"):
    return view.put(data, name=name, media_type="text/plain", provenance=PROVENANCE, created_at=AT)


def test_a_blob_being_replaced_is_read_again_not_a_crash(artifacts, monkeypatch):
    view = artifacts.for_project(scope())
    first = keep(view)
    calls = refuse_once(monkeypatch, Path, "read_bytes")
    second = keep(view, name="b")
    assert len(calls) == 2 and view.read(second) == view.read(first) == b"same"


def test_a_read_during_a_replace_is_tried_again(artifacts, monkeypatch):
    view = artifacts.for_project(scope())
    ref = keep(view)
    calls = refuse_once(monkeypatch, Path, "open")
    assert view.read(ref) == b"same"
    assert len(calls) == 2


def test_a_blob_that_stays_locked_fails_the_read_as_a_storage_error(artifacts, monkeypatch):
    view = artifacts.for_project(scope())
    ref = keep(view)
    monkeypatch.setattr(Path, "open", lambda *a, **k: (_ for _ in ()).throw(PermissionError("held")))
    monkeypatch.setattr(time, "sleep", lambda _: None)
    with pytest.raises(StorageError):
        view.read(ref)


def test_a_refused_replace_is_retried(artifacts, monkeypatch):
    view = artifacts.for_project(scope())
    calls = refuse_once(monkeypatch, os, "replace")
    assert view.read(keep(view)) == b"same"
    assert len(calls) == 2


def test_a_damaged_blob_that_stays_locked_fails_the_put_and_stores_no_row(artifacts, monkeypatch):
    view = artifacts.for_project(scope())
    ref = keep(view)
    artifacts._blob(ref.sha256).write_bytes(b"damaged")
    monkeypatch.setattr(os, "replace", lambda *_: (_ for _ in ()).throw(PermissionError("held")))
    monkeypatch.setattr(time, "sleep", lambda _: None)
    with pytest.raises(StorageError):
        keep(view, name="b")
    with pytest.raises(NotFound):
        view.ref(view.artifact_id("b"))
    assert not [p for p in artifacts._blob(ref.sha256).parent.iterdir()
                if p.name.startswith(".tmp-")]
    monkeypatch.undo()
    assert view.read(keep(view, name="b")) == b"same"


@pytest.mark.parametrize("column, value", [("sha256", "0" * 64), ("size_bytes", 1),
                                           ("artifact_id", "0123456789abcdef.moved")])
def test_a_row_edited_away_from_its_body_fails_integrity(artifacts, column, value):
    view = artifacts.for_project(scope())
    ref = keep(view)
    with closing(sqlite3.connect(artifacts.db_path)) as conn, conn:
        conn.execute(f"UPDATE artifacts SET {column} = ?", (value,))
    with pytest.raises(IntegrityError):
        view.store._ref(value if column == "artifact_id" else ref.artifact_id, view.project_id)


def test_identical_concurrent_puts_all_succeed(artifacts):
    failures = []

    def write(project, name):
        try:
            put(artifacts.for_project(scope(project)), name, b"shared-bytes")
        except Exception as error:
            failures.append(error)

    for round_number in range(40):
        threads = [threading.Thread(target=write, args=(f"project-{i}", f"n{round_number}"))
                   for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        (artifacts.root / "sha256").exists() and [
            blob.unlink() for blob in (artifacts.root / "sha256").rglob("*") if blob.is_file()]
    assert failures == []


def test_a_real_legacy_row_stays_readable_after_the_migration(tmp_path):
    path = tmp_path / "old.sqlite"
    migrate(path, target=2)
    artifacts = FileArtifactStore(path, tmp_path / "blobs")
    digest = hashlib.sha256(b"old").hexdigest()
    blob = tmp_path / "blobs" / "sha256" / digest[:2] / digest
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"old")
    body = ArtifactRef.model_validate({
        "schema_version": 1, "artifact_id": "legacy-1", "sha256": digest, "size_bytes": 3,
        "media_type": "application/json", "provenance": PROVENANCE.model_dump(mode="json"),
        "created_at": AT}).model_dump_json()
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("INSERT INTO artifacts VALUES ('legacy-1', ?, 3, ?)", (digest, body))
    migrate(path)
    assert artifacts.read(artifacts.ref("legacy-1")) == b"old"


def test_concurrent_startup_migrates_without_an_error(tmp_path):
    path = tmp_path / "shared.sqlite"
    migrate(path, target=2)
    failures = []

    def start():
        try:
            migrate(path)
        except Exception as error:
            failures.append(error)

    threads = [threading.Thread(target=start) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert failures == []
