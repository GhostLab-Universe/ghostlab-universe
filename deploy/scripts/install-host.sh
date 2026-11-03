#!/usr/bin/env bash
# Operator installer. Run on the host from a pinned repo checkout AFTER
# user-data has finished machine prep:
#
#   cd /srv/ghostlab/app && git fetch && git checkout <pinned-commit>
#   sudo deploy/scripts/install-host.sh
#
# Domain values come from /etc/ghostlab/host.conf (written by user-data from
# the Terraform domain_name/acme_email vars), never from the caller env.
#
# What it does: builds the app venv (uv, python 3.13), builds the production
# web bundle, installs the Caddyfile for domain or localhost mode, installs
# the systemd units and backup scripts, and enables everything. It never
# writes secrets: /etc/ghostlab/env/api.env stays operator-owned (0600).
set -euo pipefail

[ "$(id -u)" = "0" ] || {
  echo "run as root (sudo)" >&2
  exit 2
}
[ -f /etc/ghostlab/host.conf ] || {
  echo "missing /etc/ghostlab/host.conf: user-data has not finished" >&2
  exit 2
}
# shellcheck disable=SC1091
. /etc/ghostlab/host.conf

REPO="${APP_ROOT:?host.conf must set APP_ROOT}"
DOMAIN="${DOMAIN_NAME:-}"
ACME="${ACME_EMAIL:-}"

[ -f "$REPO/pyproject.toml" ] || {
  echo "$REPO is not a ghostlab checkout" >&2
  exit 2
}

UV_BIN="/root/.local/bin/uv"
[ -x "$UV_BIN" ] || {
  echo "uv not installed: user-data has not finished" >&2
  exit 2
}

log() { echo "[install-host] $*"; }

log "app venv (python 3.13, locked deps)"
cd "$REPO"
"$UV_BIN" python pin 3.13 2>/dev/null || true
"$UV_BIN" sync --frozen 2>/dev/null || "$UV_BIN" sync
chown -R ghostlab:ghostlab "$REPO/.venv"

log "production web build (same-origin bundle, no dev server)"
cd "$REPO/web"
npm ci --no-audit --no-fund
npm run build
rm -rf "$WEB_ROOT"
mkdir -p "$(dirname "$WEB_ROOT")"
cp -a "$REPO/web/dist" "$WEB_ROOT"
chown -R ghostlab:ghostlab "$WEB_ROOT"
test -f "$WEB_ROOT/index.html" || {
  echo "web build produced no index.html" >&2
  exit 1
}

log "caddy config"
if [ -n "$DOMAIN" ]; then
  [ -n "$ACME" ] || {
    echo "DOMAIN_NAME is set but ACME_EMAIL is empty" >&2
    exit 2
  }
  cp "$REPO/deploy/caddy/Caddyfile.domain" /etc/caddy/Caddyfile
  # Caddy {$VAR} placeholders adapt from the process environment, and the
  # distro caddy unit sources no ghostlab env file, so substitute the values
  # into the installed Caddyfile: validate and the daemon then agree.
  domain_esc="$(printf '%s' "$DOMAIN" | sed 's/[&|\/]/\\&/g')"
  acme_esc="$(printf '%s' "$ACME" | sed 's/[&|\/]/\\&/g')"
  sed -i "s|{\$DOMAIN_NAME}|$domain_esc|g; s|{\$ACME_EMAIL}|$acme_esc|g" /etc/caddy/Caddyfile
  grep -q "{\\$" /etc/caddy/Caddyfile && {
    echo "unsubstituted placeholder left in /etc/caddy/Caddyfile" >&2
    exit 1
  } || true
  log "domain mode for $DOMAIN"
else
  cp "$REPO/deploy/caddy/Caddyfile.localhost" /etc/caddy/Caddyfile
  rm -f /etc/caddy/ghostlab.env
  log "IP-only mode: localhost-only listener (see Caddyfile.localhost)"
fi
caddy fmt --overwrite /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
systemctl reload-or-restart caddy

log "scripts to $REPO/bin"
install -m 0755 -d "$REPO/bin"
install -o ghostlab -g ghostlab -m 0755 "$REPO/deploy/scripts/run-api.sh" "$REPO/bin/run-api.sh"
install -o ghostlab -g ghostlab -m 0755 "$REPO/deploy/backup/ghostlab-backup.sh" "$REPO/bin/ghostlab-backup.sh"
install -o ghostlab -g ghostlab -m 0755 "$REPO/deploy/backup/ghostlab-restore.sh" "$REPO/bin/ghostlab-restore.sh"

log "systemd units"
cp "$REPO/deploy/systemd/ghostlab-api.service" /etc/systemd/system/
cp "$REPO/deploy/systemd/ghostlab-backup.service" /etc/systemd/system/
cp "$REPO/deploy/systemd/ghostlab-backup.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now ghostlab-backup.timer
systemctl enable ghostlab-api.service # started explicitly once api.env exists

log "storage root"
install -o ghostlab -g ghostlab -m 0750 -d "$STORAGE_ROOT" "$STORAGE_ROOT/artifacts"

log "done. Next: write /etc/ghostlab/env/api.env (0600), then: systemctl start ghostlab-api.service"
