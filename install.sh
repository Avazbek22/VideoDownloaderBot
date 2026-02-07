#!/usr/bin/env bash
set -euo pipefail

# =========================
# VideoDownloaderBot installer (Ubuntu 22/24)
# - Asks only for BOT_TOKEN
# - logs defaults to None
# - max_filesize fixed to 50MB (in config.py)
# - Installs ffmpeg (codecs) + required deps
# - Supports Docker (recommended) or systemd service install
# - Works with docker compose v2 OR docker-compose v1
# - Sets up nightly yt-dlp auto-update timer (server-side)
# =========================

REPO_URL_DEFAULT="https://github.com/Avazbek22/VideoDownloaderBot.git"
BRANCH_DEFAULT="main"
INSTALL_DIR_DEFAULT="$HOME/VideoDownloaderBot"

MAX_FILE_MB=50
MAX_FILE_BYTES=$((MAX_FILE_MB * 1024 * 1024))

# Reduce apt prompts & avoid needrestart interactive restarts
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=l

say()  { echo -e "\n\033[1m\033[36m$*\033[0m"; }
ok()   { echo -e "\033[32m✔\033[0m $*"; }
warn() { echo -e "\033[33m⚠\033[0m $*"; }
err()  { echo -e "\033[31m✖\033[0m $*" >&2; }

need_cmd() { command -v "$1" >/dev/null 2>&1; }

as_root() {
  if [[ ${EUID:-$(id -u)} -eq 0 ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

prompt_yn() {
  # prompt_yn "Question?" "Y"|"N"  -> returns 0 yes, 1 no
  local prompt="$1"
  local def="${2:-N}"
  local suffix="[y/N]"
  [[ "$def" == "Y" ]] && suffix="[Y/n]"
  read -r -p "$prompt $suffix: " ans || true
  ans="${ans:-$def}"
  case "${ans,,}" in
    y|yes) return 0 ;;
    n|no)  return 1 ;;
    *) [[ "$def" == "Y" ]] && return 0 || return 1 ;;
  esac
}

is_ubuntu() {
  [[ -f /etc/os-release ]] && grep -qi "ubuntu" /etc/os-release
}

has_systemd() {
  need_cmd systemctl && [[ "$(ps -p 1 -o comm= 2>/dev/null || true)" == "systemd" ]]
}

detect_compose() {
  # prints: "docker compose" or "docker-compose" or empty
  if need_cmd docker; then
    if docker compose version >/dev/null 2>&1; then
      echo "docker compose"
      return 0
    fi
    if need_cmd docker-compose; then
      echo "docker-compose"
      return 0
    fi
  fi
  echo ""
  return 1
}

ensure_universe_enabled() {
  if ! grep -Rqs "^[^#].*ubuntu.* universe" /etc/apt/sources.list /etc/apt/sources.list.d/*.list 2>/dev/null; then
    warn "Ubuntu 'universe' repository seems disabled."
    if prompt_yn "Enable 'universe' repository?" "Y"; then
      as_root apt-get update -y
      as_root apt-get install -y --no-install-recommends software-properties-common
      as_root add-apt-repository -y universe
      as_root apt-get update -y
      ok "Universe enabled."
    else
      warn "Universe not enabled. Some packages may be unavailable."
    fi
  fi
}

install_base_deps() {
  say "Installing system dependencies (python3, venv, pip, git, ffmpeg, nodejs)..."
  as_root apt-get update -y
  as_root apt-get install -y --no-install-recommends \
    ca-certificates curl git python3 python3-venv python3-pip ffmpeg nodejs
  ok "FFmpeg installed. Codecs are included in Ubuntu's ffmpeg build."
}

clone_or_update_repo() {
  local repo_url="$1"
  local branch="$2"
  local install_dir="$3"

  if [[ -d "$install_dir/.git" ]]; then
    say "Repository exists: $install_dir"
    say "Updating (git pull)..."
    git -C "$install_dir" fetch --all --prune
    git -C "$install_dir" checkout "$branch" >/dev/null 2>&1 || true
    git -C "$install_dir" pull --ff-only || true
    ok "Repository updated."
  else
    say "Cloning repository into: $install_dir"
    git clone --branch "$branch" --single-branch "$repo_url" "$install_dir"
    ok "Repository cloned."
  fi
}

write_env_and_config() {
  local install_dir="$1"
  local token="$2"

  # .env is used by docker-compose automatically and can be used by systemd EnvironmentFile
  cat > "$install_dir/.env" <<EOF
BOT_TOKEN=$token
OUTPUT_FOLDER=/tmp/yt-dlp-telegram
YTDLP_AUTO_UPDATE=1
YTDLP_JS_RUNTIMES=node
YTDLP_REMOTE_COMPONENTS=ejs:github
EOF

  # Keep config.py minimal: only reads env; logs=None; max_filesize fixed to 50MB
  cat > "$install_dir/config.py" <<'PY'
import os

# Required
token = (os.getenv("BOT_TOKEN") or "").strip()
if not token:
    raise RuntimeError("BOT_TOKEN is not set. Put it into .env or environment variables.")

# Optional logs chat id (disabled by default)
logs = None

# Fixed limit (50 MB)
max_filesize = 50 * 1024 * 1024

# Temp folder for downloads (can be overridden)
output_folder = (os.getenv("OUTPUT_FOLDER") or "/tmp/yt-dlp-telegram").strip() or "/tmp/yt-dlp-telegram"
PY

  # Make sure token file isn't accidentally committed (best effort)
  if [[ -f "$install_dir/.gitignore" ]]; then
    grep -qxF ".env" "$install_dir/.gitignore" || echo ".env" >> "$install_dir/.gitignore"
  else
    echo ".env" > "$install_dir/.gitignore"
  fi

  ok "Created config.py (env-based) + .env"
}

write_ytdlp_updater_script() {
  local install_dir="$1"
  mkdir -p "$install_dir/scripts"

  cat > "$install_dir/scripts/ytdlp-nightly-update.sh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$INSTALL_DIR/.env"
LOG_DIR="$INSTALL_DIR/logs"
LOG_FILE="$LOG_DIR/ytdlp-auto-update.log"

mkdir -p "$LOG_DIR"

log() {
  printf "%s %s\n" "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG_FILE"
}

load_env() {
  [[ -f "$ENV_FILE" ]] || return 0
  while IFS= read -r raw; do
    line="${raw#"${raw%%[![:space:]]*}"}"
    [[ -z "$line" || "$line" == \#* ]] && continue
    [[ "$line" != *=* ]] && continue
    key="${line%%=*}"
    value="${line#*=}"
    value="${value%\"}"
    value="${value#\"}"
    value="${value%\'}"
    value="${value#\'}"
    export "$key=$value"
  done < "$ENV_FILE"
}

yt_dlp_version_py='import yt_dlp
v = getattr(yt_dlp, "__version__", None)
if not v:
    v = getattr(getattr(yt_dlp, "version", None), "__version__", None)
print(v or "unknown")'

check_runtime_py='import os, shutil
raw = (os.getenv("YTDLP_JS_RUNTIMES") or "node").split(",")[0].strip()
runtime, _, path = raw.partition(":")
runtime = runtime.strip() or "node"
target = (path.strip() if path.strip() else runtime)
print(f"runtime={runtime} target={target} found={bool(shutil.which(target) if target == runtime else os.path.exists(target))}")'

detect_compose() {
  if command -v docker >/dev/null 2>&1; then
    if docker compose version >/dev/null 2>&1; then
      echo "docker compose"
      return 0
    fi
    if command -v docker-compose >/dev/null 2>&1; then
      echo "docker-compose"
      return 0
    fi
  fi
  return 1
}

restart_system_service_if_exists() {
  if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files --type=service | grep -q '^videodownloaderbot\.service'; then
    systemctl restart videodownloaderbot.service
    log "Restarted videodownloaderbot.service"
  else
    log "videodownloaderbot.service not found; skip restart"
  fi
}

log_runtime_status_system() {
  local py="$INSTALL_DIR/.venv/bin/python"
  [[ -x "$py" ]] || return 0
  "$py" -c "$check_runtime_py" >>"$LOG_FILE" 2>&1 || true
}

log_runtime_status_docker() {
  docker exec videodownloaderbot python -c "$check_runtime_py" >>"$LOG_FILE" 2>&1 || true
}

update_system_mode() {
  local py="$INSTALL_DIR/.venv/bin/python"
  if [[ ! -x "$py" ]]; then
    return 1
  fi

  local before after
  before="$("$py" -c "$yt_dlp_version_py" 2>/dev/null || echo "unknown")"
  log "System mode: current yt-dlp version: $before"

  if ! "$py" -m pip install --upgrade --disable-pip-version-check "yt-dlp[default]" >>"$LOG_FILE" 2>&1; then
    log "System mode: pip update failed"
    return 1
  fi

  after="$("$py" -c "$yt_dlp_version_py" 2>/dev/null || echo "unknown")"
  if [[ "$before" != "$after" ]]; then
    log "System mode: yt-dlp updated: $before -> $after"
    restart_system_service_if_exists
  else
    log "System mode: yt-dlp already up to date: $after"
  fi
  log_runtime_status_system
  return 0
}

update_docker_mode() {
  [[ -f "$INSTALL_DIR/docker-compose.yml" ]] || return 1
  command -v docker >/dev/null 2>&1 || return 1

  local compose_cmd=""
  compose_cmd="$(detect_compose || true)"
  if [[ -n "$compose_cmd" ]]; then
    (cd "$INSTALL_DIR" && $compose_cmd up -d >/dev/null 2>&1) || true
  fi

  if ! docker ps --format '{{.Names}}' | grep -qx "videodownloaderbot"; then
    log "Docker mode: container 'videodownloaderbot' is not running; skip"
    return 1
  fi

  local before after
  before="$(docker exec videodownloaderbot python -c "$yt_dlp_version_py" 2>/dev/null || echo "unknown")"
  log "Docker mode: current yt-dlp version: $before"

  if ! docker exec videodownloaderbot python -m pip install --upgrade --disable-pip-version-check "yt-dlp[default]" >>"$LOG_FILE" 2>&1; then
    log "Docker mode: pip update failed"
    return 1
  fi

  after="$(docker exec videodownloaderbot python -c "$yt_dlp_version_py" 2>/dev/null || echo "unknown")"
  if [[ "$before" != "$after" ]]; then
    log "Docker mode: yt-dlp updated: $before -> $after"
    docker restart videodownloaderbot >/dev/null
    log "Docker mode: restarted container videodownloaderbot"
  else
    log "Docker mode: yt-dlp already up to date: $after"
  fi
  log_runtime_status_docker
  return 0
}

main() {
  load_env
  if [[ "${YTDLP_AUTO_UPDATE:-1}" == "0" ]]; then
    log "YTDLP_AUTO_UPDATE=0 -> auto update disabled"
    exit 0
  fi

  local ok_mode=1
  if update_docker_mode; then
    ok_mode=0
  elif update_system_mode; then
    ok_mode=0
  fi

  if [[ $ok_mode -ne 0 ]]; then
    log "No supported runtime found (.venv or docker-compose/container)."
    exit 0
  fi
}

main "$@"
SH

  chmod +x "$install_dir/scripts/ytdlp-nightly-update.sh"
  ok "Created nightly updater: $install_dir/scripts/ytdlp-nightly-update.sh"
}

setup_ytdlp_auto_update_timer() {
  local install_dir="$1"

  write_ytdlp_updater_script "$install_dir"

  if ! has_systemd; then
    warn "systemd not detected; nightly yt-dlp auto-update timer was not created."
    return 0
  fi

  say "Creating systemd timer: videodownloaderbot-ytdlp-update.timer"

  as_root tee /etc/systemd/system/videodownloaderbot-ytdlp-update.service >/dev/null <<EOF
[Unit]
Description=Nightly yt-dlp update for VideoDownloaderBot
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$install_dir
ExecStart=$install_dir/scripts/ytdlp-nightly-update.sh
EOF

  as_root tee /etc/systemd/system/videodownloaderbot-ytdlp-update.timer >/dev/null <<'EOF'
[Unit]
Description=Run nightly yt-dlp update for VideoDownloaderBot

[Timer]
OnCalendar=*-*-* 03:00:00
RandomizedDelaySec=2h
Persistent=true
Unit=videodownloaderbot-ytdlp-update.service

[Install]
WantedBy=timers.target
EOF

  as_root systemctl daemon-reload
  as_root systemctl enable --now videodownloaderbot-ytdlp-update.timer

  ok "Nightly yt-dlp auto-update is enabled."
  echo "Timer status:"
  echo "  sudo systemctl status videodownloaderbot-ytdlp-update.timer"
  echo "Last/next runs:"
  echo "  systemctl list-timers --all | grep videodownloaderbot-ytdlp-update"
  echo "Manual run:"
  echo "  sudo systemctl start videodownloaderbot-ytdlp-update.service"
}

ensure_docker_installed() {
  if need_cmd docker; then
    ok "Docker found."
    return 0
  fi

  warn "Docker is not installed."
  if prompt_yn "Install Docker (docker.io) from Ubuntu repositories?" "Y"; then
    ensure_universe_enabled
    as_root apt-get update -y
    as_root apt-get install -y --no-install-recommends docker.io
    as_root systemctl enable --now docker || true
    ok "Docker installed."
  else
    return 1
  fi
}

ensure_compose_available() {
  local compose_cmd
  compose_cmd="$(detect_compose || true)"
  if [[ -n "$compose_cmd" ]]; then
    ok "Compose found: $compose_cmd"
    return 0
  fi

  warn "Docker Compose is not available."
  ensure_universe_enabled

  # Try plugin first (docker compose v2)
  if prompt_yn "Install Docker Compose plugin (recommended)?" "Y"; then
    if as_root apt-get install -y --no-install-recommends docker-compose-plugin; then
      ok "docker-compose-plugin installed."
    else
      warn "docker-compose-plugin is not available in your apt sources."
    fi
  fi

  compose_cmd="$(detect_compose || true)"
  if [[ -n "$compose_cmd" ]]; then
    ok "Compose is ready: $compose_cmd"
    return 0
  fi

  # Fallback to docker-compose v1 (package name differs by distro; on Ubuntu it’s docker-compose)
  warn "Falling back to docker-compose (v1)."
  if as_root apt-get install -y --no-install-recommends docker-compose; then
    ok "docker-compose installed."
  else
    err "Failed to install Docker Compose. Please install it manually, then re-run install.sh."
    return 1
  fi

  compose_cmd="$(detect_compose || true)"
  [[ -n "$compose_cmd" ]] && ok "Compose is ready: $compose_cmd" && return 0

  err "Compose still not detected."
  return 1
}

ensure_docker_permissions() {
  # If not root and docker socket permission denied: add user to docker group
  if [[ ${EUID:-$(id -u)} -eq 0 ]]; then
    return 0
  fi
  if docker ps >/dev/null 2>&1; then
    return 0
  fi

  # If it's permission denied, fix group membership
  if docker ps 2>&1 | grep -qi "permission denied"; then
    warn "Docker daemon socket permission denied for user '$USER'."
    if prompt_yn "Add user '$USER' to docker group (recommended)?" "Y"; then
      as_root usermod -aG docker "$USER" || true
      ok "User added to docker group."
      warn "You may need to re-login. For this script run, I'll try to continue via 'sg docker' where possible."
    fi
  fi
}

write_docker_files() {
  local install_dir="$1"

  mkdir -p "$install_dir/docker"

  cat > "$install_dir/docker/entrypoint.sh" <<'SH'
#!/usr/bin/env sh
set -eu

cd /app
# Load .env if present (docker compose already does, but keep safe)
if [ -f "/app/.env" ]; then
  # shellcheck disable=SC2046
  export $(grep -v '^#' /app/.env | xargs -d '\n' 2>/dev/null || true)
fi

exec python -u /app/main.py
SH
  chmod +x "$install_dir/docker/entrypoint.sh" || true

  cat > "$install_dir/Dockerfile" <<'DOCKER'
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update -y && apt-get install -y --no-install-recommends \
      ffmpeg ca-certificates nodejs \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app
RUN chmod +x /app/docker/entrypoint.sh

ENTRYPOINT ["/app/docker/entrypoint.sh"]
DOCKER

  cat > "$install_dir/docker-compose.yml" <<'YML'
services:
  videodownloaderbot:
    build: .
    container_name: videodownloaderbot
    restart: unless-stopped
    env_file:
      - .env
YML

  ok "Docker files created."
}

run_with_compose() {
  local install_dir="$1"
  local compose_cmd
  compose_cmd="$(detect_compose)"

  say "Starting with Docker Compose..."
  (cd "$install_dir" && $compose_cmd up -d --build)

  ok "Docker container started."
  echo
  echo "Logs:"
  echo "  cd \"$install_dir\" && ($compose_cmd logs -f --tail=200)"
  echo "Stop:"
  echo "  cd \"$install_dir\" && ($compose_cmd down)"
}

install_system_mode() {
  local install_dir="$1"

  say "Installing in system mode (venv + systemd if available)..."
  mkdir -p "$install_dir/.venv"

  python3 -m venv "$install_dir/.venv"
  "$install_dir/.venv/bin/pip" install --upgrade pip >/dev/null 2>&1 || true
  "$install_dir/.venv/bin/pip" install -r "$install_dir/requirements.txt"
  if ! need_cmd node; then
    warn "node is missing; installing nodejs for yt-dlp JS runtime..."
    as_root apt-get update -y
    as_root apt-get install -y --no-install-recommends nodejs
  fi

  ok "Python venv ready."

  if has_systemd; then
    say "Creating systemd service: videodownloaderbot"

    as_root tee /etc/systemd/system/videodownloaderbot.service >/dev/null <<EOF
[Unit]
Description=VideoDownloaderBot (Telegram)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$install_dir
EnvironmentFile=$install_dir/.env
ExecStart=$install_dir/.venv/bin/python -u $install_dir/main.py
Restart=on-failure
RestartSec=3
# Security basics
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

    as_root systemctl daemon-reload
    as_root systemctl enable --now videodownloaderbot
    ok "Service started."

    echo
    echo "Status:"
    echo "  sudo systemctl status videodownloaderbot"
    echo "Logs:"
    echo "  sudo journalctl -u videodownloaderbot -f --no-pager"
    echo "Stop:"
    echo "  sudo systemctl stop videodownloaderbot"
  else
    warn "systemd is not available (or PID1 is not systemd)."
    echo
    echo "Run manually:"
    echo "  cd \"$install_dir\""
    echo "  set -a; source .env; set +a"
    echo "  \"$install_dir/.venv/bin/python\" -u main.py"
  fi
}

main() {
  if ! is_ubuntu; then
    warn "This installer is designed for Ubuntu 22.04/24.04. Continuing anyway..."
  fi

  local repo_url="${REPO_URL:-$REPO_URL_DEFAULT}"
  local branch="${BRANCH:-$BRANCH_DEFAULT}"
  local install_dir="${INSTALL_DIR:-$INSTALL_DIR_DEFAULT}"

  echo
  say "VideoDownloaderBot installer (Ubuntu 22/24)"
  echo
  echo "Repository: $repo_url"
  echo "Install folder: $install_dir"
  echo

  install_base_deps
  clone_or_update_repo "$repo_url" "$branch" "$install_dir"

  # Ask only for token
  local token=""
  while [[ -z "$token" ]]; do
    read -r -p "Enter Telegram Bot Token: " token || true
    token="$(echo -n "$token" | tr -d '\r\n' | xargs)"
    [[ -z "$token" ]] && warn "Token can't be empty."
  done

  write_env_and_config "$install_dir" "$token"

  if prompt_yn "Install & run using Docker?" "Y"; then
    ensure_docker_installed || { err "Docker is required for Docker install."; exit 1; }
    ensure_docker_permissions || true
    ensure_compose_available || { err "Compose is required."; exit 1; }
    write_docker_files "$install_dir"

    # If non-root and still permission issues, try running compose via sg docker
    if [[ ${EUID:-$(id -u)} -ne 0 ]] && ! docker ps >/dev/null 2>&1; then
      if need_cmd sg; then
        say "Trying to run Docker commands via 'sg docker'..."
        sg docker -c "cd \"$install_dir\" && $(detect_compose) up -d --build"
        ok "Docker container started (via sg docker)."
        echo
        echo "Logs:"
        echo "  cd \"$install_dir\" && ($(detect_compose) logs -f --tail=200)"
        echo "Stop:"
        echo "  cd \"$install_dir\" && ($(detect_compose) down)"
      else
        err "Docker permission issue detected. Please re-login or run the script as root."
        exit 1
      fi
    else
      run_with_compose "$install_dir"
    fi
  else
    install_system_mode "$install_dir"
  fi

  setup_ytdlp_auto_update_timer "$install_dir"

  echo
  ok "Done."
}

main "$@"
