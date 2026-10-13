from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, ResultMessage, TextBlock, ToolUseBlock

from .assemble import assemble_application_model, assemble_contracts
from .config import DiscoveryRequest, settings
from .contracts.models import ContractStatus
from .contracts.profile import SiteProfile, load_profile
from .contracts.skeleton import write_missing_skeletons
from .contracts.store import contract_path, load_all
from .prompts import CONTRACTS_PROMPT_PATH, SYSTEM_PROMPT_PATH
from .run_logger import DiscoveryRun
from .tools import build_can_use_tool, build_discovery_tools

SIGNED_IN_NOTE = (
    "\nThe browser starts signed in as the site's test account, so protected pages are reachable. "
    "You are not given the password: do not log out until protected pages are done, and use only "
    "invalid inputs when exploring the login form.\n"
)


def _bullets(items) -> str:
    return "\n".join(f"- {item}" for item in items) or "- (none)"


def _build_prompt(request: DiscoveryRequest, signed_in: bool = False) -> str:
    profile = load_profile(request.site_profile) if request.site_profile else None
    site_rules = f"\nSite rules:\n{_bullets(profile.rules)}\n" if profile else ""
    return (
        f"Discover the {profile.name if profile else 'target'} application.\n"
        f"Frontend: {request.application_url}\n"
        f"Backend: {request.backend_url}\n"
        f"Source code root: {request.project_path}\n"
        f"Mode: {request.mode}\n"
        f"{site_rules}{SIGNED_IN_NOTE if signed_in else ''}\n"
        "Follow the phases and rules in your system prompt. When you believe discovery "
        "is complete, summarize what you found and stop."
    )


def _build_contracts_prompt(request: DiscoveryRequest, signed_in: bool = False) -> str:
    profile = load_profile(request.site_profile)
    contracts_dir = request.baseline_dir / "contracts"
    write_missing_skeletons(request.baseline_dir)
    drafts = [c for c in load_all(contracts_dir) if c.status is ContractStatus.DRAFT]
    return (
        f"Fill in page contracts for {profile.name}.\n"
        f"Frontend: {request.application_url}\n"
        f"Backend: {request.backend_url}\n\n"
        f"Draft contracts (Read each file):\n"
        + _bullets(f"{contract_path(contracts_dir, c.page_id)} - {c.route} - {len(c.gaps())} gaps" for c in drafts)
        + "\n\nSamples to visit pages with:\n"
        + _bullets(profile.sample_labels())
        + "\n\nFind samples (locate these yourself):\n"
        + _bullets(profile.find_samples)
        + "\n\nSite rules:\n"
        + _bullets(profile.rules)
        + (SIGNED_IN_NOTE if signed_in else "")
        + "\n\nFollow the procedure in your system prompt and save every contract with save_contract."
    )


def _playwright_args(storage_state: Path | None) -> list[str]:
    args = ["-y", "@playwright/mcp@latest", "--headless"]
    if storage_state:
        args += ["--isolated", f"--storage-state={storage_state}"]
    return args


def _write_signed_in_state(url: str, profile: SiteProfile, folder: Path) -> Path:
    """Sign in with Python Playwright so the agent's browser starts logged in without seeing the password."""
    from playwright.sync_api import sync_playwright

    from .session import sign_in

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            state = sign_in(browser, url, profile)
        finally:
            browser.close()
    path = folder / "storage-state.json"
    path.write_text(json.dumps(state), encoding="utf-8")
    return path


async def run_discovery(request: DiscoveryRequest) -> Path:
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set. Add it to .env before running discovery.")

    profile = load_profile(request.site_profile) if request.site_profile else None
    session_dir = Path(tempfile.mkdtemp(prefix="discovery-session-")) if profile and profile.login else None
    try:
        storage_state = None
        if session_dir:
            try:
                storage_state = await asyncio.to_thread(_write_signed_in_state, request.application_url, profile, session_dir)
            except Exception as error:
                raise RuntimeError(f"Signing in with the site profile's login steps failed: {error}") from error
        return await _run_agent(request, storage_state, session_dir)
    finally:
        if session_dir:
            shutil.rmtree(session_dir, ignore_errors=True)


async def _run_agent(request: DiscoveryRequest, storage_state: Path | None, session_dir: Path | None) -> Path:
    run = DiscoveryRun.create(request.output_root, request.model_dump(mode="json"))
    discovery_server = build_discovery_tools(run.root)
    can_use_tool = build_can_use_tool(run.root, secret_dir=session_dir)
    contracts_mode = request.mode == "contracts"
    signed_in = storage_state is not None
    prompt = _build_contracts_prompt(request, signed_in) if contracts_mode else _build_prompt(request, signed_in)
    system_prompt_path = CONTRACTS_PROMPT_PATH if contracts_mode else SYSTEM_PROMPT_PATH

    options = ClaudeAgentOptions(
        system_prompt={"type": "file", "path": str(system_prompt_path)},
        cwd=str(request.project_path),
        mcp_servers={
            "discovery": discovery_server,
            "playwright": {
                "type": "stdio",
                "command": "npx",
                "args": _playwright_args(storage_state),
            },
        },
        can_use_tool=can_use_tool,
        permission_mode="default",
        max_turns=request.max_turns,
        max_budget_usd=request.max_budget_usd,
        env={"ANTHROPIC_API_KEY": settings.anthropic_api_key},
    )

    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        run.log(block.text)
                        print(block.text, flush=True)
                    elif isinstance(block, ToolUseBlock):
                        run.record_tool_call(block.name, block.input)
                        print(f"  -> {block.name.removeprefix('mcp__')}", flush=True)
            elif isinstance(message, ResultMessage):
                run.write_result(
                    {
                        "subtype": message.subtype,
                        "is_error": message.is_error,
                        "num_turns": message.num_turns,
                        "total_cost_usd": message.total_cost_usd,
                        "result": message.result,
                    }
                )
                run.record_cost(message.num_turns, message.total_cost_usd)

    if contracts_mode:
        # A contracts run saves only contracts; rebuilding application.json would wipe it.
        assemble_contracts(run.root, request.baseline_dir / "contracts")
    else:
        assemble_application_model(run.root, request.baseline_dir)
    return run.root
