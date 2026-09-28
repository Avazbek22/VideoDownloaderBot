#!/usr/bin/env bash
# Deployment helpers shared by install.sh and the scripts in this directory.
# Everything here acts only on this checkout, its Compose project, and the
# image tags that start with this bot's APP_SLUG.

SERVICE_KEY="${SERVICE_KEY:-videodownloaderbot}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
GITHUB_API_URL="${GITHUB_API_URL:-https://api.github.com}"
STATE_DIR="$ROOT_DIR/.deploy"
# shellcheck disable=SC2034 # used by the scripts that source this file
TOKEN_REGEX='[0-9]{5,20}:[A-Za-z0-9_-]{20,128}'

CONFIG_KEYS=(
  DEPLOY_BRANCH REQUIRE_CI CI_WAIT_MINUTES REBUILD_SCHEDULE
  REBUILD_VERSION_CMD SMOKE_COMMAND WATCH_MINUTES KEEP_RELEASES
)
# Variables exported by the caller win over deploy.conf (tests, one-off runs).
declare -A CONFIG_OVERRIDES=()
for _config_key in "${CONFIG_KEYS[@]}"; do
  if [[ -n "${!_config_key+x}" ]]; then
    CONFIG_OVERRIDES[$_config_key]="${!_config_key}"
  fi
done
unset _config_key

log() {
  printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"
}

die() {
  log "ERROR: $*" >&2
  return 1
}

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

require_root() {
  [[ "$(id -u)" == "0" ]] || die "Run this script with sudo"
}

# --- Configuration -----------------------------------------------------------

env_value() {
  local name="$1" file="$2"
  [[ -f "$file" ]] || return 0
  awk -F= -v wanted="$name" '
    $0 !~ /^[[:space:]]*#/ && $1 ~ "^[[:space:]]*" wanted "[[:space:]]*$" {
      value=substr($0, index($0, "=") + 1)
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
      if ((substr(value,1,1) == "\"" && substr(value,length(value),1) == "\"") ||
          (substr(value,1,1) == "\047" && substr(value,length(value),1) == "\047")) {
        value=substr(value,2,length(value)-2)
      }
      print value
      exit
    }
  ' "$file"
}

set_env_value() {
  local name="$1" value="$2" file="$3" temporary
  temporary="$(mktemp "$file.XXXXXX")"
  ENV_WRITE_VALUE="$value" awk -v name="$name" '
    BEGIN { replaced=0 }
    $0 ~ "^[[:space:]]*" name "[[:space:]]*=" {
      if (!replaced) {
        print name "=" ENVIRON["ENV_WRITE_VALUE"]
        replaced=1
      }
      next
    }
    { print }
    END { if (!replaced) print name "=" ENVIRON["ENV_WRITE_VALUE"] }
  ' "$file" >"$temporary"
  chmod 600 "$temporary"
  mv -f "$temporary" "$file"
}

config_default() {
  case "$1" in
    DEPLOY_BRANCH) printf 'main' ;;
    REQUIRE_CI) printf 'auto' ;;
    CI_WAIT_MINUTES) printf '30' ;;
    REBUILD_SCHEDULE) printf 'Sun *-*-* 04:00:00' ;;
    WATCH_MINUTES) printf '10' ;;
    KEEP_RELEASES) printf '3' ;;
  esac
}

integer_between() {
  local name="$1" value="$2" minimum="$3" maximum="$4"
  if [[ "$value" =~ ^[0-9]+$ ]] && ((value >= minimum && value <= maximum)); then
    return 0
  fi
  die "deploy.conf: $name must be a number from $minimum to $maximum"
}

load_deploy_config() {
  local key value file="$ROOT_DIR/deploy.conf"
  for key in "${CONFIG_KEYS[@]}"; do
    if [[ -n "${CONFIG_OVERRIDES[$key]+x}" ]]; then
      value="${CONFIG_OVERRIDES[$key]}"
    elif [[ -f "$file" ]] && grep -Eq "^[[:space:]]*${key}[[:space:]]*=" "$file"; then
      value="$(env_value "$key" "$file")"
    else
      value="$(config_default "$key")"
    fi
    # An empty value disables optional features and restores other defaults.
    case "$key" in
      REBUILD_SCHEDULE | REBUILD_VERSION_CMD | SMOKE_COMMAND) ;;
      *) [[ -n "$value" ]] || value="$(config_default "$key")" ;;
    esac
    printf -v "$key" '%s' "$value"
  done

  [[ "$DEPLOY_BRANCH" =~ ^[A-Za-z0-9._/-]+$ ]] ||
    die "deploy.conf: DEPLOY_BRANCH is not a valid branch name" || return 1
  case "$REQUIRE_CI" in
    auto | yes | no) ;;
    *) die "deploy.conf: REQUIRE_CI must be auto, yes, or no" || return 1 ;;
  esac
  integer_between CI_WAIT_MINUTES "$CI_WAIT_MINUTES" 1 1440 || return 1
  integer_between WATCH_MINUTES "$WATCH_MINUTES" 0 1440 || return 1
  integer_between KEEP_RELEASES "$KEEP_RELEASES" 1 50 || return 1
  [[ "$REBUILD_SCHEDULE" =~ ^[A-Za-z0-9\ *:,./~-]*$ ]] ||
    die "deploy.conf: REBUILD_SCHEDULE is not a systemd calendar expression" || return 1
  export SMOKE_COMMAND
}

slugify() {
  printf '%s' "$1" |
    tr '[:upper:]' '[:lower:]' |
    sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//; s/-+/-/g'
}

resolve_app_slug() {
  local source
  if [[ -z "${APP_SLUG:-}" ]]; then
    APP_SLUG="$(env_value APP_SLUG "$ROOT_DIR/.env")"
  fi
  if [[ -z "$APP_SLUG" ]]; then
    source="$(env_value APP_NAME "$ROOT_DIR/.env")"
    APP_SLUG="$(slugify "${source:-$(basename "$ROOT_DIR")}")"
  fi
  [[ "$APP_SLUG" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]] ||
    die "APP_SLUG must contain only lowercase letters, digits, and hyphens" || return 1
  ((${#APP_SLUG} >= 3 && ${#APP_SLUG} <= 63)) ||
    die "APP_SLUG length must be between 3 and 63 characters" || return 1
  COMPOSE_PROJECT="$APP_SLUG"
  CURRENT_IMAGE="$APP_SLUG:local"
  CANDIDATE_IMAGE="$APP_SLUG:candidate"
  LOCK_FILE="${LOCK_FILE:-/run/lock/$APP_SLUG-deploy.lock}"
  export APP_SLUG
}

# --- Logging, locking, and state ---------------------------------------------

open_log() {
  local file
  mkdir -p "$ROOT_DIR/logs"
  find "$ROOT_DIR/logs" -maxdepth 1 -type f -name "$APP_SLUG-deploy-*.log" \
    -mtime +30 -delete 2>/dev/null || true
  file="$ROOT_DIR/logs/$APP_SLUG-deploy-$(date -u '+%Y-%m-%d').log"
  if [[ -t 1 ]]; then
    exec > >(tee -a "$file") 2>&1
  else
    exec >>"$file" 2>&1
  fi
}

# Serializes install, deploy, rebuild, and rollback of this bot. Waits up to
# the given number of seconds for a running operation (default: no waiting).
acquire_lock() {
  local wait_seconds="${1:-0}"
  command_exists flock || die "flock is required" || return 1
  mkdir -p "$(dirname "$LOCK_FILE")"
  exec 9>"$LOCK_FILE"
  if ((wait_seconds > 0)); then
    flock -w "$wait_seconds" 9
  else
    flock -n 9
  fi
}

prepare_state_dir() {
  mkdir -p "$STATE_DIR"
  chmod 0700 "$STATE_DIR"
}

state_file() {
  printf '%s/%s' "$STATE_DIR" "$1"
}

# Release records (current, previous, pending) are small key=value files.
state_get() {
  local file
  file="$(state_file "$1")"
  [[ -f "$file" ]] || return 0
  awk -F= -v key="$2" '$1 == key { print substr($0, length(key) + 2); exit }' "$file"
}

state_write() {
  local file temporary
  file="$(state_file "$1")"
  shift
  temporary="$(mktemp "$file.XXXXXX")"
  printf '%s\n' "$@" >"$temporary"
  mv -f "$temporary" "$file"
}

state_copy() {
  local source destination temporary
  source="$(state_file "$1")"
  destination="$(state_file "$2")"
  temporary="$(mktemp "$destination.XXXXXX")"
  cp "$source" "$temporary"
  mv -f "$temporary" "$destination"
}

state_remove() {
  local name
  for name in "$@"; do
    rm -f -- "$(state_file "$name")"
  done
}

# After a rollback the previous release runs again; the release it replaced
# becomes the previous one, so a rollback can itself be undone.
swap_current_and_previous() {
  local replaced
  replaced="$(mktemp "$STATE_DIR/replaced.XXXXXX")"
  cp "$(state_file current)" "$replaced"
  state_write current "commit=$(state_get previous commit)" \
    "image=$(state_get previous image)" "tag=$(state_get previous tag)" "deployed_at=0"
  mv -f "$replaced" "$(state_file previous)"
}

state_read_line() {
  local file
  file="$(state_file "$1")"
  [[ -f "$file" ]] || return 0
  tr -d '[:space:]' <"$file"
}

# Log a recurring condition once instead of on every timer run.
note_once() {
  local file message="$2"
  file="$(state_file "note-$1")"
  if [[ -f "$file" && "$(<"$file")" == "$message" ]]; then
    return 0
  fi
  printf '%s\n' "$message" >"$file"
  log "$message"
}

clear_note() {
  rm -f -- "$(state_file "note-$1")"
}

# --- Git ---------------------------------------------------------------------

GIT_OWNER_RESOLVED=0
GIT_OWNER=""
GIT_OWNER_HOME=""

# Git refuses to work in a repository owned by another user, and root-owned
# files would break the owner's own git commands later. When root operates on
# a checkout cloned by a regular user, run git as that user instead.
resolve_git_owner() {
  local uid entry
  GIT_OWNER_RESOLVED=1
  [[ "$(id -u)" == "0" ]] || return 0
  uid="$(stat -c %u "$ROOT_DIR/.git" 2>/dev/null)" || return 0
  [[ "$uid" != "0" ]] || return 0
  entry="$(getent passwd "$uid")" ||
    die "Cannot find the user who owns $ROOT_DIR/.git" || return 1
  GIT_OWNER="$(cut -d: -f1 <<<"$entry")"
  GIT_OWNER_HOME="$(cut -d: -f6 <<<"$entry")"
}

run_git() {
  if [[ "$GIT_OWNER_RESOLVED" != "1" ]]; then
    resolve_git_owner || return 1
  fi
  if [[ -n "$GIT_OWNER" ]]; then
    runuser -u "$GIT_OWNER" -- env HOME="$GIT_OWNER_HOME" git -C "$ROOT_DIR" "$@"
  else
    git -C "$ROOT_DIR" "$@"
  fi
}

commit_subject() {
  run_git log -1 --format=%s "$1" 2>/dev/null | cut -c1-72 || true
}

validate_checkout() {
  local branch
  [[ -d "$ROOT_DIR/.git" ]] || die "Not a Git checkout: $ROOT_DIR" || return 1
  [[ -f "$ROOT_DIR/.env" ]] || die "Missing $ROOT_DIR/.env; run install.sh" || return 1
  branch="$(run_git branch --show-current)" || return 1
  if [[ "$branch" != "$DEPLOY_BRANCH" ]]; then
    note_once checkout "Checkout is on branch '$branch', not $DEPLOY_BRANCH; automatic deployment is paused"
    return 1
  fi
  if [[ -n "$(run_git status --porcelain --untracked-files=no)" ]]; then
    note_once checkout "Tracked files were edited on the server; automatic deployment is paused until they are restored"
    return 1
  fi
  clear_note checkout
}

# --- Docker ------------------------------------------------------------------

compose() {
  if docker compose version >/dev/null 2>&1; then
    docker compose "$@"
  elif command_exists docker-compose; then
    docker-compose "$@"
  else
    die "Docker Compose is unavailable"
  fi
}

app_compose() {
  compose -p "$COMPOSE_PROJECT" -f "$ROOT_DIR/docker-compose.yml" "$@"
}

image_id() {
  docker image inspect --format '{{.Id}}' "$1" 2>/dev/null || true
}

# Prints "<running> <health> <restarts> <image-id>", or "absent".
container_state() {
  local id
  id="$(app_compose ps -a -q "$SERVICE_KEY" 2>/dev/null | head -n 1)" || id=""
  if [[ -z "$id" ]]; then
    printf 'absent\n'
    return 0
  fi
  docker inspect --format \
    '{{.State.Running}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} {{.RestartCount}} {{.Image}}' \
    "$id" 2>/dev/null || printf 'absent\n'
}

# Succeeds once the container runs the expected image, reports healthy, and
# has not restarted for several consecutive checks. Fails fast on a crash or
# an unhealthy report.
wait_until_stable() {
  local expected="$1" running health restarts image
  local attempt stable=0
  local attempts="${HEALTH_ATTEMPTS:-75}"
  local required="${HEALTH_STABLE_COUNT:-5}"
  local interval="${HEALTH_INTERVAL_SECONDS:-2}"

  for ((attempt = 1; attempt <= attempts; attempt++)); do
    read -r running health restarts image < <(container_state) || true
    if [[ "$running" == "true" && "$health" == "healthy" &&
      "$restarts" == "0" && "$image" == "$expected" ]]; then
      stable=$((stable + 1))
      if ((stable >= required)); then
        return 0
      fi
    elif [[ "$restarts" =~ ^[1-9] || "$health" == "unhealthy" ]]; then
      log "Container is failing (running=$running health=$health restarts=$restarts)"
      return 1
    else
      stable=0
    fi
    sleep "$interval"
  done
  log "Container did not become healthy in time (running=${running:-?} health=${health:-?} restarts=${restarts:-?})"
  return 1
}

# Makes the given release the running one: its commit checked out, its image
# tagged :local, and the container recreated when it runs something else.
restore_release() {
  local commit="$1" image="$2" running health restarts current
  if [[ -n "$commit" ]]; then
    run_git checkout -q -B "$DEPLOY_BRANCH" "$commit" ||
      log "WARNING: could not check out $commit"
  fi
  if [[ -z "$image" ]]; then
    app_compose stop "$SERVICE_KEY" >/dev/null 2>&1 ||
      log "WARNING: could not stop the failed container"
    return 0
  fi
  docker image tag "$image" "$CURRENT_IMAGE" ||
    { log "WARNING: previous image $image is missing"; return 1; }
  read -r running health restarts current < <(container_state) || true
  if [[ "$running" == "true" && "$current" == "$image" && "$restarts" == "0" &&
    "$health" != "unhealthy" ]]; then
    return 0
  fi
  app_compose up -d --no-deps --force-recreate "$SERVICE_KEY" ||
    { log "WARNING: could not recreate the previous container"; return 1; }
  wait_until_stable "$image" ||
    { log "WARNING: the previous release did not become healthy again"; return 1; }
}

new_release_tag() {
  printf 'r-%s-%s' "$(date -u '+%Y%m%d-%H%M%S')" "${1:0:12}"
}

# Keeps the current release, the previous one, and the newest older releases
# up to KEEP_RELEASES; every other image of this bot is removed.
prune_releases() {
  local tag current_tag previous_tag budget="$KEEP_RELEASES"
  current_tag="$(state_get current tag)"
  previous_tag="$(state_get previous tag)"
  [[ -z "$previous_tag" ]] || budget=$((budget - 1))
  while IFS= read -r tag; do
    [[ "$tag" == r-* && "$tag" != "$current_tag" && "$tag" != "$previous_tag" ]] ||
      continue
    if ((budget > 0)); then
      budget=$((budget - 1))
      continue
    fi
    docker image rm "$APP_SLUG:$tag" >/dev/null 2>&1 ||
      log "Could not remove old release image $APP_SLUG:$tag"
  done < <(docker image ls --format '{{.Tag}}' "$APP_SLUG" 2>/dev/null | sort -r)
}

remove_image_tag() {
  if [[ -n "$(image_id "$1")" ]]; then
    docker image rm "$1" >/dev/null 2>&1 || log "Could not remove image tag $1"
  fi
}

# --- CI gate -----------------------------------------------------------------

github_repository() {
  local url path
  url="$(run_git config --get remote.origin.url 2>/dev/null)" || return 1
  case "$url" in
    https://github.com/*) path="${url#https://github.com/}" ;;
    https://*@github.com/*) path="${url#*@github.com/}" ;;
    git@github.com:*) path="${url#git@github.com:}" ;;
    ssh://git@github.com/*) path="${url#ssh://git@github.com/}" ;;
    *) return 1 ;;
  esac
  path="${path%/}"
  path="${path%.git}"
  [[ "$path" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || return 1
  printf '%s\n' "$path"
}

github_api() {
  local token_file="$STATE_DIR/github-token"
  {
    printf 'url = "%s/%s"\n' "$GITHUB_API_URL" "$1"
    printf 'header = "Accept: application/vnd.github+json"\n'
    printf 'header = "X-GitHub-Api-Version: 2022-11-28"\n'
    printf 'user-agent = "%s-autodeploy"\n' "$APP_SLUG"
    if [[ -s "$token_file" ]]; then
      printf 'header = "Authorization: Bearer %s"\n' "$(tr -d '[:space:]' <"$token_file")"
    fi
  } | curl --silent --show-error --fail --max-time 20 --config -
}

# Reduces the latest run of every check to success, failure, pending, or
# nochecks (CI has not started yet).
summarize_check_runs() {
  python3 -c '
import json
import sys

BAD = {"failure", "cancelled", "timed_out", "action_required", "startup_failure", "stale"}
runs = json.load(sys.stdin).get("check_runs") or []
latest = {}
for run in runs:
    name = run.get("name") or ""
    if name not in latest or run.get("id", 0) > latest[name].get("id", 0):
        latest[name] = run
if not latest:
    print("nochecks")
elif any(run.get("status") != "completed" for run in latest.values()):
    print("pending")
elif any(run.get("conclusion") in BAD for run in latest.values()):
    print("failure")
else:
    print("success")
'
}

# Prints success, failure, pending, none (no CI to wait for), nochecks (CI has
# not started yet), or unknown (GitHub could not be asked).
ci_status() {
  local sha="$1" repository response status recorded_sha recorded_at now
  if [[ "$REQUIRE_CI" == "no" ]]; then
    printf 'none\n'
    return 0
  fi
  if [[ "$REQUIRE_CI" == "auto" ]] &&
    ! run_git ls-tree -r --name-only "$sha" -- .github/workflows |
    grep -Eq '\.ya?ml$'; then
    printf 'none\n'
    return 0
  fi
  if ! repository="$(github_repository)"; then
    [[ "$REQUIRE_CI" == "yes" ]] && printf 'unknown\n' || printf 'none\n'
    return 0
  fi
  # A failed commit is re-checked every ten minutes in case CI is re-run.
  read -r recorded_sha recorded_at < <(cat "$(state_file ci-failed)" 2>/dev/null) || true
  now="$(date +%s)"
  if [[ "${recorded_sha:-}" == "$sha" && "${recorded_at:-}" =~ ^[0-9]+$ ]] &&
    ((now - recorded_at < 600)); then
    printf 'failure\n'
    return 0
  fi
  if ! response="$(github_api "repos/$repository/commits/$sha/check-runs?per_page=100" 2>/dev/null)" ||
    ! status="$(summarize_check_runs <<<"$response" 2>/dev/null)"; then
    printf 'unknown\n'
    return 0
  fi
  if [[ "$status" == "failure" ]]; then
    printf '%s %s\n' "$sha" "$now" >"$(state_file ci-failed)"
  fi
  printf '%s\n' "$status"
}

# True once CI has had CI_WAIT_MINUTES to start for this commit.
ci_wait_expired() {
  local sha="$1" recorded_sha first_seen now
  now="$(date +%s)"
  read -r recorded_sha first_seen < <(cat "$(state_file ci-wait)" 2>/dev/null) || true
  if [[ "${recorded_sha:-}" != "$sha" || ! "${first_seen:-}" =~ ^[0-9]+$ ]]; then
    state_write ci-wait "$sha $now"
    return 1
  fi
  ((now - first_seen >= CI_WAIT_MINUTES * 60))
}

# Sets CI_DECISION to pass, wait, or failed for the given commit.
ci_gate() {
  local sha="$1" short="${1:0:7}" status
  CI_DECISION="pass"
  status="$(ci_status "$sha")"
  case "$status" in
    none) ;;
    success)
      log "CI passed for $short"
      ;;
    pending)
      note_once ci "Waiting for CI checks of $short to finish"
      CI_DECISION="wait"
      ;;
    failure)
      note_once ci "CI failed for $short; it will not be deployed"
      CI_DECISION="failed"
      ;;
    nochecks | unknown)
      if ci_wait_expired "$sha"; then
        log "No CI result for $short after $CI_WAIT_MINUTES minutes; deploying without CI"
      else
        note_once ci "Waiting for CI checks of $short to start"
        CI_DECISION="wait"
      fi
      ;;
  esac
  if [[ "$CI_DECISION" == "pass" ]]; then
    clear_note ci
    state_remove ci-wait ci-failed
  fi
}

# --- systemd -----------------------------------------------------------------

unit_file() {
  printf '%s/%s-%s.%s' "$SYSTEMD_DIR" "$APP_SLUG" "$1" "$2"
}

sed_escape() {
  local value="${1//\\/\\\\}"
  value="${value//&/\\&}"
  printf '%s' "${value//|/\\|}"
}

render_unit() {
  sed \
    -e "s|__INSTALL_DIR__|$(sed_escape "$ROOT_DIR")|g" \
    -e "s|__APP_SLUG__|$(sed_escape "$APP_SLUG")|g" \
    -e "s|__REBUILD_SCHEDULE__|$(sed_escape "$REBUILD_SCHEDULE")|g" \
    "$1" >"$2"
}

# Installs this bot's deploy timer and, when REBUILD_SCHEDULE is set, its
# rebuild timer. Unchanged units are left alone; a failure restores them.
install_units() {
  local kind ext target rendered backup name changed=0
  local -a restart_timers=()
  mkdir -p "$SYSTEMD_DIR" || return 1
  backup="$(mktemp -d)" || return 1
  for kind in deploy rebuild; do
    for ext in service timer; do
      target="$(unit_file "$kind" "$ext")"
      [[ ! -f "$target" ]] || cp "$target" "$backup/" || return 1
    done
  done

  for kind in deploy rebuild; do
    for ext in service timer; do
      target="$(unit_file "$kind" "$ext")"
      name="$(basename "$target")"
      if [[ "$kind" == "rebuild" && -z "$REBUILD_SCHEDULE" ]]; then
        if [[ -f "$target" ]]; then
          if [[ "$ext" == "timer" ]]; then
            "$SYSTEMCTL" disable --now "$name" >/dev/null 2>&1 || true
          fi
          rm -f "$target"
          changed=1
        fi
        continue
      fi
      rendered="$(mktemp)" || return 1
      render_unit "$ROOT_DIR/scripts/systemd/telegram-bot-$kind.$ext" "$rendered" || {
        rm -f "$rendered"
        return 1
      }
      if ! cmp -s "$rendered" "$target"; then
        if ! install -m 0644 "$rendered" "$target"; then
          rm -f "$rendered"
          restore_units "$backup"
          return 1
        fi
        changed=1
        [[ "$ext" != "timer" || ! -f "$backup/$name" ]] || restart_timers+=("$name")
      fi
      rm -f "$rendered"
    done
  done

  if [[ "$changed" == "1" ]] && ! "$SYSTEMCTL" daemon-reload; then
    restore_units "$backup"
    return 1
  fi
  local -a timers=("$APP_SLUG-deploy.timer")
  [[ -z "$REBUILD_SCHEDULE" ]] || timers+=("$APP_SLUG-rebuild.timer")
  if ! "$SYSTEMCTL" enable --now "${timers[@]}"; then
    restore_units "$backup"
    return 1
  fi
  if ((${#restart_timers[@]} > 0)); then
    "$SYSTEMCTL" restart "${restart_timers[@]}" || true
  fi
  rm -rf -- "$backup"
}

restore_units() {
  local backup="$1" kind ext target name
  for kind in deploy rebuild; do
    for ext in service timer; do
      target="$(unit_file "$kind" "$ext")"
      name="$(basename "$target")"
      if [[ -f "$backup/$name" ]]; then
        install -m 0644 "$backup/$name" "$target" || true
      else
        rm -f "$target"
      fi
    done
  done
  "$SYSTEMCTL" daemon-reload || true
  rm -rf -- "$backup"
  log "WARNING: systemd units could not be updated; the previous units were restored"
}

# --- Releases ----------------------------------------------------------------
#
# A release is a commit plus the exact image built from it. The current and
# previous releases are recorded in .deploy/, every release image keeps its own
# r-<time>-<commit> tag, and :local always points at the running release so
# manual `docker compose` commands keep working.

RELEASE_TARGET=""
RELEASE_KIND=""
RELEASE_FROM_COMMIT=""
RELEASE_FROM_IMAGE=""
RELEASE_STAGE=""
RELEASE_REPLACED=0
RELEASE_TAG=""

container_runs() {
  local running health restarts image
  read -r running health restarts image < <(container_state) || true
  [[ "$running" == "true" && "$image" == "$1" ]]
}

check_scripts_syntax() {
  local script
  for script in "$ROOT_DIR/install.sh" "$ROOT_DIR"/scripts/*.sh; do
    bash -n "$script" || {
      log "Syntax error in ${script#"$ROOT_DIR"/}"
      return 1
    }
  done
}

build_candidate() {
  local -a arguments=(build --pull)
  if [[ "$RELEASE_KIND" == "rebuild" ]] &&
    grep -Eq '^[[:space:]]*ARG[[:space:]]+REBUILD_STAMP' "$ROOT_DIR/Dockerfile"; then
    arguments+=(--build-arg "REBUILD_STAMP=$(date -u '+%Y%m%dT%H%M%SZ')")
  fi
  APP_IMAGE_TAG=candidate app_compose "${arguments[@]}" "$SERVICE_KEY"
}

image_version() {
  docker run --rm --network none --entrypoint sh "$1" -c "$REBUILD_VERSION_CMD" 2>/dev/null
}

# Builds, checks, and starts a commit as the new release. Kinds: install,
# deploy (a new commit), rebuild (the same commit with fresh dependencies).
# Any failure runs abort_release, which restores the previous release.
release_commit() {
  local target="$1" head candidate old_version="" new_version=""
  RELEASE_TARGET="$target"
  RELEASE_KIND="$2"
  RELEASE_FROM_COMMIT="$(state_get current commit)"
  RELEASE_FROM_IMAGE="$(state_get current image)"
  RELEASE_REPLACED=0
  RELEASE_TAG=""
  RELEASE_STAGE="checkout"
  head="$(run_git rev-parse HEAD)"
  [[ -n "$RELEASE_FROM_COMMIT" ]] || RELEASE_FROM_COMMIT="$head"

  state_write pending "target=$target" "commit=$RELEASE_FROM_COMMIT" \
    "image=$RELEASE_FROM_IMAGE"
  trap 'abort_release $?' ERR INT TERM

  if [[ "$head" != "$target" ]]; then
    run_git checkout -q -B "$DEPLOY_BRANCH" "$target"
    # From here on the new commit's deploy.conf applies.
    load_deploy_config
  fi
  RELEASE_STAGE="script check"
  check_scripts_syntax
  RELEASE_STAGE="build"
  build_candidate
  candidate="$(image_id "$CANDIDATE_IMAGE")"
  [[ -n "$candidate" ]]

  if [[ "$candidate" == "$RELEASE_FROM_IMAGE" ]]; then
    # An installer run always ends with a running, healthy bot.
    if [[ "$RELEASE_KIND" == "install" ]]; then
      if ! container_runs "$candidate"; then
        RELEASE_STAGE="start"
        RELEASE_REPLACED=1
        app_compose up -d --no-deps --force-recreate "$SERVICE_KEY"
      fi
      RELEASE_STAGE="health check"
      wait_until_stable "$candidate"
    fi
    finish_release "$candidate" unchanged
    return 0
  fi
  if [[ "$RELEASE_KIND" == "rebuild" && -n "$REBUILD_VERSION_CMD" &&
    -n "$RELEASE_FROM_IMAGE" ]]; then
    RELEASE_STAGE="version check"
    old_version="$(image_version "$RELEASE_FROM_IMAGE")"
    new_version="$(image_version "$candidate")"
    if [[ "$old_version" == "$new_version" ]]; then
      finish_release "$candidate" same-version "$new_version"
      return 0
    fi
  fi

  RELEASE_STAGE="smoke test"
  APP_IMAGE_TAG=candidate bash "$ROOT_DIR/scripts/smoke-test.sh"
  RELEASE_STAGE="start"
  RELEASE_TAG="$(new_release_tag "$target")"
  docker image tag "$candidate" "$APP_SLUG:$RELEASE_TAG"
  docker image tag "$candidate" "$CURRENT_IMAGE"
  RELEASE_REPLACED=1
  app_compose up -d --no-deps --force-recreate "$SERVICE_KEY"
  RELEASE_STAGE="health check"
  wait_until_stable "$candidate"
  if [[ -n "$new_version" ]]; then
    finish_release "$candidate" released "$old_version -> $new_version"
  else
    finish_release "$candidate" released
  fi
}

finish_release() {
  local candidate="$1" outcome="$2" detail="${3:-}" short="${RELEASE_TARGET:0:7}"
  local subject
  subject="$(commit_subject "$RELEASE_TARGET")"
  case "$outcome" in
    released)
      if [[ -f "$(state_file current)" ]]; then
        state_copy current previous
      fi
      state_write current "commit=$RELEASE_TARGET" "image=$candidate" \
        "tag=$RELEASE_TAG" "deployed_at=$(date +%s)"
      ;;
    unchanged)
      state_write current "commit=$RELEASE_TARGET" "image=$candidate" \
        "tag=$(state_get current tag)" "deployed_at=$(state_get current deployed_at)"
      ;;
  esac
  remove_image_tag "$CANDIDATE_IMAGE"
  state_remove pending interrupted
  # A newer deployed commit supersedes a failed one; install and rebuild only
  # clear the marker for the commit they released.
  if [[ "$RELEASE_KIND" == "deploy" ||
    "$(state_read_line failed-commit)" == "$RELEASE_TARGET" ]]; then
    state_remove failed-commit
  fi
  trap - ERR INT TERM

  case "$RELEASE_KIND:$outcome" in
    rebuild:released) log "Rebuilt $short with fresh dependencies${detail:+ ($detail)}" ;;
    rebuild:same-version) log "Rebuild of $short kept version $detail; the running bot was left alone" ;;
    rebuild:unchanged) log "Rebuild of $short produced the same image; the running bot was left alone" ;;
    install:unchanged) log "Release $short is running: $subject" ;;
    *:unchanged) log "Deployed $short without a restart; nothing inside the container changed: $subject" ;;
    *) log "Released $short: $subject" ;;
  esac
  prune_releases
}

abort_release() {
  local code="${1:-1}" short="${RELEASE_TARGET:0:7}"
  trap - ERR INT TERM
  ((code != 0)) || code=1
  log "Release of $short failed during the $RELEASE_STAGE; restoring ${RELEASE_FROM_COMMIT:0:7}"
  if [[ "$RELEASE_REPLACED" == "1" || -z "$RELEASE_FROM_IMAGE" ]]; then
    restore_release "$RELEASE_FROM_COMMIT" "$RELEASE_FROM_IMAGE" || true
  else
    # The running container was never touched; only undo the checkout.
    run_git checkout -q -B "$DEPLOY_BRANCH" "$RELEASE_FROM_COMMIT" ||
      log "WARNING: could not check out ${RELEASE_FROM_COMMIT:0:7}"
    docker image tag "$RELEASE_FROM_IMAGE" "$CURRENT_IMAGE" ||
      log "WARNING: could not restore $CURRENT_IMAGE"
  fi
  remove_image_tag "$CANDIDATE_IMAGE"
  [[ -z "$RELEASE_TAG" ]] || remove_image_tag "$APP_SLUG:$RELEASE_TAG"
  if [[ "$RELEASE_KIND" == "deploy" ]]; then
    printf '%s\n' "$RELEASE_TARGET" >"$(state_file failed-commit)"
    log "Commit $short will be skipped until a newer commit arrives (sudo bash scripts/deploy.sh --retry tries it again)"
  fi
  state_remove pending
  exit "$code"
}

# A run killed mid-release (power loss, SIGKILL) leaves .deploy/pending behind.
# Put the previous release back; a commit interrupted twice is treated as failed.
recover_interrupted_release() {
  local target commit image tag current_tag previous_tag
  [[ -f "$(state_file pending)" ]] || return 0
  target="$(state_get pending target)"
  commit="$(state_get pending commit)"
  image="$(state_get pending image)"
  current_tag="$(state_get current tag)"
  previous_tag="$(state_get previous tag)"
  log "A previous run stopped while releasing ${target:0:7}; restoring ${commit:0:7}"
  restore_release "$commit" "$image" ||
    log "WARNING: the previous release could not be fully restored"
  remove_image_tag "$CANDIDATE_IMAGE"
  while IFS= read -r tag; do
    if [[ "$tag" == r-*"-${target:0:12}" && "$tag" != "$current_tag" &&
      "$tag" != "$previous_tag" ]]; then
      remove_image_tag "$APP_SLUG:$tag"
    fi
  done < <(docker image ls --format '{{.Tag}}' "$APP_SLUG" 2>/dev/null)
  if [[ "$(state_read_line interrupted)" == "$target" ]]; then
    printf '%s\n' "$target" >"$(state_file failed-commit)"
    state_remove interrupted
    log "Release of ${target:0:7} was interrupted twice; it will not be retried automatically"
  else
    printf '%s\n' "$target" >"$(state_file interrupted)"
  fi
  state_remove pending
}

# Records a running container that has no release record yet, for example
# after upgrading from an older version of these scripts, so that a failed
# release can return to it instead of leaving the bot stopped.
adopt_running_release() {
  local running health restarts image commit tag
  [[ ! -f "$(state_file current)" ]] || return 0
  read -r running health restarts image < <(container_state) || true
  [[ "$running" == "true" && -n "$image" ]] || return 0
  commit="$(run_git rev-parse HEAD)" || return 1
  tag="$(new_release_tag "$commit")"
  docker image tag "$image" "$APP_SLUG:$tag" || return 1
  docker image tag "$image" "$CURRENT_IMAGE" || return 1
  state_write current "commit=$commit" "image=$image" "tag=$tag" "deployed_at=0"
  log "Recorded the running container as release ${commit:0:7}"
}
