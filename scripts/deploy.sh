#!/usr/bin/env bash
# Runs every two minutes from the <slug>-deploy.timer systemd timer, and on
# REBUILD_SCHEDULE from <slug>-rebuild.timer with --rebuild:
#
#   1. rolls a fresh release back if it turns unhealthy within WATCH_MINUTES;
#   2. deploys the newest commit of DEPLOY_BRANCH once its CI checks pass;
#   3. with --rebuild, rebuilds the running commit with fresh dependencies.
#
# Every step keeps the previous release ready and restores it on any failure.
set -Eeuo pipefail

ROOT_DIR="${ROOT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib-production.sh
source "$SCRIPT_DIR/lib-production.sh"

retry=0
skip_ci=0

usage() {
  cat <<'USAGE'
Usage: sudo bash scripts/deploy.sh [--retry | --skip-ci | --rebuild]

Deploys the newest commit of the deploy branch after its CI checks pass. The
systemd timer already runs this every two minutes; run it by hand only to act
immediately.

  --retry    try again a commit that failed before
  --skip-ci  deploy without waiting for CI checks
  --rebuild  rebuild the running commit with fresh dependencies
USAGE
}

# Rolls back a release that turns unhealthy while it is still fresh. Returns
# non-zero after a rollback so that nothing else happens in this run.
watch_fresh_release() {
  local deployed_at now running health restarts image bad good
  deployed_at="$(state_get current deployed_at)"
  [[ "$deployed_at" =~ ^[0-9]+$ ]] || return 0
  ((deployed_at > 0 && WATCH_MINUTES > 0)) || return 0
  now="$(date +%s)"
  ((now - deployed_at < WATCH_MINUTES * 60)) || return 0
  [[ -f "$(state_file previous)" ]] || return 0

  read -r running health restarts image < <(container_state) || true
  # Leave containers alone that someone stopped or replaced by hand.
  [[ "$image" == "$(state_get current image)" ]] || return 0
  if [[ ! "$restarts" =~ ^[1-9] && ! ("$running" == "true" && "$health" == "unhealthy") ]]; then
    return 0
  fi

  bad="$(state_get current commit)"
  good="$(state_get previous commit)"
  log "Release ${bad:0:7} became unhealthy after it started (health=$health restarts=$restarts); rolling back to ${good:0:7}"
  if ! restore_release "$good" "$(state_get previous image)"; then
    log "ERROR: ${good:0:7} did not become healthy either; the bot needs attention"
    return 1
  fi
  swap_current_and_previous
  printf '%s\n' "$bad" >"$(state_file failed-commit)"
  log "Rolled back to ${good:0:7}; ${bad:0:7} will be skipped until a newer commit arrives"
  return 1
}

deploy_new_commit() {
  local head target output
  if ! output="$(run_git fetch -q origin \
    "+refs/heads/$DEPLOY_BRANCH:refs/remotes/origin/$DEPLOY_BRANCH" 2>&1)"; then
    note_once fetch "Cannot fetch origin/$DEPLOY_BRANCH, will retry: ${output##*$'\n'}"
    return 0
  fi
  clear_note fetch
  head="$(run_git rev-parse HEAD)"
  target="$(run_git rev-parse "refs/remotes/origin/$DEPLOY_BRANCH")"
  if [[ "$head" == "$target" ]]; then
    clear_note ci
    clear_note skip
    clear_note history
    return 0
  fi

  if [[ "$retry" == "1" ]]; then
    state_remove failed-commit interrupted ci-failed
  fi
  if [[ "$(state_read_line failed-commit)" == "$target" ]]; then
    note_once skip "Commit ${target:0:7} failed before; waiting for a newer commit (sudo bash scripts/deploy.sh --retry tries it again)"
    return 0
  fi
  clear_note skip
  if ! run_git merge-base --is-ancestor "$head" "$target"; then
    note_once history "origin/$DEPLOY_BRANCH was rewritten and no longer contains ${head:0:7}; automatic deployment is paused"
    return 0
  fi
  clear_note history

  if [[ "$skip_ci" != "1" ]]; then
    ci_gate "$target"
    [[ "$CI_DECISION" == "pass" ]] || return 0
  fi
  log "Deploying ${target:0:7}: $(commit_subject "$target")"
  release_commit "$target" deploy
  # The new commit may change the timers, for example REBUILD_SCHEDULE.
  install_units || true
}

rebuild_running_commit() {
  local head
  head="$(run_git rev-parse HEAD)"
  if [[ "$head" != "$(state_get current commit)" ]]; then
    log "Checkout ${head:0:7} is not the running release; scheduled rebuild skipped"
    return 0
  fi
  log "Rebuilding ${head:0:7} with fresh dependencies"
  release_commit "$head" rebuild
  install_units || true
}

main() {
  local mode=deploy lock_wait=0
  case "${1:-}" in
    "") ;;
    --retry) retry=1 ;;
    --skip-ci) skip_ci=1 ;;
    --rebuild)
      mode=rebuild
      lock_wait=1800
      ;;
    -h | --help)
      usage
      return 0
      ;;
    *)
      usage >&2
      return 2
      ;;
  esac

  require_root
  resolve_app_slug
  load_deploy_config
  prepare_state_dir
  open_log
  if ! acquire_lock "$lock_wait"; then
    log "Another deployment is running; skipping this run"
    return 0
  fi
  # A paused checkout (edited files, another branch) is reported once.
  validate_checkout || return 0
  recover_interrupted_release
  adopt_running_release
  if [[ ! -f "$(state_file current)" ]]; then
    note_once release "No running release is recorded; run install.sh first"
    return 0
  fi
  clear_note release
  watch_fresh_release || return 0

  # release_commit relies on its ERR trap, so it must not run inside a condition.
  if [[ "$mode" == "rebuild" ]]; then
    rebuild_running_commit
  else
    deploy_new_commit
  fi
}

main "$@"
