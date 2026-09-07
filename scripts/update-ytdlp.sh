#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="${ROOT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib-production.sh
source "$SCRIPT_DIR/lib-production.sh"
SERVICE_KEY="${SERVICE_KEY:-videodownloaderbot}"
COMPOSE_PROJECT="${COMPOSE_PROJECT:-videodownloaderbot}"
IMAGE_NAME="${IMAGE_NAME:-videodownloaderbot:local}"
ROLLBACK_IMAGE="${IMAGE_NAME%:*}:rollback"
LOCK_FILE="${LOCK_FILE:-/run/lock/videodownloaderbot-update.lock}"
update_started=0
replacement_attempted=0
candidate_image_id=""
previous_image_id=""

log() { printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }

rollback() {
  local code=$?
  [[ "$code" -ne 0 ]] || code=1
  trap - ERR INT TERM
  if [[ "$update_started" == "1" ]] && docker image inspect "$ROLLBACK_IMAGE" >/dev/null 2>&1; then
    log "yt-dlp update failed; restoring previous image"
    local image_restored=0
    if docker image tag "$ROLLBACK_IMAGE" "$IMAGE_NAME"; then
      image_restored=1
    else
      log "failed to restore rollback tag"
    fi
    if [[ "$replacement_attempted" == "1" && "$image_restored" == "1" ]]; then
      compose -p "$COMPOSE_PROJECT" -f "$ROOT_DIR/docker-compose.yml" \
        up -d --no-deps --force-recreate "$SERVICE_KEY" || log "failed to recreate rollback container"
      if ! wait_until_stable "$previous_image_id"; then
        log "rollback container did not return to a stable healthy state"
      fi
    elif [[ "$replacement_attempted" == "1" ]]; then
      log "rollback container was not recreated because the previous image tag could not be restored"
    fi
    if [[ "$image_restored" == "1" ]]; then
      cleanup_candidate_image
    fi
  fi
  exit "$code"
}

cleanup_candidate_image() {
  [[ -n "$candidate_image_id" ]] || return 0
  local current_image_id
  current_image_id="$(docker image inspect "$IMAGE_NAME" --format '{{.Id}}' 2>/dev/null || true)"
  [[ "$candidate_image_id" != "$current_image_id" ]] || return 0
  docker image rm "$candidate_image_id" >/dev/null 2>&1 \
    || log "candidate image cleanup skipped id=$candidate_image_id"
}

ensure_current_image_tag() {
  local tagged_image_id
  tagged_image_id="$(docker image inspect "$IMAGE_NAME" --format '{{.Id}}' 2>/dev/null || true)"
  local -a container_ids=()
  local container_id details running_image_id running health restarts project service
  mapfile -t container_ids < <(
    compose -p "$COMPOSE_PROJECT" -f "$ROOT_DIR/docker-compose.yml" ps -q "$SERVICE_KEY" |
      sed '/^[[:space:]]*$/d'
  )
  if [[ "${#container_ids[@]}" -ne 1 ]]; then
    log "cannot recover $IMAGE_NAME: expected one compose container, found ${#container_ids[@]}"
    return 1
  fi

  container_id="${container_ids[0]}"
  details="$(docker inspect --format \
    '{{.Image}}|{{.State.Running}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}|{{.RestartCount}}|{{index .Config.Labels "com.docker.compose.project"}}|{{index .Config.Labels "com.docker.compose.service"}}' \
    "$container_id" 2>/dev/null || true)"
  IFS='|' read -r running_image_id running health restarts project service <<<"$details"
  if [[ -z "$running_image_id" || "$running" != "true" || "$health" != "healthy" \
      || "$restarts" != "0" || "$project" != "$COMPOSE_PROJECT" || "$service" != "$SERVICE_KEY" ]]; then
    log "cannot recover $IMAGE_NAME: compose container identity or health check failed"
    return 1
  fi
  if ! docker image inspect "$running_image_id" >/dev/null 2>&1; then
    log "cannot recover $IMAGE_NAME: running image is unavailable"
    return 1
  fi
  previous_image_id="$running_image_id"
  if [[ "$tagged_image_id" != "$running_image_id" ]]; then
    docker image tag "$running_image_id" "$IMAGE_NAME"
    if [[ -z "$tagged_image_id" ]]; then
      log "restored missing image tag $IMAGE_NAME from the healthy compose container"
    else
      log "corrected stale image tag $IMAGE_NAME from the healthy compose container"
    fi
  fi
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
  ensure_current_image_tag
  local before after
  before="$(compose -p "$COMPOSE_PROJECT" run --rm --no-deps "$SERVICE_KEY" python -m yt_dlp --version)"
  docker image tag "$IMAGE_NAME" "$ROLLBACK_IMAGE"
  update_started=1
  trap rollback ERR INT TERM

  compose -p "$COMPOSE_PROJECT" build \
    --build-arg "YTDLP_CACHEBUST=$(date -u '+%Y%m%dT%H%M%SZ')" "$SERVICE_KEY"
  candidate_image_id="$(docker image inspect "$IMAGE_NAME" --format '{{.Id}}' 2>/dev/null || true)"
  [[ -n "$candidate_image_id" ]] || { log "updated image cannot be identified"; return 1; }
  after="$(compose -p "$COMPOSE_PROJECT" run --rm --no-deps "$SERVICE_KEY" python -m yt_dlp --version)"
  if [[ "$before" == "$after" ]]; then
    docker image tag "$ROLLBACK_IMAGE" "$IMAGE_NAME"
    cleanup_candidate_image
    update_started=0
    trap - ERR INT TERM
    log "yt-dlp already up to date: $before"
    return 0
  fi
  smoke_test_image
  replacement_attempted=1
  compose -p "$COMPOSE_PROJECT" up -d --no-deps --force-recreate "$SERVICE_KEY"
  wait_until_stable "$candidate_image_id"

  update_started=0
  trap - ERR INT TERM
  log "yt-dlp update successful: $before -> $after"
}

main "$@"
