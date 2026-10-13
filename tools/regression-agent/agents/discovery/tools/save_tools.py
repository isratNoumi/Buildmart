from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from pydantic import ValidationError

from ..contracts.models import ContractStatus, PageContract
from ..models import ApiModel, ComponentModel, DynamicElementModel, ElementModel, FlowModel, PageModel, RouteModel

# Structural records (component/page) describe what exists, not a claim that needs
# independent corroboration, so they are exempt from the evidence requirement below.
_REQUIRES_EVIDENCE = {"route", "api", "element", "dynamic_element", "flow", "contract"}

_MODELS: dict[str, type] = {
    "route": RouteModel,
    "component": ComponentModel,
    "api": ApiModel,
    "element": ElementModel,
    "dynamic_element": DynamicElementModel,
    "flow": FlowModel,
    "page": PageModel,
    "contract": PageContract,
}


def _append_jsonl(output_dir: Path, kind: str, record: dict[str, Any]) -> int:
    raw_dir = output_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / f"{kind}.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
    with path.open("r", encoding="utf-8") as handle:
        return sum(1 for _ in handle)


def _make_save_tool(kind: str, model_cls: type, output_dir: Path):
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        try:
            record = model_cls.model_validate(args)
        except ValidationError as error:
            return {"content": [{"type": "text", "text": f"Rejected: {error}"}], "is_error": True}

        if kind in _REQUIRES_EVIDENCE and not record.evidence:
            return {
                "content": [{"type": "text", "text": "Rejected: evidence is required for every discovery fact."}],
                "is_error": True,
            }

        if isinstance(record, PageContract):
            record.status = ContractStatus.DRAFT  # only a human approves contracts

        count = _append_jsonl(output_dir, kind, record.model_dump(mode="json"))
        return {"content": [{"type": "text", "text": f"Saved {kind} ({count} saved so far)."}]}

    return tool(f"save_{kind}", f"Save a discovered {kind} to the Application Knowledge Model.", model_cls.model_json_schema())(handler)


def build_discovery_tools(output_dir: Path):
    """In-process MCP server exposing one save_* tool per Application Knowledge Model type."""
    tools = [_make_save_tool(kind, model_cls, output_dir) for kind, model_cls in _MODELS.items()]
    return create_sdk_mcp_server(name="discovery", version="0.1.0", tools=tools)
