#!/usr/bin/env bash
# Stop what ci/start.sh started and remove the current-build checkout.
set -uo pipefail

REPO="$(git rev-parse --show-toplevel)"
WORK="${WORK_DIR:-${RUNNER_TEMP:-/tmp}/regression}"
if [[ -f "$WORK/pids" ]]; then
  while read -r pid; do
    pkill -P "$pid" 2>/dev/null
    kill "$pid" 2>/dev/null
  done < "$WORK/pids"
  rm -f "$WORK/pids"
fi
git -C "$REPO" worktree remove --force "$WORK/current" 2>/dev/null
git -C "$REPO" worktree prune
echo "Stopped."
