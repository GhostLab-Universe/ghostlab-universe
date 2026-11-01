# Recovery drill

Proves a local store can be lost and restored with a paused campaign intact. Run it for
every release, and after any change to storage, migrations or checkpoints.

The automated version is the evaluation case `ops.recovery_drill`:

```sh
python -m evals.run --out <scratch-dir> --only ops.recovery_drill
```

`--out` must be a new or empty directory. `--only` runs one case, so the gate also lists
the others as missing. Read that case's entry in `<scratch-dir>/report.json`, or run the
full suite, which has no such noise.

## What it does

1. Runs a refund campaign with mocked inference until it pauses for approval.
2. Backs up both SQLite files with SQLite's online backup API, and copies the artifact
   directory.
3. Deletes the original store.
4. Restores the backup to a new location and opens it with new connections.
5. Resumes with no decision: the campaign must stay paused.
6. Approves, resumes, and requires `repair_verified`.
7. Exports and replays the capsule from the restored store, with no provider request.

| Expectation | Result required |
| --- | --- |
| `the_paused_approval_survives_a_restore` | Still paused after the restore |
| `the_restored_store_completes_the_campaign` | `repair_verified` |
| `restored_evidence_still_replays` | `reproduced: true` |
| `restore_makes_no_model_call` | 0 provider requests after the restore |

## Backing up a real local store

Stop the coordinator first. The store's location is whatever `StoragePaths` root you use.

```python
import shutil, sqlite3
from contextlib import closing
from pathlib import Path

source, target = Path("<store>"), Path("<backup>")
target.mkdir(parents=True)
for name in ("ghostlab.sqlite", "graph-checkpoints.sqlite"):
    with closing(sqlite3.connect(source / name)) as live, \
            closing(sqlite3.connect(target / name)) as copy:
        live.backup(copy)
shutil.copytree(source / "artifacts", target / "artifacts")
```

Don't copy live `.sqlite` files directly: WAL mode keeps recent writes in a side file.
Never share a store between developers or tracks. Keep backups as private as the store,
because memory records and approvals are in them.

## Limits

- This covers one local, stopped coordinator. Hosted Postgres recovery isn't defined and
  must be drilled separately when M07 adds a hosted service.
- Graph checkpoints don't snapshot worlds. Worlds are rebuilt from the pinned fixture, and
  this holds only because the worlds are synthetic and in-process.
- Restoring discards everything after the backup. An approval consumed after the backup
  becomes unconsumed in the restored store, so check for runs completed since the backup
  before re-approving anything.
- Telemetry isn't in the backup. It's operational, and no verdict depends on it.
