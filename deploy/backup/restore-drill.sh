#!/usr/bin/env bash
# Local restore drill. Proves the backup/restore round-trip WITHOUT any cloud:
# builds a scratch store in the StoragePaths layout, snapshots it exactly the
# way ghostlab-backup.sh does (sqlite3 .backup + artifacts copy + sha256
# manifest), destroys the original, restores from the snapshot, and requires
# byte-identical database dumps plus integrity_check=ok.
#
# Usage: restore-drill.sh [scratch-dir]   (default: a fresh mktemp dir)
# Exit 0 with "DRILL: PASS" on success, non-zero with "DRILL: FAIL" otherwise.
set -euo pipefail

SCRATCH="${1:-$(mktemp -d /tmp/ghostlab-drill.XXXXXX)}"
STORE="$SCRATCH/store"
SNAP="$SCRATCH/snap"
RESTORED="$SCRATCH/restored"

fail() {
  echo "DRILL: FAIL: $*" >&2
  exit 1
}

mkdir -p "$STORE/artifacts" "$SNAP" "$RESTORED"

echo "DRILL: building scratch store in $STORE"
sqlite3 "$STORE/ghostlab.sqlite" <<'SQL'
PRAGMA journal_mode=WAL;
CREATE TABLE memory (id TEXT PRIMARY KEY, body TEXT NOT NULL);
INSERT INTO memory VALUES ('m-1', 'drill approval record'), ('m-2', 'drill evidence record');
CREATE TABLE approvals (id TEXT PRIMARY KEY, status TEXT NOT NULL);
INSERT INTO approvals VALUES ('a-1', 'pending');
SQL
sqlite3 "$STORE/graph-checkpoints.sqlite" <<'SQL'
PRAGMA journal_mode=WAL;
CREATE TABLE checkpoints (id TEXT PRIMARY KEY, payload BLOB NOT NULL);
INSERT INTO checkpoints VALUES ('c-1', x'0102030405060708090a');
SQL
echo "drill artifact bytes" >"$STORE/artifacts/drill.txt"

before_app="$(sqlite3 "$STORE/ghostlab.sqlite" '.dump')"
before_graph="$(sqlite3 "$STORE/graph-checkpoints.sqlite" '.dump')"

echo "DRILL: snapshotting (same method as ghostlab-backup.sh)"
for name in ghostlab.sqlite graph-checkpoints.sqlite; do
  sqlite3 "$STORE/$name" ".backup main '$SNAP/$name'"
done
mkdir -p "$SNAP/artifacts"
cp -a "$STORE/artifacts/." "$SNAP/artifacts/"
(
  cd "$SNAP"
  find . -type f ! -name MANIFEST.sha256 -print0 | sort -z | xargs -0 sha256sum >MANIFEST.sha256
)

echo "DRILL: destroying the original store"
rm -rf "$STORE"

echo "DRILL: verifying manifest and restoring"
(cd "$SNAP" && sha256sum -c MANIFEST.sha256) >/dev/null || fail "manifest mismatch"
cp -a "$SNAP/ghostlab.sqlite" "$SNAP/graph-checkpoints.sqlite" "$RESTORED/"
cp -a "$SNAP/artifacts" "$RESTORED/artifacts"

for name in ghostlab.sqlite graph-checkpoints.sqlite; do
  [ "$(sqlite3 "$RESTORED/$name" 'PRAGMA integrity_check;')" = "ok" ] || fail "integrity_check on $name"
done

[ "$(sqlite3 "$RESTORED/ghostlab.sqlite" '.dump')" = "$before_app" ] || fail "app db dump differs"
[ "$(sqlite3 "$RESTORED/graph-checkpoints.sqlite" '.dump')" = "$before_graph" ] || fail "graph db dump differs"
[ "$(cat "$RESTORED/artifacts/drill.txt")" = "drill artifact bytes" ] || fail "artifact differs"

echo "DRILL: PASS: round-trip byte-identical, integrity_check ok on both databases"
