"""SqliteApprovalStore: digest-bound decisions and single-use consumption."""

import threading

import pytest
from m04_fixtures import approval, database, decision

from ghostlab.storage import ApprovalStore, Conflict, NotFound
from ghostlab.storage.approval_store import SqliteApprovalStore

P = "project-local"
DIGEST = approval().action_digest


@pytest.fixture
def store(tmp_path):
    store = SqliteApprovalStore(database(tmp_path))
    store.create(approval())
    return store


def test_satisfies_published_interface(store):
    assert isinstance(store, ApprovalStore)


def test_create_rules(store):
    with pytest.raises(Conflict):
        store.create(approval())
    with pytest.raises(Conflict):
        store.create(approval("approval-2", status="approved"))
    store.create(approval(project_id="project-other"))  # same id, other project


def test_other_project_cannot_see_or_act(store):
    for call in (lambda: store.get("project-other", "approval-1"),
                 lambda: store.get_decision("project-other", "approval-1"),
                 lambda: store.record_decision("project-other", decision(),
                                               new_status="approved"),
                 lambda: store.consume("project-other", "approval-1", DIGEST)):
        with pytest.raises(NotFound):
            call()


def test_full_lifecycle_and_restart(store):
    assert store.get_decision(P, "approval-1") is None
    assert store.record_decision(P, decision(), new_status="approved").status == "approved"
    restarted = SqliteApprovalStore(store.path)
    assert restarted.get_decision(P, "approval-1") == decision()
    assert restarted.consume(P, "approval-1", DIGEST).status == "consumed"
    with pytest.raises(Conflict):
        restarted.consume(P, "approval-1", DIGEST)  # replay


@pytest.mark.parametrize("bad", [
    lambda s: s.record_decision(P, decision(action_digest="0" * 64), new_status="approved"),
    lambda s: s.record_decision(P, decision(), new_status="rejected"),
    lambda s: s.consume(P, "approval-1", DIGEST),  # still pending
], ids=["changed-action", "status-mismatch", "consume-pending"])
def test_invalid_transitions_change_nothing(store, bad):
    with pytest.raises(Conflict):
        bad(store)
    assert store.get(P, "approval-1").status == "pending"
    assert store.get_decision(P, "approval-1") is None


def test_decision_is_final(store):
    store.record_decision(P, decision(decision="reject"), new_status="rejected")
    with pytest.raises(Conflict):
        store.record_decision(P, decision(), new_status="approved")
    with pytest.raises(Conflict):
        store.consume(P, "approval-1", DIGEST)


def test_consume_checks_digest_and_expiry_blocks_use(store):
    store.record_decision(P, decision(), new_status="approved")
    with pytest.raises(Conflict):
        store.consume(P, "approval-1", "f" * 64)
    store.create(approval("approval-2"))
    store.mark_expired(P, "approval-2")
    with pytest.raises(Conflict):
        store.record_decision(P, decision("approval-2"), new_status="approved")
    with pytest.raises(Conflict):
        store.mark_expired(P, "approval-2")


def test_concurrent_consumers_exactly_one_succeeds(store):
    store.record_decision(P, decision(), new_status="approved")
    outcomes = []

    def consume():
        try:
            store.consume(P, "approval-1", DIGEST)
            outcomes.append("consumed")
        except Conflict:
            outcomes.append("conflict")
    threads = [threading.Thread(target=consume) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["conflict"] * 15 + ["consumed"]


def test_concurrent_deciders_exactly_one_succeeds(store):
    outcomes = []

    def decide(kind):
        try:
            store.record_decision(P, decision(decision=kind),
                                  new_status="approved" if kind == "approve" else "rejected")
            outcomes.append(kind)
        except Conflict:
            outcomes.append("conflict")
    threads = [threading.Thread(target=decide, args=(k,)) for k in ["approve", "reject"] * 8]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count("conflict") == 15
    assert store.get(P, "approval-1").status in {"approved", "rejected"}
