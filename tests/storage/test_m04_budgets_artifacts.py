"""Budget reservations, artifact integrity and the graph checkpoint saver."""

import sqlite3
import threading
from contextlib import closing
from typing import TypedDict

import pytest
from m04_fixtures import database, reservation

from ghostlab.contracts import Provenance
from ghostlab.storage import Conflict, IntegrityError, NotFound
from ghostlab.storage.artifacts import ArtifactStore, ArtifactTooLarge, FileArtifactStore
from ghostlab.storage.budgets import BudgetExceeded, BudgetLedger, SqliteBudgetLedger
from ghostlab.storage.checkpoints import graph_checkpointer
from ghostlab.storage.database import StoragePaths

C = "campaign-1"


@pytest.fixture
def ledger(tmp_path):
    return SqliteBudgetLedger(database(tmp_path))


def test_ledger_interface(ledger):
    assert isinstance(ledger, BudgetLedger)


def test_reserve_reconcile_release_accounting(ledger):
    ledger.reserve(reservation("r1", 3), limit=5)
    with pytest.raises(BudgetExceeded):
        ledger.reserve(reservation("r2", 3), limit=5)
    ledger.reconcile("r1", 1)                        # actual below reservation frees room
    ledger.reserve(reservation("r2", 3), limit=5)
    assert ledger.committed(C, "model_calls") == (4, 0)
    ledger.reconcile("r2", None)                      # unknown actual keeps full amount
    assert ledger.committed(C, "model_calls") == (4, 1)
    ledger.reserve(reservation("r3", 1), limit=5)
    ledger.release("r3")
    assert ledger.committed(C, "model_calls") == (4, 1)
    with pytest.raises(Conflict):
        ledger.release("r3")
    with pytest.raises(Conflict):
        ledger.reserve(reservation("r1", 1), limit=99)
    with pytest.raises(NotFound):
        ledger.reconcile("missing", 1)
    assert SqliteBudgetLedger(ledger.path).committed(C, "model_calls") == (4, 1)  # restart


def test_overspend_is_recorded_not_hidden(ledger):
    ledger.reserve(reservation("r1", 2), limit=5)
    ledger.reconcile("r1", 7)
    assert ledger.committed(C, "model_calls") == (7, 0)
    with pytest.raises(BudgetExceeded):
        ledger.reserve(reservation("r2", 1), limit=5)


def test_concurrent_reservations_never_exceed_ceiling(ledger):
    won = []

    def reserve(index):
        try:
            ledger.reserve(reservation(f"r{index}", 1), limit=5)
            won.append(index)
        except BudgetExceeded:
            pass
    threads = [threading.Thread(target=reserve, args=(i,)) for i in range(24)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(won) == 5
    assert ledger.committed(C, "model_calls") == (5, 0)


# --- artifacts -------------------------------------------------------------------

PROV = Provenance.model_validate({"source_kind": "verifier", "source_ref": None,
                                  "observed_at": "2026-10-29T10:00:05Z", "trust": "verified"})


@pytest.fixture
def artifacts(tmp_path):
    paths = StoragePaths(tmp_path)
    return FileArtifactStore(database(tmp_path), paths.artifacts, max_bytes=1024)


def put(store, data=b'{"trace": []}', artifact_id="artifact-1"):
    return store.put(data, artifact_id=artifact_id, media_type="application/json",
                     provenance=PROV, created_at="2026-10-29T10:00:05Z")


def test_artifact_roundtrip_and_dedup(artifacts):
    assert isinstance(artifacts, ArtifactStore)
    ref = put(artifacts)
    assert artifacts.read(ref) == b'{"trace": []}'
    assert artifacts.ref("artifact-1") == ref
    again = put(artifacts, artifact_id="artifact-2")
    assert again.sha256 == ref.sha256
    assert len(list((artifacts.root / "sha256").rglob("*"))) == 2  # one dir, one blob
    with pytest.raises(Conflict):
        put(artifacts)


def test_corrupt_missing_and_mismatched_bytes_fail(artifacts):
    ref = put(artifacts)
    blob = artifacts.root / "sha256" / ref.sha256[:2] / ref.sha256
    blob.write_bytes(b'{"trace": [1]}')
    with pytest.raises(IntegrityError):
        artifacts.read(ref)
    blob.unlink()
    with pytest.raises(IntegrityError):
        artifacts.read(ref)
    forged = ref.model_validate({**ref.model_dump(mode="json"), "sha256": "0" * 64})
    with pytest.raises(IntegrityError):
        artifacts.read(forged)


def test_artifact_size_bounds(artifacts):
    with pytest.raises(ArtifactTooLarge):
        put(artifacts, b"x" * 1025)
    ref = put(artifacts, b"x" * 100)
    with pytest.raises(ArtifactTooLarge):
        artifacts.read(ref, max_bytes=10)
    with pytest.raises(NotFound):
        artifacts.ref("missing")


# --- graph checkpoints -----------------------------------------------------------

class State(TypedDict):
    campaign_id: str
    completed_experiments: list[str]


def test_graph_checkpoint_survives_restart_in_its_own_file(tmp_path):
    from langgraph.graph import END, START, StateGraph

    paths = StoragePaths(tmp_path)
    app_db = database(tmp_path)
    graph = StateGraph(State)
    graph.add_node("record", lambda s: {"completed_experiments":
                                        [*s["completed_experiments"], "experiment-1"]})
    graph.add_edge(START, "record")
    graph.add_edge("record", END)
    config = {"configurable": {"thread_id": "campaign-1"}}
    with graph_checkpointer(paths.graph_db, app_db=app_db) as saver:
        graph.compile(checkpointer=saver).invoke(
            {"campaign_id": "campaign-1", "completed_experiments": []}, config)
    with graph_checkpointer(paths.graph_db, app_db=app_db) as saver:
        state = graph.compile(checkpointer=saver).get_state(config).values
    assert state["completed_experiments"] == ["experiment-1"]
    with closing(sqlite3.connect(app_db)) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    assert not {"checkpoints", "writes"} & tables
    with pytest.raises(ValueError):
        with graph_checkpointer(app_db, app_db=app_db):
            pass
