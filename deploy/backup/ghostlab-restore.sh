#!/usr/bin/env bash
# Restore a backup prefix into a storage root. Verifies every file against the
# sha256 manifest BEFORE touching the live store, restores to a fresh
# directory, runs integrity_check on both databases, and only then swaps the
# restored root into place (with the API stopped).
#
# Usage:
#   ghostlab-restore.sh <backup-stamp> <target-root>   # e.g. 20260101T000000Z /var/lib/ghostlab
#   BACKUP_BUCKET=... ghostlab-restore.sh latest <target-root>
#
# Restoring discards everything after the backup: an approval consumed after
# the backup becomes unconsumed in the restored store. Check for runs
# completed since the backup before re-approving anything.
set -euo pipefail

STAMP="${1:?usage: ghostlab-restore.sh <stamp|latest> <target-root>}"
TARGET="${2:?usage: ghostlab-restore.sh <stamp|latest> <target-root>}"
BACKUP_BUCKET="${BACKUP_BUCKET:?set BACKUP_BUCKET (from /etc/ghostlab/host.conf)}"
AWS_REGION="${AWS_REGION:-ap-south-1}"

WORK="$(mktemp -d /var/backups/ghostlab/restore.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

log() { echo "[restore] $*"; }

if [ "$STAMP" = "latest" ]; then
  STAMP="$(aws s3 ls "s3://$BACKUP_BUCKET/" --region "$AWS_REGION" | awk '{print $2}' | grep -E '^[0-9]{8}T[0-9]{6}Z/$' | sort | tail -n 1 | tr -d '/')"
  [ -n "$STAMP" ] || {
    echo "no backups in s3://$BACKUP_BUCKET/" >&2
    exit 1
  }
fi

log "fetching s3://$BACKUP_BUCKET/$STAMP/ to $WORK"
aws s3 sync "s3://$BACKUP_BUCKET/$STAMP/" "$WORK/" --region "$AWS_REGION" --only-show-errors

log "verifying manifest"
(
  cd "$WORK"
  sha256sum -c MANIFEST.sha256
)

RESTORED="$WORK/restored"
mkdir -p "$RESTORED"
cp -a "$WORK/ghostlab.sqlite" "$RESTORED/ghostlab.sqlite" 2>/dev/null || true
cp -a "$WORK/graph-checkpoints.sqlite" "$RESTORED/graph-checkpoints.sqlite" 2>/dev/null || true
[ -d "$WORK/artifacts" ] && cp -a "$WORK/artifacts" "$RESTORED/artifacts" || mkdir -p "$RESTORED/artifacts"

log "integrity_check on restored databases"
for name in ghostlab.sqlite graph-checkpoints.sqlite; do
  if [ -f "$RESTORED/$name" ]; then
    result="$(sqlite3 "$RESTORED/$name" 'PRAGMA integrity_check;')"
    [ "$result" = "ok" ] || {
      echo "integrity_check failed on $name: $result" >&2
      exit 1
    }
  fi
done

if systemctl is-active --quiet ghostlab-api.service 2>/dev/null; then
  echo "refused: stop ghostlab-api.service before swapping the store" >&2
  exit 2
fi

log "swapping $RESTORED into $TARGET"
mkdir -p "$(dirname "$TARGET")"
if [ -e "$TARGET" ]; then
  mv "$TARGET" "$TARGET.pre-restore-$(date -u +%Y%m%dT%H%M%SZ)"
fi
mv "$RESTORED" "$TARGET"
chown -R ghostlab:ghostlab "$TARGET"

log "done. Start the API and re-run the drill checks in docs/deployment-mumbai.md."
