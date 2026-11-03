"""Local restore-drill proof: backup/restore round-trip on scratch SQLite files.

Same method as deploy/backup/restore-drill.sh (online .backup API, artifacts
copy, sha256 manifest, destroy, restore, compare) but in portable Python so it
runs anywhere with the repo interpreter:

    PYTHONPATH=src python -m pytest deploy/tests/ -q

No cloud, no model, no secrets.
"""

import hashlib
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

from ghostlab.storage.database import StoragePaths


def _seed(path: Path, ddl: str) -> None:
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA journal_mode=WAL;")
        db.executescript(ddl)


def _dump(path: Path) -> str:
    with closing(sqlite3.connect(path)) as db:
        return "\n".join(db.iterdump())


def _check(path: Path) -> str:
    with closing(sqlite3.connect(path)) as db:
        return db.execute("PRAGMA integrity_check;").fetchone()[0]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_backup_restore_round_trip_is_byte_identical(tmp_path):
    store = tmp_path / "store"
    snap = tmp_path / "snap"
    restored = tmp_path / "restored"
    for d in (store, snap, restored):
        d.mkdir()

    # Scratch store in the StoragePaths layout.
    paths = StoragePaths(store)
    paths.artifacts.mkdir()
    _seed(paths.app_db,
          "CREATE TABLE memory (id TEXT PRIMARY KEY, body TEXT NOT NULL);"
          "INSERT INTO memory VALUES ('m-1', 'drill approval'), ('m-2', 'drill evidence');"
          "CREATE TABLE approvals (id TEXT PRIMARY KEY, status TEXT NOT NULL);"
          "INSERT INTO approvals VALUES ('a-1', 'pending');")
    graph = StoragePaths(store).graph_db
    _seed(graph,
          "CREATE TABLE checkpoints (id TEXT PRIMARY KEY, payload BLOB NOT NULL);"
          "INSERT INTO checkpoints VALUES ('c-1', x'0102030405060708090a');")
    (paths.artifacts / "drill.txt").write_text("drill artifact bytes", encoding="utf-8")

    before_app, before_graph = _dump(paths.app_db), _dump(graph)

    # Snapshot exactly like ghostlab-backup.sh: online backup, never raw copy.
    for name in (paths.app_db.name, graph.name):
        with closing(sqlite3.connect(store / name)) as live, \
                closing(sqlite3.connect(snap / name)) as copy:
            live.backup(copy)
    shutil.copytree(paths.artifacts, snap / "artifacts")
    manifest = {p.name: _sha256(p) for p in
                (snap / paths.app_db.name, snap / graph.name, snap / "artifacts" / "drill.txt")}

    # Lose the original.
    shutil.rmtree(store)

    # Verify the manifest, then restore to a fresh root.
    for name, digest in manifest.items():
        candidates = list(snap.rglob(name))
        assert len(candidates) == 1
        assert _sha256(candidates[0]) == digest
    for name in (paths.app_db.name, graph.name):
        shutil.copy2(snap / name, restored / name)
    shutil.copytree(snap / "artifacts", restored / "artifacts")

    assert _check(restored / paths.app_db.name) == "ok"
    assert _check(restored / graph.name) == "ok"
    assert _dump(restored / paths.app_db.name) == before_app
    assert _dump(restored / graph.name) == before_graph
    assert (restored / "artifacts" / "drill.txt").read_text(encoding="utf-8") == "drill artifact bytes"

    # The restored root is a usable StoragePaths root again.
    revived = StoragePaths(restored)
    assert revived.app_db.exists() and revived.graph_db.exists()
    assert revived.artifacts.is_dir()
