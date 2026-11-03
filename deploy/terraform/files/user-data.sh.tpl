#!/usr/bin/env bash
# Cloud-init user data for the Mumbai restricted-eval host.
# Machine prep ONLY: packages, users, directories, host config. No secrets, no
# app code, no model calls. The operator installs the app from a pinned commit
# afterwards (see docs/deployment-mumbai.md, "Install the app").
#
# Rendered by Terraform with: aws_region, backup_bucket, domain_name,
# acme_email, project. $$ escaping is for templatefile, not the shell.
set -euo pipefail

AWS_REGION="${aws_region}"
BACKUP_BUCKET="${backup_bucket}"
DOMAIN_NAME="${domain_name}"
ACME_EMAIL="${acme_email}"
PROJECT="${project}"

export DEBIAN_FRONTEND=noninteractive

log() { echo "[user-data] $*"; }

log "base packages"
apt-get update
apt-get install -y --no-install-recommends \
  ca-certificates curl debian-keyring debian-archive-keyring \
  apt-transport-https gnupg sqlite3 unzip jq awscli

log "caddy (official repo)"
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
  | gpg --dearmor -o /etc/apt/keyrings/caddy-stable.gpg
chmod 0644 /etc/apt/keyrings/caddy-stable.gpg
curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
  -o /etc/apt/sources.list.d/caddy-stable.list
apt-get update
apt-get install -y caddy

log "node 24 (nodesource, for the production web build)"
curl -fsSL https://deb.nodesource.com/setup_24.x | bash -
apt-get install -y nodejs

log "uv + cpython 3.13 (repo requires >=3.13,<3.14; noble ships 3.12)"
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="/root/.local/bin:$${PATH}"
/root/.local/bin/uv python install 3.13

log "ghostlab user and layout"
id ghostlab >/dev/null 2>&1 || useradd -r -m -s /bin/bash ghostlab
install -o ghostlab -g ghostlab -m 0755 -d \
  /srv/ghostlab/app /srv/ghostlab/web /var/lib/ghostlab /var/backups/ghostlab
install -m 0755 -d /etc/ghostlab
install -o ghostlab -g ghostlab -m 0750 -d /etc/ghostlab/env

# Host facts only. api.env (keys, live flags) is created by the operator with
# mode 0600 and is never written here or committed anywhere.
cat >/etc/ghostlab/host.conf <<EOF
# Written by user-data. Non-secret host facts; safe to print.
AWS_REGION="$${AWS_REGION}"
BACKUP_BUCKET="$${BACKUP_BUCKET}"
DOMAIN_NAME="$${DOMAIN_NAME}"
ACME_EMAIL="$${ACME_EMAIL}"
PROJECT="$${PROJECT}"
STORAGE_ROOT="/var/lib/ghostlab"
API_HOST="127.0.0.1"
API_PORT="8787"
WEB_ROOT="/srv/ghostlab/web/dist"
APP_ROOT="/srv/ghostlab/app"
EOF
chmod 0644 /etc/ghostlab/host.conf

log "host.conf written; app install is an operator step (see runbook)"
