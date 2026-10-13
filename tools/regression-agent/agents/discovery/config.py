from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from ..paths import DISCOVERY_DIR, DISCOVERY_RUNS_DIR, REPO_ROOT

# Default location of the target application's source; override with --project-path.
PROJECT_ROOT = REPO_ROOT.parent


class DiscoverySettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=REPO_ROOT / ".env", env_prefix="", extra="ignore")

    anthropic_api_key: str | None = None


settings = DiscoverySettings()


class DiscoveryRequest(BaseModel):
    """Input to a discovery run. Mirrors plan/discovery.md section 5."""

    project_path: Path = PROJECT_ROOT
    application_url: str = "http://127.0.0.1:5173"
    backend_url: str = "http://127.0.0.1:8000"
    framework: str | None = None  # informational only, e.g. "react"
    framework_version: str | None = None
    mode: str = Field(default="full", pattern="^(full|static_only|runtime_only|contracts)$")
    site_profile: Path | None = None  # required for mode=contracts

    # Required budget/time limits so a run can never go unbounded (plan/discovery.md section 23).
    max_turns: int = Field(default=60, ge=1, le=500)
    max_budget_usd: float = Field(default=2.0, gt=0)

    output_root: Path = DISCOVERY_RUNS_DIR
    baseline_dir: Path = DISCOVERY_DIR
