#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="${ROOT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
DEPLOY_BRANCH="main"
SERVICE_KEY="${SERVICE_KEY:-videodownloaderbot}"
COMPOSE_PROJECT="${COMPOSE_PROJECT:-videodownloaderbot}"
IMAGE_NAME="${IMAGE_NAME:-videodownloaderbot:local}"
ROLLBACK_IMAGE="${IMAGE_NAME%:*}:rollback"
LOCK_FILE="${LOCK_FILE:-/run/lock/videodownloaderbot-update.lock}"
FAILED_SHA_FILE="$ROOT_DIR/data/.failed-deploy-sha"

old_commit=""
target_commit=""
deployment_started=0
replacement_attempted=0

log() { printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }

compose() {
  if docker compose version >/dev/null 2>&1; then docker compose "$@"; else docker-compose "$@"; fi
}

wait_until_stable() {
  local container_id running restarts health attempt stable=0
  for ((attempt = 1; attempt <= 30; attempt++)); do
    container_id="$(compose -p "$COMPOSE_PROJECT" -f "$ROOT_DIR/docker-compose.yml" ps -q "$SERVICE_KEY")"
    if [[ -n "$container_id" ]]; then
      running="$(docker inspect --format '{{.State.Running}}' "$container_id" 2>/dev/null || printf false)"
      restarts="$(docker inspect --format '{{.RestartCount}}' "$container_id" 2>/dev/null || printf 999)"
      health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container_id" 2>/dev/null || printf unknown)"
      if [[ "$running" == "true" && "$restarts" == "0" && ( "$health" == "healthy" || "$health" == "none" ) ]]; then
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

rollback() {
  local code=$?
  [[ "$code" -ne 0 ]] || code=1
  trap - ERR INT TERM
  if [[ "$deployment_started" == "1" ]]; then
    log "deployment failed; restoring commit=$old_commit"
    if docker image inspect "$ROLLBACK_IMAGE" >/dev/null 2>&1; then
      docker image tag "$ROLLBACK_IMAGE" "$IMAGE_NAME" || log "failed to restore rollback image tag"
    fi
    git -C "$ROOT_DIR" checkout -q -B "$DEPLOY_BRANCH" "$old_commit" || log "failed to restore checkout"
    if [[ "$replacement_attempted" == "1" ]]; then
      compose -p "$COMPOSE_PROJECT" -f "$ROOT_DIR/docker-compose.yml" \
        up -d --no-deps --force-recreate "$SERVICE_KEY" || log "failed to recreate rollback container"
    fi
    printf '%s\n' "$target_commit" >"$FAILED_SHA_FILE"
  fi
  exit "$code"
}

validate_checkout() {
  [[ -d "$ROOT_DIR/.git" ]] || { log "not a Git checkout: $ROOT_DIR"; return 1; }
  [[ -f "$ROOT_DIR/.env" ]] || { log "missing $ROOT_DIR/.env"; return 1; }
  if [[ -n "$(git -C "$ROOT_DIR" status --porcelain)" ]]; then
    log "tracked local changes detected; deployment refused"
    return 1
  fi
}

requires_container_update() {
  local path
  while IFS= read -r path; do
    case "$path" in
      README.md|LICENSE|CONTRIBUTING.md|CODE_OF_CONDUCT.md|SECURITY.md|requirements-dev.txt|pyproject.toml|.github/*|tests/*)
        ;;
      *) return 0 ;;
    esac
  done < <(git diff --name-only --diff-filter=ACDMRTUXB "$old_commit" "$target_commit")
  return 1
}

smoke_test_image() {
  compose -p "$COMPOSE_PROJECT" -f "$ROOT_DIR/docker-compose.yml" run --rm --no-deps "$SERVICE_KEY" sh -ec '
    python -c "import main"
    ffmpeg -version >/dev/null
    node --version >/dev/null
    python -m yt_dlp --version >/dev/null
    python -c "import telebot; from app.settings import load_settings; telebot.TeleBot(load_settings().token).get_me()"
  '
}

main() {
  mkdir -p "$ROOT_DIR/logs" "$ROOT_DIR/data" "$(dirname "$LOCK_FILE")"
  find "$ROOT_DIR/logs" -maxdepth 1 -type f -name 'deploy-*.log' -mtime +60 -delete
  exec >>"$ROOT_DIR/logs/deploy-$(date -u '+%Y-%m-%d').log" 2>&1

  command -v flock >/dev/null 2>&1 || { log "flock is required"; return 1; }
  exec 9>"$LOCK_FILE"
  if ! flock -n 9; then log "another deployment/update is running; skipping"; return 0; fi

  validate_checkout
  cd "$ROOT_DIR"
  git fetch -q origin "+refs/heads/$DEPLOY_BRANCH:refs/remotes/origin/$DEPLOY_BRANCH"
  old_commit="$(git rev-parse HEAD)"
  target_commit="$(git rev-parse "refs/remotes/origin/$DEPLOY_BRANCH")"

  if [[ "$old_commit" == "$target_commit" ]]; then return 0; fi
  if [[ "${FORCE_DEPLOY:-0}" != "1" && -f "$FAILED_SHA_FILE" ]] \
      && [[ "$(tr -d '[:space:]' <"$FAILED_SHA_FILE")" == "$target_commit" ]]; then
    log "commit=$target_commit previously failed; waiting for a newer commit"
    return 0
  fi
  git merge-base --is-ancestor "$old_commit" "$target_commit" || {
    log "origin/main is not a fast-forward from commit=$old_commit"
    return 1
  }

  if ! requires_container_update; then
    git checkout -q -B "$DEPLOY_BRANCH" "$target_commit"
    rm -f "$FAILED_SHA_FILE"
    log "docs-only deployment commit=$target_commit; container rebuild skipped"
    return 0
  fi

  docker image inspect "$IMAGE_NAME" >/dev/null 2>&1 || { log "current image is unavailable"; return 1; }
  docker image tag "$IMAGE_NAME" "$ROLLBACK_IMAGE"
  deployment_started=1
  trap rollback ERR INT TERM

  git checkout -q -B "$DEPLOY_BRANCH" "$target_commit"
  compose -p "$COMPOSE_PROJECT" -f "$ROOT_DIR/docker-compose.yml" build --pull "$SERVICE_KEY"
  smoke_test_image
  replacement_attempted=1
  compose -p "$COMPOSE_PROJECT" -f "$ROOT_DIR/docker-compose.yml" \
    up -d --no-deps --force-recreate "$SERVICE_KEY"
  wait_until_stable

  rm -f "$FAILED_SHA_FILE"
  deployment_started=0
  trap - ERR INT TERM
  log "deployment successful commit=$target_commit"
}

main "$@"
