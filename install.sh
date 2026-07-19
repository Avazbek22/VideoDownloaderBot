#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_REPOSITORY="https://github.com/Avazbek22/VideoDownloaderBot.git"
BRANCH="main"
COMPOSE_PROJECT="${COMPOSE_PROJECT:-videodownloaderbot}"
SERVICE_KEY="videodownloaderbot"

if [[ -d "$SCRIPT_DIR/.git" ]]; then
  INSTALL_DIR="${INSTALL_DIR:-$SCRIPT_DIR}"
  if git -C "$SCRIPT_DIR" remote get-url origin >/dev/null 2>&1; then
    REPOSITORY="${REPOSITORY:-$(git -C "$SCRIPT_DIR" remote get-url origin)}"
  else
    REPOSITORY="${REPOSITORY:-$DEFAULT_REPOSITORY}"
  fi
else
  INSTALL_DIR="${INSTALL_DIR:-$PWD/VideoDownloaderBot}"
  REPOSITORY="${REPOSITORY:-$DEFAULT_REPOSITORY}"
fi

info() { printf '\n\033[1;36m%s\033[0m\n' "$*"; }
ok() { printf '\033[32m✓\033[0m %s\n' "$*"; }
die() { printf '\033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1; }
as_root() { if [[ "$(id -u)" == "0" ]]; then "$@"; else need sudo || die "sudo is required"; sudo "$@"; fi; }
compose() { if docker compose version >/dev/null 2>&1; then docker compose "$@"; else docker-compose "$@"; fi; }

install_prerequisites() {
  info "Installing production prerequisites"
  if ! need git || ! need docker || ! need flock; then
    need apt-get || die "Install Git, Docker and util-linux manually"
    as_root apt-get update -y
    need git || as_root apt-get install -y --no-install-recommends git ca-certificates
    need docker || as_root apt-get install -y --no-install-recommends docker.io
    need flock || as_root apt-get install -y --no-install-recommends util-linux
  fi
  if ! docker compose version >/dev/null 2>&1 && ! need docker-compose; then
    if ! as_root apt-get install -y --no-install-recommends docker-compose-plugin; then
      as_root apt-get install -y --no-install-recommends docker-compose
    fi
  fi
  (docker compose version >/dev/null 2>&1 || need docker-compose) || die "Docker Compose is unavailable"
  ok "Docker production prerequisites are ready"
}

prepare_repository() {
  info "Preparing origin/main checkout"
  if [[ -d "$INSTALL_DIR/.git" ]]; then
    [[ -z "$(git -C "$INSTALL_DIR" status --porcelain --untracked-files=no)" ]] \
      || die "Tracked local changes detected in $INSTALL_DIR"
    git -C "$INSTALL_DIR" fetch origin "$BRANCH"
    git -C "$INSTALL_DIR" checkout "$BRANCH"
    git -C "$INSTALL_DIR" pull --ff-only origin "$BRANCH"
  else
    git clone --branch "$BRANCH" --single-branch "$REPOSITORY" "$INSTALL_DIR"
  fi
  ok "Repository is ready at $INSTALL_DIR"
}

prepare_environment() {
  info "Preparing persistent configuration and directories"
  local env_file="$INSTALL_DIR/.env"
  if [[ ! -f "$env_file" ]]; then
    cp "$INSTALL_DIR/.env-example" "$env_file"
  fi
  if ! grep -Eq '^BOT_TOKEN=.+$' "$env_file"; then
    local token="${BOT_TOKEN:-}"
    if [[ -z "$token" ]]; then
      printf 'Telegram BOT_TOKEN: ' >&2
      read -r -s token
      printf '\n' >&2
    fi
    [[ "$token" =~ ^[0-9]+:[A-Za-z0-9_-]{20,}$ ]] || die "BOT_TOKEN has an invalid format"
    local temporary="$env_file.tmp"
    awk -v token="$token" 'BEGIN{done=0} /^BOT_TOKEN=/{print "BOT_TOKEN=" token; done=1; next} {print} END{if(!done) print "BOT_TOKEN=" token}' \
      "$env_file" >"$temporary"
    mv "$temporary" "$env_file"
  fi
  chmod 600 "$env_file"
  mkdir -p "$INSTALL_DIR/data" "$INSTALL_DIR/logs"
  as_root chown -R 10001:10001 "$INSTALL_DIR/data" "$INSTALL_DIR/logs"
  chmod 0755 "$INSTALL_DIR/scripts/deploy.sh" "$INSTALL_DIR/scripts/update-ytdlp.sh" \
    "$INSTALL_DIR/scripts/docker-entrypoint.sh"
  ok ".env, data/ and logs/ are preserved and ready"
}

wait_until_healthy() {
  local id running health attempt
  for ((attempt = 1; attempt <= 30; attempt++)); do
    id="$(compose -p "$COMPOSE_PROJECT" -f "$INSTALL_DIR/docker-compose.yml" ps -q "$SERVICE_KEY")"
    if [[ -n "$id" ]]; then
      running="$(docker inspect --format '{{.State.Running}}' "$id")"
      health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$id")"
      if [[ "$running" == "true" && ( "$health" == "healthy" || "$health" == "none" ) ]]; then return 0; fi
    fi
    sleep 2
  done
  return 1
}

start_bot() {
  info "Building and validating the production image"
  cd "$INSTALL_DIR"
  if docker image inspect videodownloaderbot:local >/dev/null 2>&1; then
    docker image tag videodownloaderbot:local videodownloaderbot:rollback
  fi
  compose -p "$COMPOSE_PROJECT" build --pull "$SERVICE_KEY"
  compose -p "$COMPOSE_PROJECT" run --rm --no-deps "$SERVICE_KEY" sh -ec '
    python -c "import main"
    ffmpeg -version >/dev/null
    node --version >/dev/null
    python -m yt_dlp --version >/dev/null
    python -c "import telebot; from app.settings import load_settings; telebot.TeleBot(load_settings().token).get_me()"
  '
  compose -p "$COMPOSE_PROJECT" up -d --no-deps --force-recreate "$SERVICE_KEY"
  wait_until_healthy || die "Container did not reach a stable healthy state"
  ok "VideoDownloaderBot is healthy"
}

install_systemd_units() {
  need systemctl || return 0
  [[ -d /run/systemd/system ]] || return 0
  info "Installing deployment and yt-dlp update timers"
  local unit source target
  for unit in videodownloaderbot-deploy.service videodownloaderbot-yt-dlp-update.service; do
    source="$INSTALL_DIR/scripts/systemd/$unit"
    target="/etc/systemd/system/$unit"
    sed -e "s|__INSTALL_DIR__|$INSTALL_DIR|g" \
        -e "s|__COMPOSE_PROJECT__|$COMPOSE_PROJECT|g" \
        -e "s|__SERVICE_KEY__|$SERVICE_KEY|g" "$source" | as_root tee "$target" >/dev/null
  done
  for unit in videodownloaderbot-deploy.timer videodownloaderbot-yt-dlp-update.timer; do
    as_root cp "$INSTALL_DIR/scripts/systemd/$unit" "/etc/systemd/system/$unit"
  done
  as_root systemctl daemon-reload
  as_root systemctl enable --now videodownloaderbot-deploy.timer videodownloaderbot-yt-dlp-update.timer
  ok "origin/main deployment and nightly yt-dlp timers are enabled"
}

main() {
  install_prerequisites
  prepare_repository
  prepare_environment
  start_bot
  install_systemd_units
  printf '\nLogs: cd %q && docker compose -p %q logs -f --tail=200\n' "$INSTALL_DIR" "$COMPOSE_PROJECT"
}

main "$@"
