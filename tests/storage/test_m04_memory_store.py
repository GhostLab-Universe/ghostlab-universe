"""SqliteMemoryStore: isolation, selective retrieval, CAS, restart and contention."""

import sqlite3
import threading
from contextlib import closing

import pytest
from m04_fixtures import database, memory, scope

from ghostlab.contracts import MemoryQuery
from ghostlab.storage import (Conflict, IntegrityError, MemoryStore, NotFound,
                              ScopeViolation)
from ghostlab.storage.memory_store import SqliteMemoryStore

UNTRUSTED = {"source_kind": "tool_result", "source_ref": None,
             "observed_at": "2026-10-29T10:00:05Z", "trust": "untrusted"}


def query(project_id="project-local", **changes):
    return MemoryQuery.model_validate({
        "schema_version": 1, "project_id": project_id, "campaign_id": None,
        "world_version": "refund-world/1", "kinds": ["finding"],
        "statuses": ["verified", "unverified"], "as_of": "2026-10-29T12:00:00Z",
        "limit": 50, **changes})


@pytest.fixture
def store(tmp_path):
    return SqliteMemoryStore(database(tmp_path))


def test_satisfies_published_interface(store):
    assert isinstance(store, MemoryStore)


def test_put_get_and_restart_recovery(tmp_path):
    path = database(tmp_path)
    SqliteMemoryStore(path).put(scope(), memory())
    assert SqliteMemoryStore(path).get(scope(), "memory-1") == memory()


def test_project_isolation(store):
    store.put(scope(), memory())
    with pytest.raises(NotFound):
        store.get(scope("project-other"), "memory-1")
    with pytest.raises(ScopeViolation):
        store.query(scope("project-other"), query())
    with pytest.raises(ScopeViolation):
        store.put(scope("project-other"), memory())
    assert store.query(scope("project-other"), query("project-other")) == []
    # The same id in another project is a separate record, not a collision.
    store.put(scope("project-other"), memory(project_id="project-other"))
    assert store.get(scope(), "memory-1").project_id == "project-local"


def test_duplicate_record_id_conflicts(store):
    store.put(scope(), memory())
    with pytest.raises(Conflict):
        store.put(scope(), memory())


def test_query_filters_effective_window_retention_order_and_limit(store):
    store.put(scope(), memory("memory-a", observed_at="2026-10-29T10:00:01Z"))
    store.put(scope(), memory("memory-b", observed_at="2026-10-29T10:00:03.5Z"))
    store.put(scope(), memory("memory-c", observed_at="2026-10-29T10:00:03Z"))
    store.put(scope(), memory("memory-future", effective_from="2026-10-30T00:00:00Z"))
    store.put(scope(), memory("memory-ended", effective_until="2026-10-29T11:00:00Z"))
    store.put(scope(), memory("memory-expired", retention={
        "policy": "project", "expires_at": "2026-10-29T11:30:00Z", "audit_hold": False}))
    store.put(scope(), memory("memory-pref", kind="preference"))
    store.put(scope(), memory("memory-other-campaign", campaign_id="campaign-2"))
    ids = [r.record_id for r in store.query(scope(), query(campaign_id="campaign-1"))]
    # Fractional seconds sort numerically, not lexically.
    assert ids == ["memory-b", "memory-c", "memory-a"]
    assert len(store.query(scope(), query(campaign_id="campaign-1", limit=2))) == 2
    assert [r.record_id for r in store.query(scope(), query(kinds=["preference"]))] == [
        "memory-pref"]
    assert "memory-ended" in {r.record_id for r in store.query(
        scope(), query(as_of="2026-10-29T10:30:00Z"))}


def test_transition_is_compare_and_set(store):
    store.put(scope(), memory())
    updated = store.transition(scope(), "memory-1", expected_status="verified",
                               new_status="contradicted")
    assert updated.status == "contradicted" == store.get(scope(), "memory-1").status
    with pytest.raises(Conflict):
        store.transition(scope(), "memory-1", expected_status="verified",
                         new_status="superseded")
    with pytest.raises(NotFound):
        store.transition(scope("project-other"), "memory-1", expected_status="contradicted",
                         new_status="expired")


def test_untrusted_record_cannot_be_verified_in_place(store):
    store.put(scope(), memory(provenance=UNTRUSTED, status="unverified"))
    with pytest.raises(Conflict):
        store.transition(scope(), "memory-1", expected_status="unverified",
                         new_status="verified")
    assert store.get(scope(), "memory-1").status == "unverified"


def test_tampered_row_fails_cleanly(store):
    store.put(scope(), memory())
    with closing(sqlite3.connect(store.path)) as conn, conn:
        conn.execute("UPDATE memory_records SET body = json_set(body, '$.status', 'bogus')")
    with pytest.raises(IntegrityError):
        store.get(scope(), "memory-1")


def test_concurrent_puts_lose_no_updates(store):
    errors = []

    def put(index):
        try:
            store.put(scope(), memory(f"memory-{index:03d}"))
        except Exception as error:  # surfaced below
            errors.append(error)
    threads = [threading.Thread(target=put, args=(i,)) for i in range(40)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(store.query(scope(), query(limit=200))) == 40


def test_concurrent_transitions_have_one_winner(store):
    store.put(scope(), memory())
    outcomes = []

    def move():
        try:
            store.transition(scope(), "memory-1", expected_status="verified",
                             new_status="contradicted")
            outcomes.append("won")
        except Conflict:
            outcomes.append("conflict")
    threads = [threading.Thread(target=move) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["conflict"] * 15 + ["won"]
