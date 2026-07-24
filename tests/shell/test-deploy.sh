#!/usr/bin/env bash
set -Eeuo pipefail

REPOSITORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEST_ROOT"' EXIT

make_fake_commands() {
  local bin="$1"
  mkdir -p "$bin"
  cat >"$bin/git" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
printf 'git %s\n' "$*" >>"$FAKE_COMMAND_LOG"
case "$*" in
  *"status --porcelain"*) exit 0 ;;
  *"rev-parse HEAD"*) cat "$FAKE_STATE_DIR/head" ;;
  *"rev-parse refs/remotes/origin/main"*) cat "$FAKE_STATE_DIR/target" ;;
  *"merge-base --is-ancestor"*) exit 0 ;;
  *"diff --name-only"*) cat "$FAKE_STATE_DIR/changes" ;;
  *"checkout -q -B"*) printf '%s\n' "${!#}" >"$FAKE_STATE_DIR/head" ;;
  *"pull --ff-only origin main"*) cat "$FAKE_STATE_DIR/target" >"$FAKE_STATE_DIR/head" ;;
esac
SH
  cat >"$bin/docker" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
printf 'docker %s\n' "$*" >>"$FAKE_COMMAND_LOG"
if [[ "$*" == *" run "* && "${FAKE_FAIL_SMOKE:-0}" == "1" ]]; then exit 1; fi
case "$*" in
  "info") exit 0 ;;
  "compose version") exit 0 ;;
  "image inspect "*) exit 0 ;;
  "image tag "*) exit 0 ;;
  *" build "*)
    [[ "${FAKE_FAIL_BUILD:-0}" != "1" ]]
    : >"$FAKE_STATE_DIR/image-built"
    ;;
  *" run "*"python -m yt_dlp --version"*)
    if [[ "${FAKE_SAME_VERSION:-0}" == "1" || ! -f "$FAKE_STATE_DIR/image-built" ]]; then
      echo 2026.01.01
    else
      echo 2026.02.01
    fi
    ;;
  *" run "*) [[ "${FAKE_FAIL_SMOKE:-0}" != "1" ]] ;;
  *" ps -q "*) echo fake-container ;;
  "inspect --format {{.State.Running}} fake-container") echo true ;;
  "inspect --format {{.RestartCount}} fake-container") echo 0 ;;
  "inspect --format "*" fake-container")
    if [[ "${FAKE_BAD_HEALTH:-0}" == "1" ]]; then
      echo unhealthy
    elif [[ "${FAKE_HEALTH_NONE:-0}" == "1" ]]; then
      echo none
    else
      echo healthy
    fi
    ;;
  *" up -d "*) exit 0 ;;
esac
SH
  cat >"$bin/systemctl" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
printf 'systemctl %s\n' "$*" >>"$FAKE_COMMAND_LOG"
[[ "${FAKE_FAIL_SYSTEMD:-0}" != "1" ]]
SH
  cat >"$bin/id" <<'SH'
#!/usr/bin/env bash
if [[ "${1:-}" == "-u" ]]; then echo 0; else /usr/bin/id "$@"; fi
SH
  cat >"$bin/chown" <<'SH'
#!/usr/bin/env bash
exit 0
SH
  cat >"$bin/flock" <<'SH'
#!/usr/bin/env bash
exit 0
SH
  cat >"$bin/sleep" <<'SH'
#!/usr/bin/env bash
exit 0
SH
  chmod 755 "$bin/git" "$bin/docker" "$bin/flock" "$bin/sleep" \
    "$bin/systemctl" "$bin/id" "$bin/chown"
}

prepare_case() {
  local root="$1"
  mkdir -p "$root/.git" "$root/data" "$root/logs" "$root/bin" "$root/lock" \
    "$root/scripts/systemd" "$root/systemd"
  printf '%s\n' 'BOT_TOKEN=123:test-token-value-abcdefghijklmnop' >"$root/.env"
  : >"$root/docker-compose.yml"
  cp "$REPOSITORY_ROOT/.env-example" "$root/.env-example"
  cp "$REPOSITORY_ROOT/scripts/systemd/"* "$root/scripts/systemd/"
  cp "$REPOSITORY_ROOT/scripts/deploy.sh" "$root/scripts/deploy.sh"
  cp "$REPOSITORY_ROOT/scripts/update-ytdlp.sh" "$root/scripts/update-ytdlp.sh"
  cp "$REPOSITORY_ROOT/scripts/docker-entrypoint.sh" "$root/scripts/docker-entrypoint.sh"
  printf '%s\n' old-commit >"$root/head"
  printf '%s\n' new-commit >"$root/target"
  printf '%s\n' main.py >"$root/changes"
  : >"$root/commands.log"
  make_fake_commands "$root/bin"
}

run_script() {
  local root="$1" script="$2"
  shift 2
  env PATH="$root/bin:$PATH" ROOT_DIR="$root" LOCK_FILE="$root/lock/update.lock" \
    FAKE_STATE_DIR="$root" FAKE_COMMAND_LOG="$root/commands.log" "$@" bash "$script"
}

success="$TEST_ROOT/deploy-success"
prepare_case "$success"
run_script "$success" "$REPOSITORY_ROOT/scripts/deploy.sh"
[[ "$(<"$success/head")" == new-commit ]]
grep -q 'deployment successful commit=new-commit' "$success/logs/deploy-"*.log

docs="$TEST_ROOT/docs"
prepare_case "$docs"
printf '%s\n' README.md >"$docs/changes"
run_script "$docs" "$REPOSITORY_ROOT/scripts/deploy.sh"
grep -q 'container rebuild skipped' "$docs/logs/deploy-"*.log
if grep -q ' build ' "$docs/commands.log"; then exit 1; fi

failed="$TEST_ROOT/deploy-failed"
prepare_case "$failed"
if run_script "$failed" "$REPOSITORY_ROOT/scripts/deploy.sh" FAKE_FAIL_BUILD=1; then exit 1; fi
[[ "$(<"$failed/head")" == old-commit ]]
[[ "$(<"$failed/data/.failed-deploy-sha")" == new-commit ]]
grep -q 'image tag videodownloaderbot:rollback videodownloaderbot:local' "$failed/commands.log"

invalid_token="$TEST_ROOT/invalid-token"
prepare_case "$invalid_token"
if run_script "$invalid_token" "$REPOSITORY_ROOT/scripts/deploy.sh" FAKE_FAIL_SMOKE=1; then exit 1; fi
if grep -q ' up -d ' "$invalid_token/commands.log"; then
  echo "invalid token replaced the running container" >&2
  exit 1
fi

unhealthy="$TEST_ROOT/deploy-unhealthy"
prepare_case "$unhealthy"
if run_script "$unhealthy" "$REPOSITORY_ROOT/scripts/deploy.sh" FAKE_BAD_HEALTH=1; then exit 1; fi
[[ "$(<"$unhealthy/head")" == old-commit ]]
[[ "$(grep -c ' up -d ' "$unhealthy/commands.log")" -ge 2 ]]

missing_health="$TEST_ROOT/deploy-health-none"
prepare_case "$missing_health"
if run_script "$missing_health" "$REPOSITORY_ROOT/scripts/deploy.sh" FAKE_HEALTH_NONE=1; then exit 1; fi
[[ "$(<"$missing_health/head")" == old-commit ]]

systemd_success="$TEST_ROOT/systemd-success"
prepare_case "$systemd_success"
printf '%s\n' scripts/systemd/videodownloaderbot-deploy.timer >"$systemd_success/changes"
run_script "$systemd_success" "$REPOSITORY_ROOT/scripts/deploy.sh" \
  SYSTEMD_DIR="$systemd_success/systemd" SYSTEMCTL=systemctl
grep -q 'systemctl daemon-reload' "$systemd_success/commands.log"
grep -q 'systemctl enable videodownloaderbot-deploy.timer' "$systemd_success/commands.log"

systemd_failed="$TEST_ROOT/systemd-failed"
prepare_case "$systemd_failed"
printf 'previous unit\n' >"$systemd_failed/systemd/videodownloaderbot-deploy.timer"
printf '%s\n' scripts/systemd/videodownloaderbot-deploy.timer >"$systemd_failed/changes"
if run_script "$systemd_failed" "$REPOSITORY_ROOT/scripts/deploy.sh" \
  SYSTEMD_DIR="$systemd_failed/systemd" SYSTEMCTL=systemctl FAKE_FAIL_SYSTEMD=1; then
  exit 1
fi
[[ "$(<"$systemd_failed/head")" == old-commit ]]
grep -q 'previous unit' "$systemd_failed/systemd/videodownloaderbot-deploy.timer"

updater="$TEST_ROOT/updater-failed"
prepare_case "$updater"
if run_script "$updater" "$REPOSITORY_ROOT/scripts/update-ytdlp.sh" FAKE_FAIL_BUILD=1; then exit 1; fi
grep -q 'yt-dlp update failed; restoring previous image' "$updater/logs/updater-"*.log
grep -q 'image tag videodownloaderbot:rollback videodownloaderbot:local' "$updater/commands.log"

updater_unhealthy="$TEST_ROOT/updater-unhealthy"
prepare_case "$updater_unhealthy"
if run_script "$updater_unhealthy" "$REPOSITORY_ROOT/scripts/update-ytdlp.sh" FAKE_BAD_HEALTH=1; then exit 1; fi
[[ "$(grep -c ' up -d ' "$updater_unhealthy/commands.log")" -ge 2 ]]

updater_missing_health="$TEST_ROOT/updater-health-none"
prepare_case "$updater_missing_health"
if run_script "$updater_missing_health" "$REPOSITORY_ROOT/scripts/update-ytdlp.sh" \
  FAKE_HEALTH_NONE=1; then
  exit 1
fi
[[ "$(grep -c ' up -d ' "$updater_missing_health/commands.log")" -ge 2 ]]

updater_same="$TEST_ROOT/updater-same"
prepare_case "$updater_same"
run_script "$updater_same" "$REPOSITORY_ROOT/scripts/update-ytdlp.sh" FAKE_SAME_VERSION=1
if grep -q ' up -d ' "$updater_same/commands.log"; then
  echo "unchanged yt-dlp restarted the container" >&2
  exit 1
fi
grep -q 'yt-dlp already up to date' "$updater_same/logs/updater-"*.log
grep -q 'image tag videodownloaderbot:rollback videodownloaderbot:local' "$updater_same/commands.log"

installer="$TEST_ROOT/installer-rollback"
prepare_case "$installer"
printf '%s\n' 'CUSTOM_SETTING=preserve-me' >>"$installer/.env"
printf '%s\n' persistent >"$installer/data/keep"
printf '%s\n' persistent >"$installer/logs/keep"
if env PATH="$installer/bin:$PATH" INSTALL_DIR="$installer" \
  FAKE_STATE_DIR="$installer" FAKE_COMMAND_LOG="$installer/commands.log" \
  COMPOSE_PROJECT=videodownloaderbot FAKE_FAIL_BUILD=1 \
  bash "$REPOSITORY_ROOT/install.sh"; then
  exit 1
fi
[[ "$(<"$installer/head")" == old-commit ]]
grep -q '^CUSTOM_SETTING=preserve-me$' "$installer/.env"
[[ -f "$installer/data/keep" && -f "$installer/logs/keep" ]]
grep -q 'image tag videodownloaderbot:install-rollback videodownloaderbot:local' \
  "$installer/commands.log"

installer_health="$TEST_ROOT/installer-health-none"
prepare_case "$installer_health"
if env PATH="$installer_health/bin:$PATH" INSTALL_DIR="$installer_health" \
  FAKE_STATE_DIR="$installer_health" FAKE_COMMAND_LOG="$installer_health/commands.log" \
  COMPOSE_PROJECT=videodownloaderbot FAKE_HEALTH_NONE=1 \
  bash "$REPOSITORY_ROOT/install.sh"; then
  exit 1
fi
[[ "$(<"$installer_health/head")" == old-commit ]]
[[ "$(grep -c ' up -d ' "$installer_health/commands.log")" -ge 2 ]]

if grep -q -- '--pull' "$REPOSITORY_ROOT/scripts/update-ytdlp.sh"; then
  echo "nightly updater must not use --pull" >&2
  exit 1
fi
