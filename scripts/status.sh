#!/usr/bin/env bash
# Shows what this bot runs now, what it can roll back to, and what automatic
# deployment is waiting for. Read-only; it never changes the deployment.
set -Euo pipefail

ROOT_DIR="${ROOT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib-production.sh
source "$SCRIPT_DIR/lib-production.sh"

describe_release() {
  local name="$1" label="$2" commit deployed_at image when
  commit="$(state_get "$name" commit)"
  if [[ -z "$commit" ]]; then
    printf '%-10s none\n' "$label"
    return 0
  fi
  deployed_at="$(state_get "$name" deployed_at)"
  image="$(state_get "$name" image)"
  when="unknown time"
  if [[ "$deployed_at" =~ ^[1-9][0-9]*$ ]]; then
    when="$(date -d "@$deployed_at" '+%Y-%m-%d %H:%M %Z' 2>/dev/null || printf '%s' "$deployed_at")"
  fi
  image="${image#sha256:}"
  printf '%-10s %s %s\n' "$label" "${commit:0:7}" "$(commit_subject "$commit")"
  printf '%-10s released %s, image %s\n' "" "$when" "${image:0:12}"
}

main() {
  local running health restarts image head target failed note
  if [[ "$(id -u)" != "0" ]] && ! docker info >/dev/null 2>&1; then
    printf 'Run with sudo: sudo bash scripts/status.sh\n' >&2
    return 1
  fi
  resolve_app_slug || return 1
  load_deploy_config || return 1

  printf 'Bot:       %s (%s)\n' "$APP_SLUG" "$ROOT_DIR"
  describe_release current "Running:"
  read -r running health restarts image < <(container_state) || true
  if [[ "$running" == "absent" ]]; then
    printf '%-10s no container\n' "Container:"
  else
    printf '%-10s running=%s health=%s restarts=%s\n' "Container:" \
      "$running" "$health" "$restarts"
  fi
  describe_release previous "Previous:"

  head="$(run_git rev-parse HEAD 2>/dev/null || true)"
  target="$(run_git rev-parse --verify -q "refs/remotes/origin/$DEPLOY_BRANCH" 2>/dev/null || true)"
  if [[ -n "$target" && "$target" != "$head" ]]; then
    printf '%-10s %s %s\n' "Waiting:" "${target:0:7}" "$(commit_subject "$target")"
  else
    printf '%-10s up to date with origin/%s (as of the last check)\n' "Updates:" "$DEPLOY_BRANCH"
  fi
  failed="$(state_read_line failed-commit)"
  if [[ -n "$failed" ]]; then
    printf '%-10s %s is skipped until a newer commit arrives (--retry to force)\n' \
      "Failed:" "${failed:0:7}"
  fi
  for note in "$STATE_DIR"/note-*; do
    [[ -f "$note" ]] && printf '%-10s %s\n' "Note:" "$(<"$note")"
  done

  printf '\n'
  "$SYSTEMCTL" list-timers --all --no-pager "$APP_SLUG-deploy.timer" \
    "$APP_SLUG-rebuild.timer" 2>/dev/null || true
  printf '\nLog: %s\n' "$ROOT_DIR/logs/$APP_SLUG-deploy-$(date -u '+%Y-%m-%d').log"
}

main "$@"
