#!/usr/bin/env bash
# Puts the previous release back: its commit, the exact image it ran, and the
# container. Automatic deployment then waits for the next push, and running
# this script again returns to the release that was just replaced.
set -Eeuo pipefail

ROOT_DIR="${ROOT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib-production.sh
source "$SCRIPT_DIR/lib-production.sh"

usage() {
  cat <<'USAGE'
Usage: sudo bash scripts/rollback.sh [--yes]

Returns the bot to the previous release. Use --yes to skip the confirmation.
USAGE
}

main() {
  local assume_yes=0 answer target
  local current_commit current_image previous_commit previous_image
  case "${1:-}" in
    "") ;;
    -y | --yes) assume_yes=1 ;;
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
  acquire_lock 0 || die "Another deployment is running; try again in a minute"
  validate_checkout || die "Automatic deployment is paused; fix the checkout first"
  recover_interrupted_release
  [[ -f "$(state_file previous)" ]] || die "No previous release is recorded yet"

  current_commit="$(state_get current commit)"
  current_image="$(state_get current image)"
  previous_commit="$(state_get previous commit)"
  previous_image="$(state_get previous image)"
  [[ -n "$(image_id "$previous_image")" ]] ||
    die "The image of the previous release is no longer available"
  run_git cat-file -e "$previous_commit^{commit}" 2>/dev/null ||
    die "Commit ${previous_commit:0:7} is no longer in the repository"

  printf 'Running now:  %s %s\n' "${current_commit:0:7}" "$(commit_subject "$current_commit")"
  printf 'Roll back to: %s %s\n' "${previous_commit:0:7}" "$(commit_subject "$previous_commit")"
  if [[ "$assume_yes" != "1" ]]; then
    [[ -t 0 ]] || die "Pass --yes when no terminal is attached"
    read -r -p 'Continue? [y/N] ' answer
    if [[ "${answer,,}" != "y" && "${answer,,}" != "yes" ]]; then
      log "Rollback cancelled"
      return 1
    fi
  fi

  log "Rolling back from ${current_commit:0:7} to ${previous_commit:0:7}"
  if ! restore_release "$previous_commit" "$previous_image"; then
    log "Rollback failed; returning to ${current_commit:0:7}"
    restore_release "$current_commit" "$current_image" ||
      log "ERROR: ${current_commit:0:7} did not come back either; the bot needs attention"
    return 1
  fi
  swap_current_and_previous

  # Keep the timer from reinstalling what was just rolled back; the next push
  # resumes automatic deployment.
  target="$(run_git rev-parse --verify -q "refs/remotes/origin/$DEPLOY_BRANCH" || true)"
  printf '%s\n' "${target:-$current_commit}" >"$(state_file failed-commit)"
  log "Rolled back to ${previous_commit:0:7}. The next push is deployed as usual; sudo bash scripts/deploy.sh --retry redeploys ${target:0:7} now"
}

main "$@"
