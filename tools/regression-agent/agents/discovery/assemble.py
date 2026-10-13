from __future__ import annotations

import json
from pathlib import Path

from .contracts.models import PageContract
from .contracts.store import save_draft
from .models import ApiModel, ApplicationModel, ComponentModel, DynamicElementModel, ElementModel, FlowModel, PageModel, RouteModel


def _load_jsonl(path: Path, model_cls: type) -> list:
    if not path.exists():
        return []
    items = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                items.append(model_cls.model_validate_json(line))
    return items


def assemble_application_model(run_dir: Path, baseline_dir: Path) -> ApplicationModel:
    """Rebuild the Application Knowledge Model from one run's raw save_* records.

    Deterministic plain Python, not an LLM call: the agent already decided and cited each
    fact via the save_* tools, so recombining those records into the plan/discovery.md
    section 20 layout must not introduce any further variance between runs.
    """
    raw_dir = run_dir / "raw"
    routes = _load_jsonl(raw_dir / "route.jsonl", RouteModel)
    components = _load_jsonl(raw_dir / "component.jsonl", ComponentModel)
    apis = _load_jsonl(raw_dir / "api.jsonl", ApiModel)
    elements = _load_jsonl(raw_dir / "element.jsonl", ElementModel)
    dynamic_elements = _load_jsonl(raw_dir / "dynamic_element.jsonl", DynamicElementModel)
    flows = _load_jsonl(raw_dir / "flow.jsonl", FlowModel)
    pages = _load_jsonl(raw_dir / "page.jsonl", PageModel)

    model = ApplicationModel(
        routes=routes,
        components=components,
        apis=apis,
        elements=elements,
        dynamic_elements=dynamic_elements,
        flows=flows,
        pages=pages,
    )

    baseline_dir.mkdir(parents=True, exist_ok=True)
    (baseline_dir / "application.json").write_text(model.model_dump_json(indent=2), encoding="utf-8")
    (baseline_dir / "routes.json").write_text(json.dumps([r.model_dump(mode="json") for r in routes], indent=2), encoding="utf-8")
    (baseline_dir / "apis.json").write_text(json.dumps([a.model_dump(mode="json") for a in apis], indent=2), encoding="utf-8")
    (baseline_dir / "elements.json").write_text(json.dumps([e.model_dump(mode="json") for e in elements], indent=2), encoding="utf-8")
    (baseline_dir / "dynamic-elements.json").write_text(
        json.dumps([d.model_dump(mode="json") for d in dynamic_elements], indent=2), encoding="utf-8"
    )

    for subdir, items in (("pages", pages), ("flows", flows), ("components", components)):
        folder = baseline_dir / subdir
        if folder.exists():
            for stale_file in folder.glob("*.json"):
                stale_file.unlink()
        folder.mkdir(exist_ok=True)
        for item in items:
            name = getattr(item, "page_id", None) or getattr(item, "flow_id", None) or getattr(item, "name")
            (folder / f"{name}.json").write_text(item.model_dump_json(indent=2), encoding="utf-8")

    return model


def assemble_contracts(run_dir: Path, contracts_dir: Path) -> list[Path]:
    """Save the contracts from one contracts-mode run. Leaves application.json untouched."""
    saved = _load_jsonl(run_dir / "raw" / "contract.jsonl", PageContract)
    latest = {contract.page_id: contract for contract in saved}  # agent may save a page more than once
    return [save_draft(contracts_dir, contract) for contract in latest.values()]
