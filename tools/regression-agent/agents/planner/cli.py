from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import DEFAULT_DISCOVERY_DIR, DEFAULT_JOURNEYS_ROOT
from .plan import generate_candidates


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate approved journey specs from a Discovery Agent baseline.")
    parser.add_argument("--discovery-dir", type=Path, default=DEFAULT_DISCOVERY_DIR)
    parser.add_argument("--journeys-root", type=Path, default=DEFAULT_JOURNEYS_ROOT)
    args = parser.parse_args(argv)
    if not (args.discovery_dir / "application.json").exists():
        print(f"No discovery results in '{args.discovery_dir}'. Run discovery first.")
        return 1

    written = generate_candidates(discovery_dir=args.discovery_dir, journeys_root=args.journeys_root)
    for path in written:
        print(f"wrote {path}")
    print(f"{len(written)} journey spec(s) generated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
