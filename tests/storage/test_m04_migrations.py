"""Forward-only checksummed migrations preserve existing data."""

import shutil

import pytest
from m04_fixtures import memory, query_scope_free_memory_query, scope

from ghostlab.storage import IntegrityError
from ghostlab.storage.migrations import MIGRATIONS_DIR, available, current_version, migrate
from ghostlab.storage.memory_store import SqliteMemoryStore
from ghostlab.storage.sqlite import enable_wal


def test_fresh_database_reaches_latest_and_rerun_is_noop(tmp_path):
    path = tmp_path / "db.sqlite"
    enable_wal(path)
    latest = len(available())
    assert migrate(path) == latest == current_version(path)
    assert migrate(path) == latest


def test_upgrade_preserves_existing_records(tmp_path):
    path = tmp_path / "db.sqlite"
    assert migrate(path, target=1) == 1
    store = SqliteMemoryStore(path)
    store.put(scope(), memory())
    assert migrate(path) == len(available())
    assert store.get(scope(), "memory-1") == memory()
    assert [r.record_id for r in store.query(scope(), query_scope_free_memory_query())] == [
        "memory-1"]


def test_modified_applied_migration_is_rejected(tmp_path):
    directory = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS_DIR, directory)
    path = tmp_path / "db.sqlite"
    migrate(path, directory)
    first = sorted(directory.glob("0001_*.sql"))[0]
    first.write_text(first.read_text(encoding="utf-8") + "\n-- edited\n", encoding="utf-8")
    with pytest.raises(IntegrityError, match="modified"):
        migrate(path, directory)


def test_database_newer_than_code_is_rejected(tmp_path):
    directory = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS_DIR, directory)
    number = len(available()) + 1
    (directory / f"{number:04d}_future.sql").write_text("CREATE TABLE future (x);\n",
                                                        encoding="utf-8")
    path = tmp_path / "db.sqlite"
    migrate(path, directory)
    with pytest.raises(IntegrityError, match="newer"):
        migrate(path)


def test_failed_migration_rolls_back(tmp_path):
    directory = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS_DIR, directory)
    number = len(available()) + 1
    (directory / f"{number:04d}_broken.sql").write_text(
        "CREATE TABLE partial (x);\nTHIS IS NOT SQL;\n", encoding="utf-8")
    path = tmp_path / "db.sqlite"
    with pytest.raises(Exception):
        migrate(path, directory)
    assert current_version(path) == number - 1
    migrate(path)  # the real set still applies cleanly afterwards


@pytest.mark.parametrize("name", ["0003_gap.sql", "1_short.sql", "0001_Upper.sql"])
def test_bad_migration_names_rejected(tmp_path, name):
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / name).write_text("SELECT 1;\n", encoding="utf-8")
    with pytest.raises(IntegrityError):
        available(directory)
