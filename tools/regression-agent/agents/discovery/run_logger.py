from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")


@dataclass
class DiscoveryRun:
    """One discovery session's logs and evidence (plan/discovery.md section 25)."""

    root: Path
    tool_calls: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def create(cls, output_root: Path, request: dict[str, Any]) -> "DiscoveryRun":
        root = output_root / _timestamp()
        (root / "raw").mkdir(parents=True, exist_ok=True)
        (root / "run.json").write_text(json.dumps(request, indent=2, default=str), encoding="utf-8")
        return cls(root=root)

    def log(self, line: str) -> None:
        with (self.root / "agent.log").open("a", encoding="utf-8") as handle:
            handle.write(line.rstrip("\n") + "\n")

    def record_tool_call(self, name: str, input_data: dict[str, Any]) -> None:
        self.tool_calls.append({"tool": name, "input": input_data})
        (self.root / "tool-calls.json").write_text(json.dumps(self.tool_calls, indent=2, default=str), encoding="utf-8")

    def write_result(self, result: dict[str, Any]) -> None:
        (self.root / "findings.json").write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")

    def record_cost(self, num_turns: int, total_cost_usd: float | None) -> None:
        """Append one line to the cumulative cost log shared across all runs."""
        costs_log = self.root.parent / "costs.log"
        line = f"{_timestamp()}\trun={self.root.name}\tturns={num_turns}\tcost_usd={total_cost_usd if total_cost_usd is not None else 'unknown'}"
        with costs_log.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
