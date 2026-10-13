"""Agent 1 - Planner: decide which pages to check and with which samples.

    pages    all contracts, or only pages affected by changed files
    samples  pinned samples every run + a few rotated samples (seeded by --iteration)
"""

from __future__ import annotations

import random
import re

from agents.discovery.contracts.models import PageContract
from agents.discovery.contracts.profile import SiteProfile

from .models import Cell

_PARAM = re.compile(r":(\w+)")
NOT_FOUND_PATH = "/__parity_check_not_found__"


def fill_route(route: str, sample: dict[str, str]) -> str | None:
    """Fill :params from same-named sample keys. None if the sample can't fill this route."""
    if route.startswith("*") or not route.startswith("/"):
        return NOT_FOUND_PATH  # catch-all route (e.g. "*" or "* (not found)"): visit a path that cannot exist
    if any(name not in sample for name in _PARAM.findall(route)):
        return None
    return _PARAM.sub(lambda m: sample[m.group(1)], route)


def _usable(route: str, samples: list[dict[str, str]]) -> list[dict[str, str]]:
    return [s for s in samples if fill_route(route, s) is not None]


def plan(
    contracts: list[PageContract],
    profile: SiteProfile,
    page_ids: set[str] | None = None,
    iteration: int = 0,
    per_page: int = 2,
) -> tuple[list[Cell], list[str]]:
    """Return (cells to run, pages that no sample can cover)."""
    rng = random.Random(iteration)
    cells: list[Cell] = []
    uncovered: list[str] = []
    for contract in sorted(contracts, key=lambda c: c.page_id):
        if page_ids is not None and contract.page_id not in page_ids:
            continue
        pinned = _usable(contract.route, profile.pinned)
        pool = [s for s in _usable(contract.route, profile.samples) if s not in pinned]
        rotated = rng.sample(pool, min(per_page, len(pool)))
        chosen = [(s, True) for s in pinned] + [(s, False) for s in rotated]
        if not chosen:
            missing = ", ".join(_PARAM.findall(contract.route))
            uncovered.append(f"{contract.page_id} ({contract.route}): no sample provides {missing}")
        for sample, is_pinned in chosen:
            cells.append(Cell(page_id=contract.page_id, route=contract.route,
                              path=fill_route(contract.route, sample), sample=sample, pinned=is_pinned,
                              requires_login=contract.requires_login))
    return cells, uncovered
