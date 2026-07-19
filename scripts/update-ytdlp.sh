#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="${ROOT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SERVICE_KEY="${SERVICE_KEY:-videodownloaderbot}"
COMPOSE_PROJECT="${COMPOSE_PROJECT:-videodownloaderbot}"
IMAGE_NAME="${IMAGE_NAME:-videodownloaderbot:local}"
ROLLBACK_IMAGE="${IMAGE_NAME%:*}:rollback"
LOCK_FILE="${LOCK_FILE:-/run/lock/videodownloaderbot-update.lock}"
update_started=0
replacement_attempted=0

log() { printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }
compose() { if docker compose version >/dev/null 2>&1; then docker compose "$@"; else docker-compose "$@"; fi; }

wait_until_stable() {
  local id running restarts health attempt stable=0
  for ((attempt = 1; attempt <= 30; attempt++)); do
    id="$(compose -p "$COMPOSE_PROJECT" -f "$ROOT_DIR/docker-compose.yml" ps -q "$SERVICE_KEY")"
    running="$(docker inspect --format '{{.State.Running}}' "$id" 2>/dev/null || printf false)"
    restarts="$(docker inspect --format '{{.RestartCount}}' "$id" 2>/dev/null || printf 999)"
    health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$id" 2>/dev/null || printf unknown)"
    if [[ -n "$id" && "$running" == "true" && "$restarts" == "0" && ( "$health" == "healthy" || "$health" == "none" ) ]]; then
      stable=$((stable + 1)); [[ "$stable" -ge 5 ]] && return 0
    else
      stable=0
    fi
    sleep 2
  done
  return 1
}

rollback() {
  local code=$?
  [[ "$code" -ne 0 ]] || code=1
  trap - ERR INT TERM
  if [[ "$update_started" == "1" ]] && docker image inspect "$ROLLBACK_IMAGE" >/dev/null 2>&1; then
    log "yt-dlp update failed; restoring previous image"
    docker image tag "$ROLLBACK_IMAGE" "$IMAGE_NAME" || log "failed to restore rollback tag"
    if [[ "$replacement_attempted" == "1" ]]; then
      compose -p "$COMPOSE_PROJECT" -f "$ROOT_DIR/docker-compose.yml" \
        up -d --no-deps --force-recreate "$SERVICE_KEY" || log "failed to recreate rollback container"
    fi
  fi
  exit "$code"
}

main() {
  mkdir -p "$ROOT_DIR/logs" "$ROOT_DIR/data" "$(dirname "$LOCK_FILE")"
  find "$ROOT_DIR/logs" -maxdepth 1 -type f -name 'updater-*.log' -mtime +60 -delete
  exec >>"$ROOT_DIR/logs/updater-$(date -u '+%Y-%m-%d').log" 2>&1
  command -v flock >/dev/null 2>&1 || { log "flock is required"; return 1; }
  exec 9>"$LOCK_FILE"
  if ! flock -n 9; then log "another deployment/update is running; skipping"; return 0; fi
  [[ -f "$ROOT_DIR/.env" ]] || { log "missing .env"; return 1; }

  cd "$ROOT_DIR"
  docker image inspect "$IMAGE_NAME" >/dev/null 2>&1 || { log "current image is unavailable"; return 1; }
  local before after
  before="$(compose -p "$COMPOSE_PROJECT" run --rm --no-deps "$SERVICE_KEY" python -m yt_dlp --version)"
  docker image tag "$IMAGE_NAME" "$ROLLBACK_IMAGE"
  update_started=1
  trap rollback ERR INT TERM

  compose -p "$COMPOSE_PROJECT" build --pull \
    --build-arg "YTDLP_CACHEBUST=$(date -u '+%Y%m%dT%H%M%SZ')" "$SERVICE_KEY"
  compose -p "$COMPOSE_PROJECT" run --rm --no-deps "$SERVICE_KEY" sh -ec '
    python -c "import main, yt_dlp"
    ffmpeg -version >/dev/null
    node --version >/dev/null
    python -m yt_dlp --version >/dev/null
    python -c "import telebot; from app.settings import load_settings; telebot.TeleBot(load_settings().token).get_me()"
  '
  after="$(compose -p "$COMPOSE_PROJECT" run --rm --no-deps "$SERVICE_KEY" python -m yt_dlp --version)"
  replacement_attempted=1
  compose -p "$COMPOSE_PROJECT" up -d --no-deps --force-recreate "$SERVICE_KEY"
  wait_until_stable

  update_started=0
  trap - ERR INT TERM
  log "yt-dlp update successful: $before -> $after"
}

main "$@"
