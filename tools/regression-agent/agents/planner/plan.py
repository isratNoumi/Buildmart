"""Deterministic generation of approved journey specs from a Discovery Agent baseline.

No LLM calls here: this module only rearranges facts that the Discovery Agent
already extracted (and evidenced) in baseline/discovery/application.json into the
journey YAML shape consumed by Playwright. Given the same input, it always
produces the same output.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from agents.discovery.models import ApplicationModel, ElementModel, FlowModel, PageModel

from .config import DEFAULT_DISCOVERY_DIR, DEFAULT_JOURNEYS_ROOT

GENERATED_BY = "test_planner"


def _load_application_model(discovery_dir: Path) -> ApplicationModel:
    data = json.loads((discovery_dir / "application.json").read_text(encoding="utf-8"))
    return ApplicationModel.model_validate(data)


def _area_for_route(route: str) -> str:
    """Group journeys by the first route segment: /settings/profile -> settings, / -> home."""
    first = route.strip("/").split("/")[0]
    if not first:
        return "home"
    area = re.sub(r"[^a-z0-9_-]+", "-", first.lower()).strip("-_")
    return area if area and not first.startswith(":") else "other"


def _pages_by_id(model: ApplicationModel) -> dict[str, PageModel]:
    # FlowStep.page has been observed as both a page_id ("login") and a route path
    # ("/login") across discovery runs, so index by both to be robust either way.
    by_id: dict[str, PageModel] = {}
    for p in model.pages:
        by_id[p.page_id] = p
        by_id[p.route] = p
    return by_id


def _elements_by_id(model: ApplicationModel) -> dict[str, ElementModel]:
    # ElementModel has no stable id in the current schema; key by (page element list index)
    # is not available here, so elements are looked up per-page by matching action/text below.
    return {}


def _find_element_for_action(model: ApplicationModel, page: PageModel | None, action_text: str) -> ElementModel | None:
    if page is None:
        return None
    action_lower = action_text.lower()
    candidates = [e for e in model.elements if e.component == page.component]
    for element in candidates:
        haystack = f"{element.action or ''} {element.text or ''}".lower()
        if haystack.strip() and haystack.strip() in action_lower or action_lower in haystack:
            return element
    return None


_SCHEMA_ACTION_BY_ELEMENT_TYPE = {
    "button": "click",
    "link": "click",
    "input": "fill",
    "select": "select",
    "checkbox": "check",
}


def _build_steps_and_dynamic_values(
    model: ApplicationModel, pages: dict[str, PageModel], flow: FlowModel
) -> tuple[list[dict[str, Any]], list[str]]:
    steps: list[dict[str, Any]] = []
    dynamic_values: list[str] = []
    last_route: str | None = None

    for flow_step in flow.steps:
        page = pages.get(flow_step.page)
        route = page.route if page else flow_step.page

        if route != last_route:
            steps.append({"action": "navigate", "target": route})
            last_route = route

        element = _find_element_for_action(model, page, flow_step.action)
        if element is not None:
            schema_action = _SCHEMA_ACTION_BY_ELEMENT_TYPE.get(element.element_type, "click")
            step: dict[str, Any] = {"action": schema_action, "target": element.selector}
            if schema_action in ("fill", "select"):
                step["value"] = f"<{element.text or flow_step.action}>"
            steps.append(step)
        else:
            # No matching catalogued element; record the intent as an explicit wait/assert
            # so a human reviewer can see the gap instead of the step silently disappearing.
            steps.append({"action": "assert", "condition": flow_step.action})

        if page:
            for dyn in model.dynamic_elements:
                if dyn.element not in dynamic_values and dyn.selector and page.component and dyn.element.startswith(page.component):
                    dynamic_values.append(dyn.element)

    return steps, dynamic_values


def _build_assertions(flow: FlowModel) -> list[str]:
    assertions = [
        e.observation
        for e in flow.evidence
        if e.type in ("runtime", "dom") and e.observation
    ]
    if not assertions:
        assertions = [f"Flow '{flow.name}' completes without console errors"]
    return assertions


def _next_id(registry: dict[str, Any]) -> str:
    max_n = 0
    for existing_id in registry:
        if existing_id.startswith("J") and existing_id[1:].isdigit():
            max_n = max(max_n, int(existing_id[1:]))
    return f"J{max_n + 1:03d}"


def _load_registry(journeys_root: Path) -> dict[str, Any]:
    registry_path = journeys_root / "registry.yaml"
    if not registry_path.exists():
        return {}
    return yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}


def _save_registry(journeys_root: Path, registry: dict[str, Any]) -> None:
    (journeys_root / "registry.yaml").write_text(yaml.safe_dump(registry, sort_keys=True), encoding="utf-8")


def generate_candidates(
    discovery_dir: Path = DEFAULT_DISCOVERY_DIR,
    journeys_root: Path = DEFAULT_JOURNEYS_ROOT,
) -> list[Path]:
    """Generate one approved journey YAML per Discovery flow. Returns the written paths."""
    model = _load_application_model(discovery_dir)
    pages = _pages_by_id(model)
    registry = _load_registry(journeys_root)
    written: list[Path] = []
    now = datetime.now(timezone.utc).isoformat()

    for flow in model.flows:
        first_page = pages.get(flow.steps[0].page) if flow.steps else None
        route = first_page.route if first_page else "/"
        area = _area_for_route(route)

        steps, dynamic_values = _build_steps_and_dynamic_values(model, pages, flow)
        assertions = _build_assertions(flow)

        spec_id = _next_id(registry)
        spec: dict[str, Any] = {
            "id": spec_id,
            "name": flow.name,
            "area": area,
            "status": "approved",
            "version": 1,
            "priority": flow.importance.value,
            "steps": steps,
            "assertions": assertions,
            "provenance": {
                "generated_by": GENERATED_BY,
                "generated_at": now,
                "source_flow_id": flow.flow_id,
            },
        }
        if dynamic_values:
            spec["dynamic_values"] = dynamic_values

        area_dir = journeys_root / area
        area_dir.mkdir(parents=True, exist_ok=True)
        out_path = area_dir / f"{spec_id}.yaml"
        out_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
        written.append(out_path)

        registry[spec_id] = {"area": area, "status": "approved", "source_flow_id": flow.flow_id}

    _save_registry(journeys_root, registry)
    return written
