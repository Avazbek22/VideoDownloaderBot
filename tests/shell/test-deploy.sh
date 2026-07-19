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
esac
SH
  cat >"$bin/docker" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
printf 'docker %s\n' "$*" >>"$FAKE_COMMAND_LOG"
if [[ "$*" == *" run "* && "${FAKE_FAIL_SMOKE:-0}" == "1" ]]; then exit 1; fi
case "$*" in
  "compose version") exit 0 ;;
  "image inspect "*) exit 0 ;;
  "image tag "*) exit 0 ;;
  *" build "*) [[ "${FAKE_FAIL_BUILD:-0}" != "1" ]] ;;
  *" run "*"python -m yt_dlp --version"*) echo 2026.01.01 ;;
  *" run "*) [[ "${FAKE_FAIL_SMOKE:-0}" != "1" ]] ;;
  *" ps -q "*) echo fake-container ;;
  "inspect --format {{.State.Running}} fake-container") echo true ;;
  "inspect --format {{.RestartCount}} fake-container") echo 0 ;;
  "inspect --format "*" fake-container")
    if [[ "${FAKE_BAD_HEALTH:-0}" == "1" ]]; then echo unhealthy; else echo healthy; fi
    ;;
  *" up -d "*) exit 0 ;;
esac
SH
  cat >"$bin/flock" <<'SH'
#!/usr/bin/env bash
exit 0
SH
  cat >"$bin/sleep" <<'SH'
#!/usr/bin/env bash
exit 0
SH
  chmod 755 "$bin/git" "$bin/docker" "$bin/flock" "$bin/sleep"
}

prepare_case() {
  local root="$1"
  mkdir -p "$root/.git" "$root/data" "$root/logs" "$root/bin" "$root/lock"
  : >"$root/.env"
  : >"$root/docker-compose.yml"
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

updater="$TEST_ROOT/updater-failed"
prepare_case "$updater"
if run_script "$updater" "$REPOSITORY_ROOT/scripts/update-ytdlp.sh" FAKE_FAIL_BUILD=1; then exit 1; fi
grep -q 'yt-dlp update failed; restoring previous image' "$updater/logs/updater-"*.log
grep -q 'image tag videodownloaderbot:rollback videodownloaderbot:local' "$updater/commands.log"

updater_unhealthy="$TEST_ROOT/updater-unhealthy"
prepare_case "$updater_unhealthy"
if run_script "$updater_unhealthy" "$REPOSITORY_ROOT/scripts/update-ytdlp.sh" FAKE_BAD_HEALTH=1; then exit 1; fi
[[ "$(grep -c ' up -d ' "$updater_unhealthy/commands.log")" -ge 2 ]]
