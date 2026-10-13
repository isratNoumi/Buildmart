"""Commands for page contracts.

    python -m agents.discovery.contracts skeleton           build drafts from discovery results
    python -m agents.discovery.contracts show               one line per contract
    python -m agents.discovery.contracts show PAGE_ID       regions and gaps of one contract
    python -m agents.discovery.contracts approve PAGE_ID    mark a reviewed contract as approved
"""

from __future__ import annotations

import argparse
from pathlib import Path

from ...paths import DISCOVERY_DIR as DEFAULT_DISCOVERY_DIR
from .skeleton import write_missing_skeletons
from .store import approve, draft_path, load_all, load_contract


def cmd_skeleton(discovery_dir: Path) -> int:
    if not (discovery_dir / "application.json").exists():
        print(f"No discovery results in '{discovery_dir}'. Run discovery first.")
        return 1
    for contract, is_new in write_missing_skeletons(discovery_dir):
        if is_new:
            print(f"wrote  {contract.page_id:20} {len(contract.regions):2} regions, {len(contract.gaps()):2} gaps")
        else:
            print(f"skip   {contract.page_id} (approved, or already filled in by the contracts agent)")
    return 0


def cmd_show(discovery_dir: Path, page_id: str | None) -> int:
    contracts_dir = discovery_dir / "contracts"
    if page_id is None:
        contracts = load_all(contracts_dir)
        if not contracts:
            print("No contracts yet. Run discovery, then: python -m agents.discovery.contracts skeleton")
        for c in contracts:
            pending = "  (new draft waiting for approval)" if draft_path(contracts_dir, c.page_id).exists() else ""
            print(f"{c.page_id:20} {c.status.value:9} v{c.version}  {len(c.regions):2} regions  {len(c.gaps()):2} gaps{pending}")
        return 0
    contract = load_contract(contracts_dir, page_id)
    if contract is None:
        print(f"No contract for '{page_id}'. Run: python -m agents.discovery.contracts skeleton")
        return 1
    print(f"{contract.page_id}  {contract.route}  {contract.status.value} v{contract.version}")
    print(f"flows: {', '.join(contract.flows) or '-'}")
    for r in contract.regions:
        print(f"  {r.tolerance.value:9} {r.region_id:28} {r.locator or '(locator unknown)'}")
    print("gaps:")
    for gap in contract.gaps():
        print(f"  - {gap}")
    return 0


def cmd_approve(discovery_dir: Path, page_id: str, allow_gaps: bool) -> int:
    try:
        contract = approve(discovery_dir / "contracts", page_id, allow_gaps)
    except (FileNotFoundError, ValueError) as error:
        print(f"Not approved: {error}")
        return 1
    print(f"Approved {contract.page_id} v{contract.version}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--discovery-dir", type=Path, default=DEFAULT_DISCOVERY_DIR)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("skeleton", help="Build draft contracts from discovery results")
    show = sub.add_parser("show", help="List contracts, or show one")
    show.add_argument("page_id", nargs="?")
    approve_cmd = sub.add_parser("approve", help="Approve a reviewed contract (or its waiting draft)")
    approve_cmd.add_argument("page_id")
    approve_cmd.add_argument("--allow-gaps", action="store_true", help="Approve even if details are still missing")
    args = parser.parse_args(argv)

    if args.command == "skeleton":
        return cmd_skeleton(args.discovery_dir)
    if args.command == "approve":
        return cmd_approve(args.discovery_dir, args.page_id, args.allow_gaps)
    return cmd_show(args.discovery_dir, args.page_id)
