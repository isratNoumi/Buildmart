"""Browser steps from a site profile, and signing in once so a session can be reused.

Step values may use {key} for sample values and {env:NAME} for secrets read from the
environment or .env. Secrets are only typed into the browser: never logged, saved in
reports or sent to a model.
"""

from __future__ import annotations

import os
import re
from typing import Any

from dotenv import dotenv_values
from playwright.sync_api import Browser, Page

from .config import REPO_ROOT
from .contracts.profile import SetupStep, SiteProfile

STEP_TIMEOUT_MS = 15000
_SAMPLE_KEY = re.compile(r"\{(\w+)\}")
_ENV_KEY = re.compile(r"\{env:([A-Za-z_]\w*)\}")


def resolve(template: str, sample: dict[str, str] | None = None, secrets: bool = True) -> str:
    """Fill {key} from the sample and {env:NAME} from the environment (left as-is when secrets=False)."""
    text = _SAMPLE_KEY.sub(lambda m: (sample or {}).get(m.group(1), m.group(0)), template)
    if not secrets:
        return text

    def secret(match: re.Match) -> str:
        name = match.group(1)
        value = os.environ.get(name) or dotenv_values(REPO_ROOT / ".env").get(name)
        if not value:
            raise KeyError(f"{name} is not set in the environment or .env")
        return value

    return _ENV_KEY.sub(secret, text)


def _find(page: Page, text: str):
    for locator in (page.get_by_role("button", name=text), page.get_by_role("link", name=text), page.get_by_text(text)):
        if locator.count() > 0:
            return locator.first
    raise LookupError(f"nothing to click matching '{text}'")


def run_steps(page: Page, origin: str, steps: list[SetupStep], sample: dict[str, str] | None = None) -> None:
    for step in steps:
        if step.action == "goto":
            page.goto(origin + resolve(step.target, sample), wait_until="domcontentloaded", timeout=STEP_TIMEOUT_MS)
        elif step.action == "click":
            _find(page, resolve(step.target, sample)).click(timeout=STEP_TIMEOUT_MS)
        else:
            field = page.get_by_label(step.target, exact=False)
            if field.count() == 0:
                field = page.get_by_placeholder(step.target, exact=False)
            if field.count() == 0:
                raise LookupError(f"no input matching '{step.target}'")
            field.first.fill(resolve(step.value or "", sample), timeout=STEP_TIMEOUT_MS)


def sign_in(browser: Browser, base_url: str, profile: SiteProfile) -> dict[str, Any]:
    """Run the profile's login steps once; return the session (cookies + local storage) to reuse."""
    if profile.login is None:
        raise ValueError("the site profile has no login section")
    context = browser.new_context()
    try:
        page = context.new_page()
        run_steps(page, base_url.rstrip("/"), profile.login.steps)
        if profile.login.success_text:
            page.get_by_text(profile.login.success_text).first.wait_for(timeout=STEP_TIMEOUT_MS)
        return context.storage_state()
    finally:
        context.close()
