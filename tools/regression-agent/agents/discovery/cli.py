from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from .agent import run_discovery
from .config import PROJECT_ROOT, DiscoveryRequest


def _parse_args(argv: list[str] | None = None) -> DiscoveryRequest:
    parser = argparse.ArgumentParser(description="Discovery Agent")
    parser.add_argument("--project-path", default=str(PROJECT_ROOT))
    parser.add_argument("--url", dest="application_url", default="http://127.0.0.1:5173")
    parser.add_argument("--backend-url", default="http://127.0.0.1:8000")
    parser.add_argument("--framework", help="Informational, recorded in run.json")
    parser.add_argument("--framework-version", help="Informational, recorded in run.json")
    parser.add_argument("--mode", choices=["full", "static_only", "runtime_only", "contracts"], default="full",
                        help="'contracts' fills in page contracts from existing discovery results")
    parser.add_argument("--site-profile", type=Path, help="Site profile YAML with the site's name, samples and rules (required for --mode contracts)")
    parser.add_argument("--max-turns", type=int, default=60, help="Hard cap on agent tool-use turns.")
    parser.add_argument("--max-budget-usd", type=float, default=2.0, help="Stop the run at this estimated USD spend.")
    args = parser.parse_args(argv)
    if args.mode == "contracts" and not args.site_profile:
        parser.error("--mode contracts requires --site-profile, e.g. baseline/site_profiles/<site>.yaml")

    return DiscoveryRequest(
        project_path=Path(args.project_path),
        application_url=args.application_url,
        backend_url=args.backend_url,
        framework=args.framework,
        framework_version=args.framework_version,
        mode=args.mode,
        site_profile=args.site_profile,
        max_turns=args.max_turns,
        max_budget_usd=args.max_budget_usd,
    )


def main() -> None:
    request = _parse_args()
    run_dir = asyncio.run(run_discovery(request))
    print(f"Discovery run complete. Logs and raw evidence: {run_dir}")
    print(f"Baseline written to: {request.baseline_dir}")

    findings_path = run_dir / "findings.json"
    if findings_path.exists():
        findings = json.loads(findings_path.read_text(encoding="utf-8"))
        print(f"Turns used: {findings.get('num_turns')}  Cost: ${findings.get('total_cost_usd')}")
    print(f"Cumulative cost log: {run_dir.parent / 'costs.log'}")


if __name__ == "__main__":
    main()
