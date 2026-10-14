# Build Mart

Regional home-improvement ecommerce application built with FastAPI, SQLAlchemy, SQLite, and a React frontend.

## Backend setup

Install dependencies and create the local virtual environment:

```powershell
uv sync
```



```powershell
Copy-Item .env.example .env
```

Initialize or reset the SQLite database from `schema.sql`:

```powershell
uv run python -m app.init_db
```

Run the API:

```powershell
uv run python -m app.main
```

The health endpoint is available at `http://127.0.0.1:8000/health`.



## Project layout

```text
backend/app/       FastAPI application and database utilities
schema.sql         SQLite schema and seed data
ecommerce.db       Local development database generated from schema.sql
react19/           Partial React 19 frontend (home page only), run with `npm run dev` (Vite, port 5174)
docs/              Feature documentation
.env.example       Template for the gitignored .env settings file
pyproject.toml     uv/Python project configuration for the ecommerce app (backend + tests)
tools/regression-agent/  Regression agent that compares the current build with the React 19 build
```

## Regression agent

The regression agent lives in `tools/regression-agent` and has its own uv project. Run every command below from that folder:

```powershell
cd tools/regression-agent
uv sync
uv run playwright install chromium
```

Discovery and the AI judge need `ANTHROPIC_API_KEY` in `tools/regression-agent/.env`. You can skip the judge with `--no-llm`.

The commands assume these default ports:

| Service | URL |
|---------|-----|
| Backend | `http://127.0.0.1:8000` |
| Current build | `http://127.0.0.1:5173` |
| New build (`react19`) | `http://127.0.0.1:5174` |

### 1. Discovery

Discovery learns the app and writes `baseline/discovery/`.

```powershell
# Static analysis plus a live run (uses the LLM)
uv run python -m agents.discovery --project-path ../../react19 --url http://127.0.0.1:5174 --backend-url http://127.0.0.1:8000 --mode full --max-turns 60 --max-budget-usd 2.0

# Source code only, or the browser only
uv run python -m agents.discovery --project-path ../../react19 --mode static_only
uv run python -m agents.discovery --url http://127.0.0.1:5174 --mode runtime_only

# Fill gaps in the page contracts from existing discovery results
uv run python -m agents.discovery --mode contracts --site-profile baseline/site_profiles/buildmart.yaml
```

Run logs go to `output/discovery_runs/<timestamp>/`.

### 2. Page contracts

```powershell
uv run python -m agents.discovery.contracts skeleton           # build draft contracts from discovery
uv run python -m agents.discovery.contracts show               # one line per contract
uv run python -m agents.discovery.contracts show home          # regions and gaps of one contract
uv run python -m agents.discovery.contracts approve home       # approve a reviewed contract
uv run python -m agents.discovery.contracts approve home --allow-gaps
```

### 3. Test planner

The planner generates journeys and selects which pages and journeys to run.

```powershell
# Generate journey specs in baseline/journeys/ from the discovery flows
uv run python -m agents.planner

# Find the pages and journeys affected by changes since a git ref, or by specific files
uv run python -m agents.planner.select --base-ref main --repo-root ../.. --output selection.json
uv run python -m agents.planner.select --changed-file react19/src/components/Header.jsx --repo-root ../..
```

### 4. Journeys

```powershell
uv run python -m agents.e2e.journeys validate                  # check specs against the schema and registry
uv run python -m agents.e2e.journeys list --area home --status approved
uv run python -m agents.e2e.journeys retire J001 --reason "flow removed"

# Run journeys against one build (add --selection-report selection.json to run only the selected ones)
uv run python -m agents.e2e.run_journeys --base-url http://127.0.0.1:5174 --app-root ../../react19 --headless
```

### 5. Parity (current build vs new build)

Parity compares the two builds page by page.

```powershell
# Every page, with no AI judge
uv run python -m agents.parity run --site-profile baseline/site_profiles/buildmart.yaml --current-url http://127.0.0.1:5173 --candidate-url http://127.0.0.1:5174 --all --no-llm

# Only pages affected since main, skipping pages react19 doesn't have yet
uv run python -m agents.parity run --site-profile baseline/site_profiles/buildmart.yaml --current-url http://127.0.0.1:5173 --candidate-url http://127.0.0.1:5174 --base-ref main --repo-root ../.. --source-dir ../../react19

# One page, with a visible browser
uv run python -m agents.parity run --site-profile baseline/site_profiles/buildmart.yaml --current-url http://127.0.0.1:5173 --candidate-url http://127.0.0.1:5174 --all --page home --headed --skip-journeys

# Label a diff by its fingerprint (expected | cosmetic | regression), then print a Markdown summary
uv run python -m agents.parity label <fingerprint> expected --note "new badge"
uv run python -m agents.parity summary-md output/parity_runs/<timestamp>
```

Reports are written to `output/parity_runs/<timestamp>/` (`report.html`, `report.json` and `defects/`). `run` exits with code 1 when any diff is judged a regression.

### 6. CI scripts (bash)

The CI scripts run everything in one go:

```bash
tools/regression-agent/ci/start.sh     # backend, current build (CURRENT_REF, default origin/main) on 5173, react19 on 5174
tools/regression-agent/ci/compare.sh   # runs parity and writes output/parity_runs/<timestamp>/
tools/regression-agent/ci/stop.sh      # stops the servers and removes the worktree
```

### 7. Unit tests

```powershell
uv run pytest tests/
uv run pytest tests/test_parity.py -k judge
```
