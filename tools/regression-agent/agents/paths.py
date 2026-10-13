"""Where the agents read their baseline and write their output. The backend uses the same locations.

    baseline/  what the agents learned about the current app; reviewed and committed
    output/    results of each run; not committed

Both can be moved with REGRESSION_BASELINE_DIR and REGRESSION_OUTPUT_DIR.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_DIR = Path(os.environ.get("REGRESSION_BASELINE_DIR") or REPO_ROOT / "baseline").resolve()
OUTPUT_DIR = Path(os.environ.get("REGRESSION_OUTPUT_DIR") or REPO_ROOT / "output").resolve()

DISCOVERY_DIR = BASELINE_DIR / "discovery"          # application model, pages, flows
CONTRACTS_DIR = DISCOVERY_DIR / "contracts"         # page contracts (drafts and approved)
JOURNEYS_DIR = BASELINE_DIR / "journeys"            # generated journey specs + registry.yaml
PROFILES_DIR = BASELINE_DIR / "site_profiles"       # one YAML per website
EXPECTED_CHANGES_FILE = BASELINE_DIR / "expected_changes.yaml"  # intended differences (optional)

DISCOVERY_RUNS_DIR = OUTPUT_DIR / "discovery_runs"  # raw log of each discovery run
JOURNEY_ARTIFACTS_DIR = OUTPUT_DIR / "journey_artifacts"  # traces and videos from journey runs
PARITY_RUNS_DIR = OUTPUT_DIR / "parity_runs"        # comparison reports and screenshots
PARITY_CACHE_DIR = OUTPUT_DIR / "parity_cache"      # human labels and cached AI judgments
