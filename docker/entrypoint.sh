#!/usr/bin/env sh
set -eu

cd /app
# Load .env if present (docker compose already does, but keep safe)
if [ -f "/app/.env" ]; then
  # shellcheck disable=SC2046
  export $(grep -v '^#' /app/.env | xargs -d '\n' 2>/dev/null || true)
fi

exec python -u /app/main.py
