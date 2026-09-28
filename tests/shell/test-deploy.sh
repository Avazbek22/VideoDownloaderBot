#!/usr/bin/env bash
# Deployment tests with real Git and simulated Docker, systemd, and GitHub API.
# Every case gets its own origin repository, developer clone, and server
# checkout. The fake Docker keeps images, tags, and one container on disk, and
# a commit controls how its image behaves through app/behavior:
#   fail-build, fail-smoke, unhealthy, crash
set -Eeuo pipefail

REPOSITORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf -- "$TEST_ROOT"' EXIT
BIN="$TEST_ROOT/bin"
TOKEN="123456789:abcdefghijklmnopqrstuvwxyzABCDE"
SLUG="example-bot"
CASE_NAME=""
CASE=""
DEV=""
SERVER=""
RUN_ENV=()

export HOME="$TEST_ROOT/home"
export GIT_CONFIG_NOSYSTEM=1
export GIT_AUTHOR_NAME=Test GIT_AUTHOR_EMAIL=test@example.invalid
export GIT_COMMITTER_NAME=Test GIT_COMMITTER_EMAIL=test@example.invalid
mkdir -p "$HOME"

make_fakes() {
  mkdir -p "$BIN"
  cat >"$BIN/docker" <<'SH'
#!/usr/bin/env bash
set -Eeuo pipefail
D="$FAKE_DOCKER_DIR"
printf 'docker %s\n' "$*" >>"$FAKE_COMMAND_LOG"
mkdir -p "$D/images"
touch "$D/tags"

resolve() {
  if [[ "$1" == sha256:* ]]; then
    [[ -f "$D/images/${1#sha256:}" ]] && printf '%s\n' "$1"
    return 0
  fi
  awk -v ref="$1" '$1 == ref { print $2; exit }' "$D/tags"
}
drop_tag() {
  awk -v ref="$1" '$1 != ref' "$D/tags" >"$D/tags.new"
  mv "$D/tags.new" "$D/tags"
}
set_tag() {
  drop_tag "$1"
  printf '%s %s\n' "$1" "$2" >>"$D/tags"
}
has_flag() {
  grep -qx "$2" "$D/images/${1#sha256:}"
}
counter() {
  local value=0
  [[ ! -f "$D/$1" ]] || value="$(<"$D/$1")"
  printf '%s\n' "$((value + 1))" >"$D/$1"
}

if [[ "$1" == "compose" ]]; then
  shift
  [[ "$1" != "version" ]] || exit 0
  compose_file=""
  while [[ "$1" == "-p" || "$1" == "-f" ]]; do
    [[ "$1" != "-f" ]] || compose_file="$2"
    shift 2
  done
  root="$(dirname "$compose_file")"
  image_name="$APP_SLUG:${APP_IMAGE_TAG:-local}"
  subcommand="$1"
  shift
  case "$subcommand" in
    build)
      stamp=""
      while [[ "$1" == --* ]]; do
        if [[ "$1" == "--build-arg" ]]; then
          stamp="$2"
          shift 2
        else
          shift
        fi
      done
      if [[ -f "$root/app/behavior" ]] && grep -qx fail-build "$root/app/behavior"; then
        printf 'fake build failure\n' >&2
        exit 1
      fi
      grep -Eq '^ARG REBUILD_STAMP' "$root/Dockerfile" || stamp=""
      hash="$({
        cat "$root/Dockerfile" "$root/requirements.txt" "$root/main.py"
        find "$root/app" -type f | sort | xargs cat
        printf '%s %s' "${FAKE_BASE:-base-1}" "$stamp"
      } | sha256sum | cut -c1-64)"
      {
        [[ ! -f "$root/app/behavior" ]] || cat "$root/app/behavior"
        [[ -z "${FAKE_BUILD_FLAGS:-}" ]] || printf '%s\n' "$FAKE_BUILD_FLAGS"
        printf 'version=%s\n' "${FAKE_VERSION:-1}"
      } >"$D/images/$hash"
      set_tag "$image_name" "sha256:$hash"
      counter builds
      ;;
    run)
      id="$(resolve "$image_name")"
      [[ -n "$id" ]] || { printf 'no image %s\n' "$image_name" >&2; exit 1; }
      counter smokes
      [[ -z "${FAKE_SMOKE_OUTPUT:-}" ]] || printf '%s\n' "$FAKE_SMOKE_OUTPUT"
      if has_flag "$id" fail-smoke; then
        printf 'fake smoke failure\n'
        exit 1
      fi
      ;;
    up)
      id="$(resolve "$image_name")"
      [[ -n "$id" ]] || { printf 'no image %s\n' "$image_name" >&2; exit 1; }
      health=healthy
      restarts=0
      ! has_flag "$id" unhealthy || health=unhealthy
      ! has_flag "$id" crash || restarts=2
      printf 'true %s %s %s\n' "$health" "$restarts" "$id" >"$D/container"
      counter ups
      ;;
    stop)
      if [[ -f "$D/container" ]]; then
        awk '{ $1 = "false"; print }' "$D/container" >"$D/container.new"
        mv "$D/container.new" "$D/container"
      fi
      ;;
    ps)
      [[ ! -f "$D/container" ]] || printf 'fake-container\n'
      ;;
  esac
  exit 0
fi

case "$1 ${2:-}" in
  "info "*) exit 0 ;;
  "image inspect")
    if [[ "$3" == "--format" ]]; then
      id="$(resolve "$5")"
      [[ -n "$id" ]] || exit 1
      printf '%s\n' "$id"
    else
      [[ -n "$(resolve "$3")" ]]
    fi
    ;;
  "image tag")
    id="$(resolve "$3")"
    [[ -n "$id" ]] || { printf 'No such image: %s\n' "$3" >&2; exit 1; }
    set_tag "$4" "$id"
    ;;
  "image rm")
    id="$(resolve "$3")"
    [[ -n "$id" ]] || exit 1
    drop_tag "$3"
    if ! awk -v id="$id" '$2 == id { found = 1 } END { exit !found }' "$D/tags" &&
      ! grep -q " $id\$" "$D/container" 2>/dev/null; then
      rm -f "$D/images/${id#sha256:}"
    fi
    ;;
  "image ls")
    repository="${*: -1}"
    awk -v prefix="$repository:" 'index($1, prefix) == 1 { print substr($1, length(prefix) + 1) }' "$D/tags"
    ;;
  "inspect --format")
    [[ -f "$D/container" ]] || exit 1
    cat "$D/container"
    ;;
  "run --rm")
    id="$(resolve "$7")"
    [[ -n "$id" ]] || exit 1
    sed -n 's/^version=//p' "$D/images/${id#sha256:}"
    ;;
esac
SH

  cat >"$BIN/curl" <<'SH'
#!/usr/bin/env bash
set -Eeuo pipefail
printf 'curl %s\n' "$*" >>"$FAKE_COMMAND_LOG"
config="$(cat)"
printf '%s\n' "$config" >>"$FAKE_CI_DIR/configs"
url="$(sed -n 's/^url = "\(.*\)"$/\1/p' <<<"$config")"
sha="$(sed -E 's#.*/commits/([0-9a-f]+)/check-runs.*#\1#' <<<"$url")"
printf '%s\n' "$sha" >>"$FAKE_CI_DIR/queries"
state="$(cat "$FAKE_CI_DIR/$sha" 2>/dev/null || printf 'none')"
run() {
  printf '{"id":%s,"name":"%s","status":"%s","conclusion":%s}' "$1" "$2" "$3" "$4"
}
case "$state" in
  error)
    printf 'curl: (22) The requested URL returned error: 403\n' >&2
    exit 22
    ;;
  none) printf '{"total_count":0,"check_runs":[]}\n' ;;
  pending) printf '{"check_runs":[%s,%s]}\n' "$(run 1 python completed '"success"')" "$(run 2 docker in_progress null)" ;;
  success) printf '{"check_runs":[%s,%s]}\n' "$(run 1 python completed '"success"')" "$(run 2 docker completed '"skipped"')" ;;
  failure) printf '{"check_runs":[%s,%s]}\n' "$(run 1 python completed '"success"')" "$(run 2 docker completed '"failure"')" ;;
  rerun) printf '{"check_runs":[%s,%s]}\n' "$(run 1 python completed '"failure"')" "$(run 7 python completed '"success"')" ;;
esac
SH

  cat >"$BIN/systemctl" <<'SH'
#!/usr/bin/env bash
printf 'systemctl %s\n' "$*" >>"$FAKE_COMMAND_LOG"
[[ "${FAKE_FAIL_SYSTEMD:-0}" != "1" ]]
SH
  cat >"$BIN/flock" <<'SH'
#!/usr/bin/env bash
[[ "${FAKE_LOCKED:-0}" != "1" ]]
SH
  cat >"$BIN/sleep" <<'SH'
#!/usr/bin/env bash
exit 0
SH
  cat >"$BIN/id" <<'SH'
#!/usr/bin/env bash
if [[ "${1:-}" == "-u" ]]; then
  printf '0\n'
else
  exec /usr/bin/id "$@"
fi
SH
  cat >"$BIN/chown" <<'SH'
#!/usr/bin/env bash
printf 'chown %s\n' "$*" >>"$FAKE_COMMAND_LOG"
SH
  cat >"$BIN/getent" <<'SH'
#!/usr/bin/env bash
printf 'tester:x:%s:%s::%s:/bin/bash\n' "$2" "$2" "$HOME"
SH
  cat >"$BIN/runuser" <<'SH'
#!/usr/bin/env bash
printf 'runuser %s\n' "$*" >>"$FAKE_COMMAND_LOG"
while [[ "$1" != "--" ]]; do shift; done
shift
exec "$@"
SH
  chmod 0755 "$BIN"/*
}

fail() {
  printf 'FAIL [%s]: %s\n' "$CASE_NAME" "$*" >&2
  printf -- '--- deploy log\n' >&2
  cat "$SERVER"/logs/*-deploy-*.log 2>/dev/null | tail -n 40 >&2 || true
  printf -- '--- script output\n' >&2
  tail -n 40 "$CASE/output.log" >&2 || true
  exit 1
}

new_case() {
  CASE_NAME="$1"
  CASE="$TEST_ROOT/$1"
  DEV="$CASE/dev"
  SERVER="$CASE/server"
  mkdir -p "$CASE/docker" "$CASE/ci" "$CASE/systemd" "$CASE/lock" "$DEV/app"
  : >"$CASE/commands.log"
  : >"$CASE/output.log"
  git init -q --bare -b main "$CASE/origin.git"

  cp -R "$REPOSITORY_ROOT/scripts" "$DEV/scripts"
  cp "$REPOSITORY_ROOT/install.sh" "$REPOSITORY_ROOT/docker-compose.yml" \
    "$REPOSITORY_ROOT/.env-example" "$REPOSITORY_ROOT/deploy.conf" \
    "$REPOSITORY_ROOT/Dockerfile" "$REPOSITORY_ROOT/requirements.txt" \
    "$REPOSITORY_ROOT/main.py" "$REPOSITORY_ROOT/.gitignore" "$DEV/"
  printf 'print("version 1")\n' >"$DEV/app/bot.py"
  printf '# Example bot\n' >"$DEV/README.md"
  # Exercise the deployment scripts, not this bot's nightly yt-dlp rebuild.
  sed -i 's/^REBUILD_VERSION_CMD=.*/REBUILD_VERSION_CMD=/; s/^REBUILD_SCHEDULE=.*/REBUILD_SCHEDULE="Sun *-*-* 04:00:00"/' \
    "$DEV/deploy.conf"
  printf 'FROM python:3.12-slim\n' >"$DEV/Dockerfile"
  git -C "$DEV" init -q -b main
  git -C "$DEV" add -A
  git -C "$DEV" commit -q -m "Initial bot"
  git -C "$DEV" remote add origin "$CASE/origin.git"
  git -C "$DEV" push -q origin main

  git clone -q "$CASE/origin.git" "$SERVER"
  git -C "$SERVER" config remote.origin.url https://github.com/example/test-bot.git
  git -C "$SERVER" config "url.$CASE/origin.git.insteadOf" https://github.com/example/test-bot.git
  printf 'BOT_TOKEN=%s\nAPP_NAME=Example Bot\n' "$TOKEN" >"$SERVER/.env"
}

# commit MESSAGE [path=content | -path]...; prints the new commit
commit() {
  local message="$1" spec path
  shift
  for spec in "$@"; do
    if [[ "$spec" == -* ]]; then
      rm -f "$DEV/${spec#-}"
    else
      path="${spec%%=*}"
      mkdir -p "$(dirname "$DEV/$path")"
      printf '%s\n' "${spec#*=}" >"$DEV/$path"
    fi
  done
  git -C "$DEV" add -A
  git -C "$DEV" commit -q -m "$message"
  git -C "$DEV" push -q origin main
  git -C "$DEV" rev-parse HEAD
}

run_tool() {
  local script="$1"
  shift
  env PATH="$BIN:$PATH" ROOT_DIR="$SERVER" LOCK_FILE="$CASE/lock/deploy.lock" \
    SYSTEMD_DIR="$CASE/systemd" SYSTEMCTL=systemctl INSTALL_SKIP_PREREQUISITES=1 \
    FAKE_DOCKER_DIR="$CASE/docker" FAKE_CI_DIR="$CASE/ci" \
    FAKE_COMMAND_LOG="$CASE/commands.log" GITHUB_API_URL=https://api.github.test \
    HEALTH_ATTEMPTS=4 HEALTH_STABLE_COUNT=2 HEALTH_INTERVAL_SECONDS=0 \
    "${RUN_ENV[@]}" bash "$SERVER/$script" "$@" </dev/null >>"$CASE/output.log" 2>&1
}

install_bot() { run_tool install.sh || fail "install.sh failed"; }
deploy() { run_tool scripts/deploy.sh "$@" || fail "deploy.sh failed"; }
deploy_fails() { if run_tool scripts/deploy.sh "$@"; then fail "deploy.sh unexpectedly succeeded"; fi; }
rollback() { run_tool scripts/rollback.sh --yes || fail "rollback.sh failed"; }

head_commit() { git -C "$SERVER" rev-parse HEAD; }
state() { awk -F= -v key="$2" '$1 == key { print substr($0, length(key) + 2) }' "$SERVER/.deploy/$1" 2>/dev/null || true; }
tag_id() { awk -v ref="$1" '$1 == ref { print $2 }' "$CASE/docker/tags"; }
running() { awk '{ print $1 }' "$CASE/docker/container"; }
running_image() { awk '{ print $4 }' "$CASE/docker/container"; }
count() { cat "$CASE/docker/$1" 2>/dev/null || printf '0'; }
release_tags() { awk -v prefix="$SLUG:r-" 'index($1, prefix) == 1' "$CASE/docker/tags" | wc -l | tr -d ' '; }
deploy_log() { cat "$SERVER"/logs/"$SLUG"-deploy-*.log 2>/dev/null || true; }
log_has() { deploy_log | grep -Fq -- "$1" || fail "deploy log lacks: $1"; }
log_count() { deploy_log | grep -Fc -- "$1" || true; }
expect() { [[ "$1" == "$2" ]] || fail "$3 (expected '$2', got '$1')"; }
set_container() { # set_container FIELD VALUE (1 running, 2 health, 3 restarts)
  awk -v field="$1" -v value="$2" '{ $field = value; print }' "$CASE/docker/container" >"$CASE/container.new"
  mv "$CASE/container.new" "$CASE/docker/container"
}
age_release() { # pretend the current release started N minutes ago
  local deployed_at
  deployed_at="$(($(date +%s) - $1 * 60))"
  sed -i "s/^deployed_at=.*/deployed_at=$deployed_at/" "$SERVER/.deploy/current"
}

make_fakes

# --- Installation ------------------------------------------------------------

new_case fresh-install
rm -f "$SERVER/.env"
RUN_ENV=(BOT_TOKEN="$TOKEN" APP_NAME="Example Bot")
install_bot
RUN_ENV=()
grep -q "^BOT_TOKEN=$TOKEN\$" "$SERVER/.env" || fail "token was not written to .env"
grep -q "^APP_SLUG=$SLUG\$" "$SERVER/.env" || fail "APP_SLUG was not pinned in .env"
expect "$(state current commit)" "$(head_commit)" "current release commit"
expect "$(state current image)" "$(tag_id "$SLUG:local")" ":local tags the current image"
expect "$(running_image)" "$(state current image)" "container runs the current image"
expect "$(release_tags)" "1" "one release tag"
[[ ! -f "$SERVER/.deploy/previous" ]] || fail "first install recorded a previous release"
[[ -z "$(tag_id "$SLUG:candidate")" ]] || fail "candidate tag left behind"
grep -q "ExecStart=/usr/bin/env bash \"$SERVER/scripts/deploy.sh\"" \
  "$CASE/systemd/$SLUG-deploy.service" || fail "deploy service not rendered"
grep -q "OnCalendar=Sun \*-\*-\* 04:00:00" "$CASE/systemd/$SLUG-rebuild.timer" ||
  fail "weekly rebuild timer not rendered"
grep -q "systemctl enable --now $SLUG-deploy.timer $SLUG-rebuild.timer" "$CASE/commands.log" ||
  fail "timers were not enabled"
if [[ "$(stat -c %u "$SERVER/.git")" != "0" ]]; then
  grep -q "^runuser -u tester -- env HOME=.* git -C $SERVER" "$CASE/commands.log" ||
    fail "git did not run as the checkout owner"
fi
if grep -rqF "$TOKEN" "$CASE/commands.log" "$SERVER/logs"; then
  fail "the bot token leaked into commands or logs"
fi

new_case repeated-install
install_bot
cp "$SERVER/.env" "$CASE/env-before"
image_before="$(state current image)"
install_bot
cmp -s "$CASE/env-before" "$SERVER/.env" || fail "second install changed .env"
expect "$(count ups)" "1" "unchanged install must not restart the bot"
expect "$(state current image)" "$image_before" "unchanged install image"
expect "$(find "$CASE/systemd" -type f | wc -l | tr -d ' ')" "4" "unit count"

new_case install-reports-unhealthy-bot
install_bot
set_container 2 unhealthy
if run_tool install.sh; then fail "install succeeded while the bot is unhealthy"; fi
expect "$(count ups)" "1" "an unchanged release is not recreated by the installer"

new_case install-restarts-stopped-bot
install_bot
set_container 1 false
install_bot
expect "$(running)" "true" "install starts a stopped bot"
expect "$(count ups)" "2" "a stopped bot is started once"

new_case install-smoke-failure
commit "Broken start" "app/behavior=fail-smoke" >/dev/null
git -C "$SERVER" pull -q
if run_tool install.sh; then fail "install succeeded with a failing smoke test"; fi
expect "$(count ups)" "0" "failed first install must not start a container"
[[ ! -f "$SERVER/.deploy/current" ]] || fail "failed install recorded a release"

new_case legacy-upgrade
mkdir -p "$CASE/docker/images"
printf 'version=1\n' >"$CASE/docker/images/legacyimage"
printf '%s sha256:legacyimage\n' "$SLUG:local" "$SLUG:rollback" >"$CASE/docker/tags"
# An old image without a healthcheck is still adopted as the release to return to.
printf 'true none 0 sha256:legacyimage\n' >"$CASE/docker/container"
mkdir -p "$SERVER/data"
printf 'deadbeef\n' >"$SERVER/data/.rollback-commit"
printf 'cafebabe\n' >"$SERVER/data/.failed-deploy-sha"
install_bot
[[ -z "$(tag_id "$SLUG:rollback")" ]] || fail "legacy rollback tag kept"
[[ ! -f "$SERVER/data/.rollback-commit" ]] || fail "legacy rollback file kept"
expect "$(state previous image)" "sha256:legacyimage" "adopted release becomes previous"
expect "$(tr -d '[:space:]' <"$SERVER/.deploy/failed-commit")" "cafebabe" "legacy failed commit migrated"

# --- Deploying new commits ---------------------------------------------------

new_case deploy-new-commit
install_bot
first_image="$(state current image)"
first_commit="$(head_commit)"
deploy
expect "$(count builds)" "1" "nothing new must not build"
second_commit="$(commit "Say hello" "app/bot.py=print('version 2')")"
deploy
expect "$(head_commit)" "$second_commit" "checkout advanced"
expect "$(state current commit)" "$second_commit" "current commit"
expect "$(state previous commit)" "$first_commit" "previous commit"
expect "$(state previous image)" "$first_image" "previous image"
expect "$(running_image)" "$(state current image)" "container runs the new image"
expect "$(tag_id "$SLUG:local")" "$(state current image)" ":local follows the release"
[[ "$(state current image)" != "$first_image" ]] || fail "image did not change"
log_has "Released ${second_commit:0:7}: Say hello"
[[ ! -f "$SERVER/.deploy/pending" ]] || fail "pending marker left behind"

new_case docs-only-commit
install_bot
docs_commit="$(commit "Improve README" "README.md=# Better docs")"
deploy
expect "$(head_commit)" "$docs_commit" "docs commit checked out"
expect "$(count ups)" "1" "docs-only commit must not restart the bot"
expect "$(state current commit)" "$docs_commit" "docs commit recorded"
[[ ! -f "$SERVER/.deploy/previous" ]] || fail "docs-only commit rotated releases"
log_has "without a restart"

for failure in fail-build fail-smoke unhealthy crash; do
  new_case "failed-$failure"
  install_bot
  good_commit="$(head_commit)"
  good_image="$(state current image)"
  bad_commit="$(commit "Broken: $failure" "app/behavior=$failure")"
  deploy_fails
  expect "$(head_commit)" "$good_commit" "$failure: checkout restored"
  expect "$(running_image)" "$good_image" "$failure: previous image runs again"
  expect "$(running)" "true" "$failure: container running"
  expect "$(tag_id "$SLUG:local")" "$good_image" "$failure: :local restored"
  expect "$(state current commit)" "$good_commit" "$failure: state unchanged"
  expect "$(tr -d '[:space:]' <"$SERVER/.deploy/failed-commit")" "$bad_commit" "$failure: failed commit"
  expect "$(release_tags)" "1" "$failure: failed release tag removed"
  [[ -z "$(tag_id "$SLUG:candidate")" ]] || fail "$failure: candidate tag left"
  [[ ! -f "$SERVER/.deploy/pending" ]] || fail "$failure: pending marker left"
  case "$failure" in
    fail-build | fail-smoke) expect "$(count ups)" "1" "$failure: container untouched" ;;
    *) expect "$(count ups)" "3" "$failure: candidate started, then previous restored" ;;
  esac
done

new_case failed-commit-skipped
install_bot
bad_commit="$(commit "Broken" "app/behavior=fail-smoke")"
deploy_fails
builds="$(count builds)"
deploy
deploy
expect "$(count builds)" "$builds" "failed commit must not be rebuilt"
expect "$(log_count "failed before")" "1" "skip message logged once"
deploy_fails --retry
expect "$(count builds)" "$((builds + 1))" "--retry rebuilds the failed commit"
fixed_commit="$(commit "Fix" "-app/behavior")"
deploy
expect "$(state current commit)" "$fixed_commit" "newer commit deployed after a failure"
[[ ! -f "$SERVER/.deploy/failed-commit" ]] || fail "failed marker kept after success"

new_case broken-deploy-script
install_bot
good_commit="$(head_commit)"
commit "Break status script" "scripts/status.sh=if then" >/dev/null
deploy_fails
expect "$(head_commit)" "$good_commit" "syntax error restored checkout"
expect "$(count builds)" "1" "syntax error stops before building"
log_has "Syntax error in scripts/status.sh"

new_case rewritten-history
install_bot
git -C "$DEV" commit -q --amend -m "Rewritten initial commit"
git -C "$DEV" push -q -f origin main
deploy
deploy
expect "$(count builds)" "1" "rewritten history must not deploy"
expect "$(log_count "was rewritten")" "1" "rewrite reported once"

new_case edited-server-files
install_bot
printf '# local edit\n' >>"$SERVER/docker-compose.yml"
commit "New feature" "app/bot.py=print('v2')" >/dev/null
deploy
expect "$(count builds)" "1" "edited checkout must not deploy"
log_has "Tracked files were edited on the server"

new_case locked
install_bot
commit "New feature" "app/bot.py=print('v2')" >/dev/null
RUN_ENV=(FAKE_LOCKED=1)
deploy
if run_tool scripts/rollback.sh --yes; then fail "rollback ran while locked"; fi
RUN_ENV=()
expect "$(count builds)" "1" "locked deploy must not build"

new_case interrupted-release
install_bot
good_commit="$(head_commit)"
good_image="$(state current image)"
target="$(commit "New feature" "app/bot.py=print('v2')")"
git -C "$SERVER" fetch -q origin
git -C "$SERVER" checkout -q -B main "$target"
printf 'target=%s\ncommit=%s\nimage=%s\n' "$target" "$good_commit" "$good_image" \
  >"$SERVER/.deploy/pending"
deploy
log_has "A previous run stopped while releasing ${target:0:7}"
expect "$(state current commit)" "$target" "interrupted commit is retried once"
running_commit="$(head_commit)"
running_image_id="$(state current image)"
target="$(commit "Another feature" "app/bot.py=print('v3')")"
git -C "$SERVER" fetch -q origin
git -C "$SERVER" checkout -q -B main "$target"
printf 'target=%s\ncommit=%s\nimage=%s\n' "$target" "$running_commit" "$running_image_id" \
  >"$SERVER/.deploy/pending"
printf '%s\n' "$target" >"$SERVER/.deploy/interrupted"
deploy
expect "$(tr -d '[:space:]' <"$SERVER/.deploy/failed-commit")" "$target" \
  "twice interrupted commit is marked failed"
expect "$(head_commit)" "$running_commit" "twice interrupted commit is not checked out"
expect "$(running_image)" "$running_image_id" "the running release stays"

# --- CI gate -----------------------------------------------------------------

ci_case() {
  new_case "$1"
  mkdir -p "$DEV/.github/workflows"
  printf 'name: CI\n' >"$DEV/.github/workflows/ci.yml"
  git -C "$DEV" add -A
  git -C "$DEV" commit -q -m "Add CI"
  git -C "$DEV" push -q origin main
  git -C "$SERVER" pull -q
  install_bot
}

ci_case ci-pending-then-success
target="$(commit "Feature" "app/bot.py=print('v2')")"
printf 'pending\n' >"$CASE/ci/$target"
deploy
deploy
expect "$(count builds)" "1" "pending CI must not deploy"
expect "$(log_count "Waiting for CI checks of ${target:0:7} to finish")" "1" "wait logged once"
printf 'success\n' >"$CASE/ci/$target"
deploy
expect "$(state current commit)" "$target" "deployed after CI passed"
log_has "CI passed for ${target:0:7}"
grep -q '^url = "https://api.github.test/repos/example/test-bot/commits/' "$CASE/ci/configs" ||
  fail "GitHub repository was not derived from origin"

ci_case ci-failure
target="$(commit "Feature" "app/bot.py=print('v2')")"
printf 'failure\n' >"$CASE/ci/$target"
deploy
deploy
expect "$(count builds)" "1" "failed CI must not deploy"
expect "$(grep -c "$target" "$CASE/ci/queries")" "1" "failed CI is not re-queried every run"
log_has "CI failed for ${target:0:7}"
printf 'success\n' >"$CASE/ci/$target"
sed -i "s/^$target .*/$target 0/" "$SERVER/.deploy/ci-failed"
deploy
expect "$(state current commit)" "$target" "re-run CI success deploys"

ci_case ci-rerun
target="$(commit "Feature" "app/bot.py=print('v2')")"
printf 'rerun\n' >"$CASE/ci/$target"
deploy
expect "$(state current commit)" "$target" "latest run of each check decides"

ci_case ci-not-started
target="$(commit "Feature" "app/bot.py=print('v2')")"
printf 'none\n' >"$CASE/ci/$target"
deploy
expect "$(count builds)" "1" "CI that has not started yet is awaited"
sed -i "s/^$target .*/$target $(($(date +%s) - 31 * 60))/" "$SERVER/.deploy/ci-wait"
deploy
expect "$(state current commit)" "$target" "deployed after CI_WAIT_MINUTES"
log_has "deploying without CI"

ci_case ci-api-error
target="$(commit "Feature" "app/bot.py=print('v2')")"
printf 'error\n' >"$CASE/ci/$target"
deploy
expect "$(count builds)" "1" "unreachable GitHub is awaited"
sed -i "s/^$target .*/$target $(($(date +%s) - 31 * 60))/" "$SERVER/.deploy/ci-wait"
deploy
expect "$(state current commit)" "$target" "deployed after waiting for GitHub"

ci_case ci-skip
target="$(commit "Feature" "app/bot.py=print('v2')")"
printf 'pending\n' >"$CASE/ci/$target"
deploy --skip-ci
expect "$(state current commit)" "$target" "--skip-ci deploys immediately"

ci_case ci-token
mkdir -p "$SERVER/.deploy"
printf 'ghp_secretvalue\n' >"$SERVER/.deploy/github-token"
target="$(commit "Feature" "app/bot.py=print('v2')")"
printf 'success\n' >"$CASE/ci/$target"
deploy
grep -q 'Authorization: Bearer ghp_secretvalue' "$CASE/ci/configs" ||
  fail "GitHub token was not sent"
if grep -q ghp_secretvalue "$CASE/commands.log"; then
  fail "GitHub token appeared on a command line"
fi

new_case no-workflows-no-wait
install_bot
target="$(commit "Feature" "app/bot.py=print('v2')")"
printf 'pending\n' >"$CASE/ci/$target"
deploy
expect "$(state current commit)" "$target" "repositories without workflows deploy at once"
[[ ! -f "$CASE/ci/queries" ]] || fail "GitHub was queried without workflows"

# --- Watching fresh releases -------------------------------------------------

new_case watch-rolls-back
install_bot
good_commit="$(head_commit)"
good_image="$(state current image)"
bad_commit="$(commit "Feature" "app/bot.py=print('v2')")"
deploy
set_container 2 unhealthy
deploy
expect "$(running_image)" "$good_image" "unhealthy fresh release rolled back"
expect "$(head_commit)" "$good_commit" "checkout rolled back"
expect "$(state current commit)" "$good_commit" "state rolled back"
expect "$(state previous commit)" "$bad_commit" "bad release kept as previous"
expect "$(tr -d '[:space:]' <"$SERVER/.deploy/failed-commit")" "$bad_commit" "bad commit skipped"
log_has "became unhealthy after it started"

new_case watch-restart
install_bot
good_image="$(state current image)"
commit "Feature" "app/bot.py=print('v2')" >/dev/null
deploy
set_container 3 1
deploy
expect "$(running_image)" "$good_image" "a restart during the watch window rolls back"

new_case watch-expired
install_bot
commit "Feature" "app/bot.py=print('v2')" >/dev/null
deploy
new_image="$(state current image)"
age_release 11
set_container 2 unhealthy
deploy
expect "$(running_image)" "$new_image" "no rollback after the watch window"

new_case watch-manual-stop
install_bot
commit "Feature" "app/bot.py=print('v2')" >/dev/null
deploy
new_image="$(state current image)"
set_container 1 false
deploy
expect "$(running_image)" "$new_image" "a manually stopped bot is left alone"
expect "$(running)" "false" "a manually stopped bot stays stopped"

# --- Manual rollback ---------------------------------------------------------

new_case manual-rollback
install_bot
first_commit="$(head_commit)"
first_image="$(state current image)"
second_commit="$(commit "Feature" "app/bot.py=print('v2')")"
deploy
second_image="$(state current image)"
rollback
expect "$(head_commit)" "$first_commit" "rollback checkout"
expect "$(running_image)" "$first_image" "rollback image"
expect "$(state current commit)" "$first_commit" "rollback state"
expect "$(state previous commit)" "$second_commit" "rolled-back release kept as previous"
expect "$(tr -d '[:space:]' <"$SERVER/.deploy/failed-commit")" "$second_commit" \
  "rolled-back commit is not redeployed by the timer"
deploy
expect "$(running_image)" "$first_image" "timer keeps the rollback"
rollback
expect "$(running_image)" "$second_image" "a rollback can be undone"

new_case manual-rollback-failure
install_bot
commit "Feature" "app/bot.py=print('v2')" >/dev/null
deploy
second_commit="$(head_commit)"
second_image="$(state current image)"
printf 'unhealthy\n' >>"$CASE/docker/images/$(state previous image | sed 's/^sha256://')"
if run_tool scripts/rollback.sh --yes; then fail "rollback to an unhealthy release succeeded"; fi
expect "$(running_image)" "$second_image" "failed rollback restored the running release"
expect "$(head_commit)" "$second_commit" "failed rollback restored the checkout"
expect "$(state current commit)" "$second_commit" "failed rollback kept the state"

new_case rollback-without-previous
install_bot
if run_tool scripts/rollback.sh --yes; then fail "rollback without a previous release succeeded"; fi
grep -q "No previous release is recorded yet" "$SERVER"/logs/*-deploy-*.log ||
  fail "missing previous release was not explained"

# --- Scheduled rebuilds and pruning ------------------------------------------

new_case rebuild-same-base
install_bot
deploy --rebuild
expect "$(count ups)" "1" "same base image must not restart"
log_has "produced the same image"

new_case rebuild-new-base
install_bot
first_image="$(state current image)"
RUN_ENV=(FAKE_BASE=base-2)
deploy --rebuild
RUN_ENV=()
expect "$(count ups)" "2" "new base image restarts the bot"
[[ "$(state current image)" != "$first_image" ]] || fail "rebuild did not record the new image"
expect "$(state previous image)" "$first_image" "rebuild keeps the old image for rollback"
expect "$(state current commit)" "$(state previous commit)" "rebuild keeps the commit"

new_case rebuild-version-check
printf 'FROM python:3.12-slim\nARG REBUILD_STAMP\n' >"$DEV/Dockerfile"
sed -i 's/^REBUILD_VERSION_CMD=.*/REBUILD_VERSION_CMD="python -m yt_dlp --version"/' \
  "$DEV/deploy.conf"
git -C "$DEV" commit -q -am "Nightly library refresh"
git -C "$DEV" push -q origin main
git -C "$SERVER" pull -q
install_bot
first_image="$(state current image)"
deploy --rebuild
expect "$(count ups)" "1" "same library version must not restart"
expect "$(state current image)" "$first_image" "same version keeps the image"
[[ "$(find "$CASE/docker/images" -type f | wc -l | tr -d ' ')" == "1" ]] ||
  fail "unused rebuilt image was not removed"
RUN_ENV=(FAKE_VERSION=2)
deploy --rebuild
RUN_ENV=()
expect "$(count ups)" "2" "new library version restarts the bot"
log_has "(1 -> 2)"

new_case failed-rebuild
install_bot
good_image="$(state current image)"
RUN_ENV=(FAKE_BASE=base-2 FAKE_BUILD_FLAGS=unhealthy)
deploy_fails --rebuild
RUN_ENV=()
expect "$(running_image)" "$good_image" "a failed rebuild restores the running image"
expect "$(state current image)" "$good_image" "a failed rebuild keeps the release"
[[ ! -f "$SERVER/.deploy/failed-commit" ]] || fail "a failed rebuild blocked the commit"

new_case prune-releases
install_bot
for version in 2 3 4 5 6; do
  commit "Version $version" "app/bot.py=print('v$version')" >/dev/null
  deploy
done
expect "$(release_tags)" "4" "current plus KEEP_RELEASES=3 older releases"
expect "$(find "$CASE/docker/images" -type f | wc -l | tr -d ' ')" "4" "pruned images deleted"
[[ -n "$(tag_id "$SLUG:$(state previous tag)")" ]] || fail "previous release was pruned"

# --- systemd units follow deploy.conf ----------------------------------------

new_case units-follow-config
install_bot
sed -i 's/^REBUILD_SCHEDULE=.*/REBUILD_SCHEDULE=/' "$DEV/deploy.conf"
git -C "$DEV" commit -q -am "Disable scheduled rebuilds"
git -C "$DEV" push -q origin main
deploy
[[ ! -f "$CASE/systemd/$SLUG-rebuild.timer" ]] || fail "rebuild timer kept after disabling"
grep -q "systemctl disable --now $SLUG-rebuild.timer" "$CASE/commands.log" ||
  fail "rebuild timer was not disabled"
sed -i 's/^REBUILD_SCHEDULE=.*/REBUILD_SCHEDULE="*-*-* 03:30:00"/' "$DEV/deploy.conf"
git -C "$DEV" commit -q -am "Nightly rebuilds"
git -C "$DEV" push -q origin main
deploy
grep -q 'OnCalendar=\*-\*-\* 03:30:00' "$CASE/systemd/$SLUG-rebuild.timer" ||
  fail "nightly schedule not installed"

new_case units-failure-keeps-release
install_bot
sed -i 's/04:00:00/05:00:00/' "$DEV/deploy.conf"
git -C "$DEV" commit -q -am "Move rebuild"
git -C "$DEV" push -q origin main
RUN_ENV=(FAKE_FAIL_SYSTEMD=1)
deploy
RUN_ENV=()
grep -q 'OnCalendar=Sun \*-\*-\* 04:00:00' "$CASE/systemd/$SLUG-rebuild.timer" ||
  fail "units were not restored after a systemd failure"
log_has "systemd units could not be updated"

# --- Secrets and safety ------------------------------------------------------

new_case token-redaction
install_bot
commit "Leaky smoke" "app/bot.py=print('v2')" >/dev/null
RUN_ENV=(FAKE_SMOKE_OUTPUT="token $TOKEN")
deploy_fails
RUN_ENV=()
log_has "exposed a token"
if grep -rqF "$TOKEN" "$SERVER/logs" "$CASE/commands.log"; then
  fail "token leaked into logs"
fi

# shellcheck disable=SC2016
if grep -REn -- 'rm[[:space:]]+-rf[[:space:]]+(/|"\$ROOT_DIR"|\$ROOT_DIR)' \
  "$REPOSITORY_ROOT/install.sh" "$REPOSITORY_ROOT/scripts"; then
  CASE_NAME=static fail "destructive broad command found"
fi
if grep -Rqn -- 'git pull' "$REPOSITORY_ROOT/scripts" "$REPOSITORY_ROOT/install.sh"; then
  CASE_NAME=static fail "deployment scripts contain a blind git pull"
fi

printf 'Shell deployment tests passed.\n'
