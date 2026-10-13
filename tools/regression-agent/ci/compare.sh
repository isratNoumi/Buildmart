#!/usr/bin/env bash
# Pick the pages the changes can affect, compare the current and the new build, and write the summary.
#   BEFORE  commit to compare against (push: the previous commit; pull request: the base).
#           Empty, all zeros or unknown -> every page is checked.
# Exit code: 0 also when differences or regressions are found (warn only); 1 if the run itself broke.
set -uo pipefail

REPO="$(git rev-parse --show-toplevel)"
TOOL="$REPO/tools/regression-agent"
NEW_FRONTEND="${NEW_FRONTEND:-react19}"
OUT="${REGRESSION_OUTPUT_DIR:-$TOOL/output}/parity_runs/$(date -u +%Y-%m-%dT%H-%M-%S)"
export TEST_USER_EMAIL="${TEST_USER_EMAIL:-sarah.miller@example.com}"
export TEST_USER_PASSWORD="${TEST_USER_PASSWORD:-${BUILDMART_DEMO_PASSWORD:-}}"

cd "$TOOL"
uv sync --frozen || exit 1
if [[ -n "${CI:-}" ]]; then
  uv run playwright install --with-deps chromium || exit 1
else
  uv run playwright install chromium || exit 1
fi

BEFORE="${BEFORE:-}"
if [[ -z "$BEFORE" || "$BEFORE" =~ ^0+$ ]] || ! git -C "$REPO" cat-file -e "$BEFORE^{commit}" 2>/dev/null; then
  echo "No earlier commit to compare with: checking every page."
  SCOPE=(--all --source-dir "$REPO/$NEW_FRONTEND")
else
  echo "Checking pages affected by changes since $BEFORE"
  SCOPE=(--base-ref "$BEFORE" --repo-root "$REPO" --source-dir "$REPO/$NEW_FRONTEND")
fi

uv run python -m agents.parity run \
  --site-profile baseline/site_profiles/buildmart.yaml \
  --current-url http://127.0.0.1:5173 --candidate-url http://127.0.0.1:5174 \
  --current-label "${CURRENT_LABEL:-main}" \
  --candidate-label "${CANDIDATE_LABEL:-$(git -C "$REPO" rev-parse --short HEAD)}" \
  --no-llm "${SCOPE[@]}" --output-dir "$OUT"
status=$?

if [[ -f "$OUT/report.json" ]]; then
  uv run python -m agents.parity summary-md "$OUT" | tee -a "${GITHUB_STEP_SUMMARY:-/dev/null}"
fi
if [[ $status -gt 1 || ! -f "$OUT/report.json" ]]; then
  echo "The comparison itself failed (exit $status)."
  exit 1
fi
exit 0
