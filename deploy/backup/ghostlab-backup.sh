#!/usr/bin/env bash
# Hourly SQLite-safe backup. Snapshots the live store with SQLite's online
# backup API (never a raw file copy: WAL mode keeps recent writes in a side
# file), copies the artifact tree, writes a sha256 manifest, and ships the
# whole prefix to the versioned S3 bucket. Run by ghostlab-backup.timer.
#
# Layout mirrors StoragePaths (src/ghostlab/storage/database.py):
#   $STORAGE_ROOT/ghostlab.sqlite, graph-checkpoints.sqlite, artifacts/
set -euo pipefail

STORAGE_ROOT="${STORAGE_ROOT:-/var/lib/ghostlab}"
STAGE_ROOT="${STAGE_ROOT:-/var/backups/ghostlab}"
BACKUP_BUCKET="${BACKUP_BUCKET:?set BACKUP_BUCKET (from /etc/ghostlab/host.conf)}"
AWS_REGION="${AWS_REGION:-ap-south-1}"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
STAGE="$STAGE_ROOT/$STAMP"

log() { echo "[backup $STAMP] $*"; }

mkdir -p "$STAGE"
log "snapshotting $STORAGE_ROOT"

for name in ghostlab.sqlite graph-checkpoints.sqlite; do
  if [ -f "$STORAGE_ROOT/$name" ]; then
    sqlite3 "$STORAGE_ROOT/$name" ".backup main '$STAGE/$name'"
  else
    log "note: $name absent, skipping (fresh store)"
  fi
done

if [ -d "$STORAGE_ROOT/artifacts" ]; then
  mkdir -p "$STAGE/artifacts"
  cp -a "$STORAGE_ROOT/artifacts/." "$STAGE/artifacts/"
else
  log "note: artifacts/ absent, skipping"
fi

log "writing manifest"
(
  cd "$STAGE"
  find . -type f ! -name MANIFEST.sha256 -print0 | sort -z | xargs -0 sha256sum >MANIFEST.sha256
)

log "uploading to s3://$BACKUP_BUCKET/$STAMP/"
aws s3 sync "$STAGE/" "s3://$BACKUP_BUCKET/$STAMP/" --region "$AWS_REGION" --only-show-errors

log "pruning local stage older than 48h"
find "$STAGE_ROOT" -mindepth 1 -maxdepth 1 -type d -mmin +2880 -print0 | xargs -0 -r rm -rf

log "done: s3://$BACKUP_BUCKET/$STAMP/ ($(wc -l <"$STAGE/MANIFEST.sha256") files)"
