from __future__ import annotations

from pathlib import Path
from typing import Any

from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny, ToolPermissionContext

# Denied outright: discovery never needs a shell, and must never fetch arbitrary
# external content (plan/discovery.md section 23 - "operate only inside the
# configured project/application").
_ALWAYS_DENIED = {"Bash", "WebFetch", "WebSearch"}


def _inside(raw_path: str | None, folder: Path) -> bool:
    if not raw_path:
        return False
    path = Path(raw_path).resolve()
    return path == folder or folder in path.parents


def build_can_use_tool(output_dir: Path, secret_dir: Path | None = None):
    """Build a can_use_tool callback scoped to one discovery run.

    Policy (plan/discovery.md sections 12 and 23):
    - Read/Grep/Glob: allowed (read-only source inspection), except inside secret_dir, which
      holds the signed-in browser session.
    - ToolSearch: always allowed. MCP server tools (e.g. Playwright's browser_* tools) are
      deferred until discovered via ToolSearch; denying it means MCP tools can never be
      loaded even though the server itself connects successfully.
    - mcp__playwright__*: always allowed. Playwright MCP tools are generic browser actions
      (navigate/click/snapshot/...); the tool layer has no visibility into which app action
      a click performs, so true READ_ONLY/STATE_CHANGE/DESTRUCTIVE/EXTERNAL classification is
      enforced by the system prompt (never perform irreversible actions such as payments) plus running
      against a disposable database copy, not by this callback.
    - mcp__discovery__*: our own save_* tools; always allowed, they validate shape and
      required evidence themselves before writing anything.
    - Write/Edit: allowed only inside this run's own output directory.
    - Everything else (Bash, WebFetch, WebSearch, ...): denied.
    """

    resolved_output_dir = output_dir.resolve()
    resolved_secret_dir = secret_dir.resolve() if secret_dir else None

    async def can_use_tool(
        tool_name: str, input_data: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResultAllow | PermissionResultDeny:
        if tool_name in ("Read", "Grep", "Glob"):
            target = input_data.get("file_path") or input_data.get("path")
            if resolved_secret_dir and _inside(target, resolved_secret_dir):
                return PermissionResultDeny(message="That folder holds the browser session and is off limits")
            return PermissionResultAllow()

        if tool_name == "ToolSearch":
            return PermissionResultAllow()

        if tool_name.startswith("mcp__playwright__") or tool_name.startswith("mcp__discovery__"):
            return PermissionResultAllow()

        if tool_name in ("Write", "Edit"):
            raw_path = input_data.get("file_path", "")
            file_path = Path(raw_path).resolve() if raw_path else None
            if file_path and (file_path == resolved_output_dir or resolved_output_dir in file_path.parents):
                return PermissionResultAllow()
            return PermissionResultDeny(message=f"Discovery may only write inside {resolved_output_dir}")

        if tool_name in _ALWAYS_DENIED:
            return PermissionResultDeny(message=f"{tool_name} is not available during discovery")

        return PermissionResultDeny(message=f"{tool_name} is not an approved discovery tool")

    return can_use_tool
