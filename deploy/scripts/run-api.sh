#!/usr/bin/env bash
# ExecStart wrapper for ghostlab-api.service. Builds the server command from the
# operator-owned env file; the bind address is hardcoded to loopback and is NOT
# overridable from the environment.
#
# Expected env (from /etc/ghostlab/env/api.env, mode 0600):
#   GHOSTLAB_LIVE=1 plus GHOSTLAB_LIVE_MODEL / GHOSTLAB_LIVE_MAX_CALLS /
#   GHOSTLAB_LIVE_COMPAT_REPORT / GHOSTLAB_CONFIRM_PAID_CALLS=1 for live mode;
#   GHOSTLAB_ALLOW_SERVER_RETENTION=1 only when explicitly approved.
#   MODEL_API_KEY travels by env only and is never echoed here.
set -euo pipefail

BIND_HOST="127.0.0.1" # NEVER from env. The proxy is the only public listener.
PORT="${API_PORT:-8787}"
APP_ROOT="${APP_ROOT:-/srv/ghostlab/app}"
VENV_BIN="$APP_ROOT/.venv/bin"

args=(--host "$BIND_HOST" --port "$PORT")

if [ "${GHOSTLAB_LIVE:-0}" = "1" ]; then
  : "${GHOSTLAB_LIVE_MODEL:?GHOSTLAB_LIVE=1 needs GHOSTLAB_LIVE_MODEL}"
  : "${GHOSTLAB_LIVE_MAX_CALLS:?GHOSTLAB_LIVE=1 needs GHOSTLAB_LIVE_MAX_CALLS}"
  : "${GHOSTLAB_LIVE_COMPAT_REPORT:?GHOSTLAB_LIVE=1 needs GHOSTLAB_LIVE_COMPAT_REPORT}"
  [ "${GHOSTLAB_CONFIRM_PAID_CALLS:-0}" = "1" ] || {
    echo "refused: GHOSTLAB_LIVE=1 needs GHOSTLAB_CONFIRM_PAID_CALLS=1" >&2
    exit 2
  }
  # The server re-checks the model id against its qualified gate (live.py) and
  # refuses invented ids; this wrapper passes the value through, never a default.
  args+=(--live --live-model "$GHOSTLAB_LIVE_MODEL"
    --live-max-calls "$GHOSTLAB_LIVE_MAX_CALLS"
    --live-compat-report "$GHOSTLAB_LIVE_COMPAT_REPORT"
    --confirm-paid-calls)
  if [ "${GHOSTLAB_ALLOW_SERVER_RETENTION:-0}" = "1" ]; then
    args+=(--allow-server-retention)
  fi
fi

exec "$VENV_BIN/python" -m ghostlab.api "${args[@]}"
