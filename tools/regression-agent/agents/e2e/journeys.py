"""CLI for inspecting and managing approved journey test specs.

Journeys are generated directly as approved specs by agents.planner (no human
approval gate); this tool validates them against the schema and lets you
retire specs that no longer apply.

Usage:
    python -m agents.e2e.journeys validate
    python -m agents.e2e.journeys list [--area AREA] [--status STATUS]
    python -m agents.e2e.journeys retire J001 --reason "..."
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft7Validator

from ..paths import JOURNEYS_DIR as JOURNEYS_ROOT

SCHEMA_PATH = Path(__file__).resolve().parent / "journey.schema.json"
REGISTRY_PATH = JOURNEYS_ROOT / "registry.yaml"

# Reject selectors that look like brittle CSS/XPath instead of semantic targets
# (role/text/label based), e.g. "div.card > span:nth-child(2)" or "//div[@id='x']".
_CSS_LOOKING = re.compile(r"(^//|>>|:nth-child|\.[a-zA-Z0-9_-]+\s*[>.#]|#[a-zA-Z0-9_-]+\s*[>.])")


def _load_schema() -> dict[str, Any]:
    import json

    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _load_registry() -> dict[str, Any]:
    if not REGISTRY_PATH.exists():
        return {}
    return yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8")) or {}


def _save_registry(registry: dict[str, Any]) -> None:
    REGISTRY_PATH.write_text(yaml.safe_dump(registry, sort_keys=True), encoding="utf-8")


def _iter_journey_files():
    yield from sorted(JOURNEYS_ROOT.glob("*/*.yaml"))


def _check_selectors(spec: dict[str, Any], path: Path, errors: list[str]) -> None:
    for i, step in enumerate(spec.get("steps", [])):
        target = step.get("target")
        if target and _CSS_LOOKING.search(target):
            errors.append(
                f"{path}: step {i} target '{target}' looks like a brittle CSS/XPath selector; "
                "use a role/text/label-based target instead"
            )


def cmd_validate(_args: argparse.Namespace) -> int:
    schema = _load_schema()
    validator = Draft7Validator(schema)
    registry = _load_registry()
    errors: list[str] = []
    seen_ids: dict[str, Path] = {}

    for path in _iter_journey_files():
        spec = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for err in validator.iter_errors(spec):
            errors.append(f"{path}: {err.message}")
        _check_selectors(spec, path, errors)

        spec_id = spec.get("id")
        if spec_id:
            if spec_id in seen_ids:
                errors.append(f"{path}: duplicate id '{spec_id}' (also in {seen_ids[spec_id]})")
            else:
                seen_ids[spec_id] = path
            if spec_id not in registry:
                errors.append(f"{path}: id '{spec_id}' missing from registry.yaml")

    for reg_id in registry:
        if reg_id not in seen_ids:
            errors.append(f"registry.yaml: id '{reg_id}' has no corresponding journey file")

    if errors:
        for e in errors:
            print(f"FAIL: {e}")
        print(f"\n{len(errors)} error(s) across {len(seen_ids)} journey file(s).")
        return 1

    print(f"OK: {len(seen_ids)} journey file(s) valid, registry consistent.")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    for path in _iter_journey_files():
        spec = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if args.area and spec.get("area") != args.area:
            continue
        if args.status and spec.get("status") != args.status:
            continue
        print(f"{spec.get('id', '?'):8} {spec.get('status', '?'):10} {spec.get('area', '?'):15} {spec.get('name', '')}")
    return 0


def cmd_retire(args: argparse.Namespace) -> int:
    for path in _iter_journey_files():
        spec = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if spec.get("id") != args.id:
            continue
        spec["status"] = "retired"
        spec.setdefault("provenance", {})["retired_at"] = datetime.now(timezone.utc).isoformat()
        if args.reason:
            spec["provenance"]["retirement_reason"] = args.reason
        path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")

        registry = _load_registry()
        if args.id in registry:
            registry[args.id]["status"] = "retired"
            _save_registry(registry)

        print(f"Retired {args.id} ({path}).")
        return 0

    print(f"ERROR: journey '{args.id}' not found.")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("validate", help="Validate all journey specs against the schema and registry.")

    p_list = sub.add_parser("list", help="List journey specs.")
    p_list.add_argument("--area", help="Area folder name, e.g. login")
    p_list.add_argument("--status", choices=["approved", "retired"])

    p_retire = sub.add_parser("retire", help="Mark a journey spec as retired.")
    p_retire.add_argument("id")
    p_retire.add_argument("--reason", default="")

    args = parser.parse_args(argv)
    if args.command == "validate":
        return cmd_validate(args)
    if args.command == "list":
        return cmd_list(args)
    if args.command == "retire":
        return cmd_retire(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
