"""Build draft contracts from saved discovery results. No AI, no browser.

Mapping:
    page component + shared components  -> structure regions (landmark locator when the name says so)
    page dynamic_elements               -> volatile regions
    page elements (buttons, inputs...)  -> behavior regions
    flows that visit the page           -> contract.flows
    route marked protected              -> contract.requires_login
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TypeVar

from ..models import ApplicationModel, DynamicElementModel, ElementModel, PageModel
from .models import ContractStatus, PageContract, Region, Tolerance
from .store import load_contract, write_contract

T = TypeVar("T")

# Page element ids end with their control type ("save-settings-button"); element text doesn't.
_CONTROL_WORDS = {"button", "link", "radio", "input", "checkbox", "select", "textbox"}
_SELECTOR = re.compile(r"^(role|text|internal:\w+)=")
# Standard page landmarks every accessible site exposes, keyed by words in a component name.
_LANDMARKS = (("header", "role=banner"), ("footer", "role=contentinfo"), ("nav", "role=navigation"),
              ("card", "role=article"))


def _is_selector(text: str) -> bool:
    return bool(_SELECTOR.match(text))


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _match(region_id: str, candidates: Iterable[T], text_of: Callable[[T], str]) -> T | None:
    """First candidate whose text contains every word of the region id."""
    wanted = _words(region_id) - _CONTROL_WORDS
    if not wanted:
        return None
    return next((c for c in candidates if wanted <= _words(text_of(c))), None)


def _landmark(name: str, page: PageModel) -> str | None:
    if name == page.component:
        return "role=main"
    lowered = name.lower()
    return next((locator for word, locator in _LANDMARKS if word in lowered), None)


def _structure_regions(page: PageModel) -> list[Region]:
    names = [page.component, *page.components] if page.component else page.components
    return [
        Region(region_id=name, tolerance=Tolerance.STRUCTURE, component=name, description=f"{name} component",
               locator=_landmark(name, page))
        for name in dict.fromkeys(names)
    ]


def _volatile_regions(page: PageModel, dynamic: list[DynamicElementModel]) -> list[Region]:
    regions = []
    for region_id in page.dynamic_elements:
        found = (next((d for d in dynamic if region_id in (d.element, d.selector)), None)
                 or _match(region_id, dynamic, lambda d: d.element))
        locator = found.selector if found and found.selector else (region_id if _is_selector(region_id) else None)
        regions.append(Region(
            region_id=region_id,
            tolerance=Tolerance.VOLATILE,
            description=found.reason if found else None,
            locator=locator,
            evidence=found.evidence if found else [],
        ))
    return regions


def _behavior_regions(page: PageModel, elements: list[ElementModel]) -> list[Region]:
    on_page = {page.component, *page.components}
    # Prefer elements discovered on this page's own components.
    ranked = sorted(elements, key=lambda e: e.component not in on_page)
    regions = []
    for region_id in page.elements:
        # Visible label only: action descriptions mention other controls and cause false matches.
        found = next((e for e in ranked if e.selector == region_id), None) or _match(region_id, ranked, lambda e: e.text or "")
        regions.append(Region(
            region_id=region_id,
            tolerance=Tolerance.BEHAVIOR,
            description=found.action if found else None,
            locator=found.selector if found else (region_id if _is_selector(region_id) else None),
            component=found.component if found else None,
            evidence=found.evidence if found else [],
        ))
    return regions


def build_skeletons(model: ApplicationModel) -> list[PageContract]:
    protected = {route.route for route in model.routes if route.protected}
    contracts = []
    for page in model.pages:
        flows = [f.flow_id for f in model.flows if any(s.page in (page.page_id, page.route) for s in f.steps)]
        contracts.append(PageContract(
            page_id=page.page_id,
            route=page.route,
            component=page.component,
            requires_login=page.route in protected,
            regions=_structure_regions(page)
            + _volatile_regions(page, model.dynamic_elements)
            + _behavior_regions(page, model.elements),
            flows=flows,
            apis=page.apis,
            evidence=page.evidence,
        ))
    return contracts


def write_missing_skeletons(discovery_dir: Path) -> list[tuple[PageContract, bool]]:
    """Write a skeleton for every page without a contract, and refresh drafts nobody has filled in yet.

    Returns (contract, was_written) pairs.
    """
    model = ApplicationModel.model_validate_json((discovery_dir / "application.json").read_text(encoding="utf-8"))
    contracts_dir = discovery_dir / "contracts"
    results = []
    for contract in build_skeletons(model):
        existing = load_contract(contracts_dir, contract.page_id)
        # Keep approved contracts and drafts the contracts agent already worked on (it records samples_seen).
        replace = existing is None or (existing.status is ContractStatus.DRAFT and not existing.samples_seen)
        if replace:
            write_contract(contracts_dir, contract)
        results.append((contract, replace))
    return results
