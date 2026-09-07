#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="${ROOT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib-production.sh
source "$SCRIPT_DIR/lib-production.sh"
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
candidate_image_id=""
previous_image_id=""
units_changed=0
units_backup=""
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"

log() { printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }

rollback() {
  local code=$?
  [[ "$code" -ne 0 ]] || code=1
  trap - ERR INT TERM
  if [[ "$deployment_started" == "1" ]]; then
    log "deployment failed; restoring commit=$old_commit"
    local image_restored=0
    if docker image inspect "$ROLLBACK_IMAGE" >/dev/null 2>&1; then
      if docker image tag "$ROLLBACK_IMAGE" "$IMAGE_NAME"; then
        image_restored=1
      else
        log "failed to restore rollback image tag"
      fi
    fi
    git -C "$ROOT_DIR" checkout -q -B "$DEPLOY_BRANCH" "$old_commit" || log "failed to restore checkout"
    if [[ "$units_changed" == "1" ]]; then
      restore_systemd_units || log "failed to restore previous systemd units"
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
    cleanup_candidate_image
    printf '%s\n' "$target_commit" >"$FAILED_SHA_FILE"
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

unit_names=(
  videodownloaderbot-deploy.service
  videodownloaderbot-deploy.timer
  videodownloaderbot-yt-dlp-update.service
  videodownloaderbot-yt-dlp-update.timer
)

backup_systemd_units() {
  local unit
  units_backup="$(mktemp -d)"
  for unit in "${unit_names[@]}"; do
    if [[ -f "$SYSTEMD_DIR/$unit" ]]; then
      cp "$SYSTEMD_DIR/$unit" "$units_backup/$unit"
    else
      : >"$units_backup/$unit.absent"
    fi
  done
}

install_systemd_units() {
  local unit source target
  mkdir -p "$SYSTEMD_DIR"
  for unit in videodownloaderbot-deploy.service videodownloaderbot-yt-dlp-update.service; do
    source="$ROOT_DIR/scripts/systemd/$unit"
    target="$SYSTEMD_DIR/$unit"
    sed -e "s|__INSTALL_DIR__|$ROOT_DIR|g" \
      -e "s|__COMPOSE_PROJECT__|$COMPOSE_PROJECT|g" \
      -e "s|__SERVICE_KEY__|$SERVICE_KEY|g" "$source" >"$target"
  done
  for unit in videodownloaderbot-deploy.timer videodownloaderbot-yt-dlp-update.timer; do
    cp "$ROOT_DIR/scripts/systemd/$unit" "$SYSTEMD_DIR/$unit"
  done
  "$SYSTEMCTL" daemon-reload
  "$SYSTEMCTL" enable videodownloaderbot-deploy.timer videodownloaderbot-yt-dlp-update.timer
}

restore_systemd_units() {
  local unit
  for unit in "${unit_names[@]}"; do
    if [[ -f "$units_backup/$unit.absent" ]]; then
      rm -f "$SYSTEMD_DIR/$unit"
    else
      cp "$units_backup/$unit" "$SYSTEMD_DIR/$unit"
    fi
  done
  "$SYSTEMCTL" daemon-reload
}

cleanup_units_backup() {
  local unit
  [[ -n "$units_backup" && -d "$units_backup" ]] || return 0
  for unit in "${unit_names[@]}"; do
    rm -f "$units_backup/$unit" "$units_backup/$unit.absent"
  done
  rmdir "$units_backup"
}

validate_checkout() {
  [[ -d "$ROOT_DIR/.git" ]] || { log "not a Git checkout: $ROOT_DIR"; return 1; }
  [[ -f "$ROOT_DIR/.env" ]] || { log "missing $ROOT_DIR/.env"; return 1; }
  # Host execution modes are intentionally managed by install.sh/systemd.
  # Ignore mode-only differences while preserving all content checks.
  git -C "$ROOT_DIR" config core.fileMode false
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

  ensure_current_image_tag
  docker image tag "$IMAGE_NAME" "$ROLLBACK_IMAGE"
  deployment_started=1
  trap rollback ERR INT TERM

  if git diff --name-only "$old_commit" "$target_commit" -- scripts/systemd |
    grep -q '^scripts/systemd/'; then
    units_changed=1
    backup_systemd_units
  fi
  git checkout -q -B "$DEPLOY_BRANCH" "$target_commit"
  if [[ "$units_changed" == "1" ]]; then
    install_systemd_units
  fi
  compose -p "$COMPOSE_PROJECT" -f "$ROOT_DIR/docker-compose.yml" build --pull "$SERVICE_KEY"
  candidate_image_id="$(docker image inspect "$IMAGE_NAME" --format '{{.Id}}' 2>/dev/null || true)"
  [[ -n "$candidate_image_id" ]] || { log "built image cannot be identified"; return 1; }
  smoke_test_image
  replacement_attempted=1
  compose -p "$COMPOSE_PROJECT" -f "$ROOT_DIR/docker-compose.yml" \
    up -d --no-deps --force-recreate "$SERVICE_KEY"
  wait_until_stable "$candidate_image_id"

  rm -f "$FAILED_SHA_FILE"
  deployment_started=0
  trap - ERR INT TERM
  cleanup_units_backup
  log "deployment successful commit=$target_commit"
}

main "$@"
