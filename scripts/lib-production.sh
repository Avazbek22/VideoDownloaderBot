#!/usr/bin/env bash

compose() {
  if docker compose version >/dev/null 2>&1; then
    docker compose "$@"
  else
    docker-compose "$@"
  fi
}

wait_until_stable() {
  local container_id running restarts health attempt stable=0
  for ((attempt = 1; attempt <= 30; attempt++)); do
    container_id="$(compose -p "$COMPOSE_PROJECT" -f "$ROOT_DIR/docker-compose.yml" ps -q "$SERVICE_KEY")"
    if [[ -n "$container_id" ]]; then
      running="$(docker inspect --format '{{.State.Running}}' "$container_id" 2>/dev/null || printf false)"
      restarts="$(docker inspect --format '{{.RestartCount}}' "$container_id" 2>/dev/null || printf 999)"
      health="$(docker inspect \
        --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
        "$container_id" 2>/dev/null || printf unknown)"
      if [[ "$running" == "true" && "$restarts" == "0" && "$health" == "healthy" ]]; then
        stable=$((stable + 1))
        [[ "$stable" -ge 5 ]] && return 0
      else
        stable=0
      fi
    fi
    sleep 2
  done
  return 1
}

smoke_test_image() {
  compose -p "$COMPOSE_PROJECT" -f "$ROOT_DIR/docker-compose.yml" run --rm --no-deps "$SERVICE_KEY" sh -ec '
    python -c "import main"
    ffmpeg -version >/dev/null
    ffprobe -version >/dev/null
    node --version >/dev/null
    python -m yt_dlp --version >/dev/null
    python -c "import telebot; from app.settings import load_settings; telebot.TeleBot(load_settings().token).get_me()"
  '
}
