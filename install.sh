#!/usr/bin/env bash
# One-time server setup that is safe to run again at any time. It installs
# Docker when it is missing, prepares .env, starts the checked-out commit as a
# verified release, and enables automatic deployment for this bot only.
set -Eeuo pipefail

REPOSITORY="${REPOSITORY:-https://github.com/Avazbek22/VideoDownloaderBot.git}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# `bash <(curl -fsSL .../install.sh)` runs this file without a checkout next to
# it: clone the repository as the current user first, then run its installer.
if [[ ! -f "$ROOT_DIR/scripts/lib-production.sh" ]]; then
  target="${INSTALL_DIR:-$PWD/VideoDownloaderBot}"
  if [[ ! -d "$target/.git" ]]; then
    command -v git >/dev/null 2>&1 || {
      printf 'Install git first: sudo apt-get install -y git\n' >&2
      exit 1
    }
    git clone --branch main "$REPOSITORY" "$target"
  fi
  exec bash "$target/install.sh" "$@"
fi

if [[ "$(id -u)" != "0" ]]; then
  command -v sudo >/dev/null 2>&1 || {
    printf 'Run install.sh as root.\n' >&2
    exit 1
  }
  exec sudo --preserve-env=BOT_TOKEN,APP_NAME,APP_SLUG,INSTALL_SKIP_PREREQUISITES \
    bash "$ROOT_DIR/install.sh" "$@"
fi
LIBRARY="$ROOT_DIR/scripts/lib-production.sh"
[[ -f "$LIBRARY" ]] || {
  printf 'Run install.sh from a complete repository checkout.\n' >&2
  exit 1
}
# shellcheck source=scripts/lib-production.sh
source "$LIBRARY"

install_prerequisites() {
  local -a packages=()
  if [[ "${INSTALL_SKIP_PREREQUISITES:-0}" == "1" ]]; then
    return 0
  fi
  [[ -r /etc/os-release ]] || die "Ubuntu 22.04 or 24.04 is required"
  # shellcheck disable=SC1091
  source /etc/os-release
  [[ "${ID:-}" == "ubuntu" ]] || die "Ubuntu 22.04 or 24.04 is required"
  case "${VERSION_ID:-}" in
    22.04 | 24.04) ;;
    *) die "Supported Ubuntu versions are 22.04 and 24.04" ;;
  esac

  command_exists git || packages+=(git)
  command_exists docker || packages+=(docker.io)
  command_exists flock || packages+=(util-linux)
  command_exists curl || packages+=(curl)
  command_exists python3 || packages+=(python3)
  dpkg-query -W -f='${Status}' ca-certificates 2>/dev/null | grep -q 'ok installed' ||
    packages+=(ca-certificates)
  if ((${#packages[@]} > 0)); then
    apt-get update
    apt-get install -y --no-install-recommends "${packages[@]}"
  fi
  systemctl enable --now docker
  docker info >/dev/null 2>&1 || die "The Docker daemon is not running"

  if ! docker compose version >/dev/null 2>&1 && ! command_exists docker-compose; then
    apt-get install -y --no-install-recommends docker-compose-v2 ||
      apt-get install -y --no-install-recommends docker-compose-plugin ||
      apt-get install -y --no-install-recommends docker-compose
  fi
  compose version >/dev/null
}

validate_repository() {
  local branch
  [[ -d "$ROOT_DIR/.git" ]] ||
    die "Clone the repository with git before running install.sh"
  run_git remote get-url origin >/dev/null 2>&1 ||
    die "Git remote 'origin' is required for automatic deployment"
  branch="$(run_git branch --show-current)"
  [[ "$branch" == "$DEPLOY_BRANCH" ]] ||
    die "Check out $DEPLOY_BRANCH before running install.sh (now on '$branch')"
  [[ -z "$(run_git status --porcelain --untracked-files=no)" ]] ||
    die "Tracked files have local changes in $ROOT_DIR; commit or restore them first"
}

prepare_environment() {
  local env_file="$ROOT_DIR/.env" token owner
  if [[ ! -f "$env_file" ]]; then
    install -m 600 "$ROOT_DIR/.env-example" "$env_file"
  fi
  token="$(env_value BOT_TOKEN "$env_file")"
  if [[ -z "$token" ]]; then
    token="${BOT_TOKEN:-}"
    if [[ -z "$token" ]]; then
      [[ -t 0 ]] || die "BOT_TOKEN is required; pass it as an environment variable"
      printf 'Telegram BOT_TOKEN: ' >&2
      read -r -s token
      printf '\n' >&2
    fi
    [[ "$token" =~ ^$TOKEN_REGEX$ ]] || die "BOT_TOKEN has an invalid format"
    set_env_value BOT_TOKEN "$token" "$env_file"
  elif [[ ! "$token" =~ ^$TOKEN_REGEX$ ]]; then
    die "BOT_TOKEN in .env has an invalid format"
  fi

  if [[ -z "$(env_value APP_NAME "$env_file")" && -n "${APP_NAME:-}" ]]; then
    set_env_value APP_NAME "$APP_NAME" "$env_file"
  fi
  # Pin the name so that renaming the directory never orphans the bot.
  resolve_app_slug
  if [[ "$(env_value APP_SLUG "$env_file")" != "$APP_SLUG" ]]; then
    set_env_value APP_SLUG "$APP_SLUG" "$env_file"
  fi
  chmod 600 "$env_file"
  owner="$(stat -c '%u:%g' "$ROOT_DIR/.git")"
  chown "$owner" "$env_file"

  mkdir -p "$ROOT_DIR/data" "$ROOT_DIR/logs"
  chown -R 10001:10001 "$ROOT_DIR/data" "$ROOT_DIR/logs"
}

# Older versions of these scripts kept rollback state in data/, extra image
# tags, and a separate yt-dlp updater timer, which the scheduled rebuild in
# deploy.conf now replaces. The running container is adopted by
# adopt_running_release.
migrate_legacy_state() {
  local tag unit legacy_failed="$ROOT_DIR/data/.failed-deploy-sha"
  if [[ -f "$legacy_failed" && ! -f "$(state_file failed-commit)" ]]; then
    tr -d '[:space:]' <"$legacy_failed" >"$(state_file failed-commit)"
  fi
  rm -f "$legacy_failed" "$ROOT_DIR/data/.rollback-commit"
  for tag in rollback install-rollback pre-manual-rollback; do
    remove_image_tag "$APP_SLUG:$tag"
  done
  unit="$APP_SLUG-yt-dlp-update"
  if [[ -f "$SYSTEMD_DIR/$unit.timer" || -f "$SYSTEMD_DIR/$unit.service" ]]; then
    "$SYSTEMCTL" disable --now "$unit.timer" >/dev/null 2>&1 || true
    rm -f "$SYSTEMD_DIR/$unit.timer" "$SYSTEMD_DIR/$unit.service"
    "$SYSTEMCTL" daemon-reload || true
    log "Removed the old $unit timer; deploy.conf schedules yt-dlp rebuilds now"
  fi
}

print_summary() {
  log "Installation complete: $APP_SLUG"
  cat <<SUMMARY

Every push to $DEPLOY_BRANCH is now deployed automatically once its CI checks pass.

  Status:    sudo bash scripts/status.sh
  Roll back: sudo bash scripts/rollback.sh
  Deploy:    sudo bash scripts/deploy.sh   (optional; the timer checks every two minutes)
  Logs:      $ROOT_DIR/logs/
SUMMARY
}

main() {
  install_prerequisites
  load_deploy_config
  validate_repository
  prepare_environment
  prepare_state_dir
  open_log
  acquire_lock 0 || die "Another deployment is running; try again in a minute"
  recover_interrupted_release
  adopt_running_release
  migrate_legacy_state
  release_commit "$(run_git rev-parse HEAD)" install
  install_units || die "Could not install the systemd timers"
  print_summary
}

main "$@"
