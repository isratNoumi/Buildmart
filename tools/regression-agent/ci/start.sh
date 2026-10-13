#!/usr/bin/env bash
# Start everything the comparison needs on this machine:
#   backend with a fresh seeded database  http://127.0.0.1:8000
#   current build (CURRENT_REF, React 16)  http://127.0.0.1:5173
#   new build (this checkout, React 19)    http://127.0.0.1:5174
# Stop them again with ci/stop.sh.
set -euo pipefail

REPO="$(git rev-parse --show-toplevel)"
WORK="${WORK_DIR:-${RUNNER_TEMP:-/tmp}/regression}"
CURRENT_REF="${CURRENT_REF:-origin/main}"
CURRENT_FRONTEND="${CURRENT_FRONTEND:-frontend}"
NEW_FRONTEND="${NEW_FRONTEND:-react19}"
mkdir -p "$WORK"
: > "$WORK/pids"

wait_for() {  # url name log
  for _ in $(seq 1 120); do
    if curl -fsS -o /dev/null "$1"; then echo "$2 is up: $1"; return 0; fi
    sleep 1
  done
  echo "$2 did not start. Last log lines:"; tail -n 40 "$3" || true
  exit 1
}

start() {  # log command...
  local log="$1"; shift
  nohup "$@" > "$log" 2>&1 &
  echo $! >> "$WORK/pids"
}

serve() {  # folder port name
  local dir="$1" port="$2" name="$3"
  echo "== $name build: $dir"
  (cd "$dir" && npm ci --no-audit --no-fund && npm run build)
  # Vite's own config only proxies /api in dev mode; this adds it to preview. *.local is gitignored,
  # so it never shows up as a changed file.
  cat > "$dir/vite.ci.local.mjs" <<EOF
import base from "./vite.config.js";
export default { ...base, preview: { host: "127.0.0.1", port: $port, strictPort: true,
  proxy: { "/api": "http://127.0.0.1:8000" } } };
EOF
  (cd "$dir" && start "$WORK/$name.log" npx vite preview --config vite.ci.local.mjs)
  wait_for "http://127.0.0.1:$port/" "$name build" "$WORK/$name.log"
}

echo "== Current build: $CURRENT_REF -> $WORK/current"
git -C "$REPO" worktree remove --force "$WORK/current" 2>/dev/null || rm -rf "$WORK/current"
git -C "$REPO" worktree prune
git -C "$REPO" worktree add --detach "$WORK/current" "$CURRENT_REF"

echo "== Backend with a fresh seeded database"
cd "$REPO"
[ -f .env ] || cp .env.example .env
uv sync --frozen
uv run python -m app.init_db
# The venv's own Python, not `uv run`: a long-running uv process blocks uv's cache cleanup at the end of the job.
PYTHON="$REPO/.venv/bin/python"; [ -x "$PYTHON" ] || PYTHON="$REPO/.venv/Scripts/python.exe"
start "$WORK/backend.log" "$PYTHON" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
wait_for http://127.0.0.1:8000/health backend "$WORK/backend.log"

serve "$WORK/current/$CURRENT_FRONTEND" 5173 current
serve "$REPO/$NEW_FRONTEND" 5174 new
echo "== Ready"
