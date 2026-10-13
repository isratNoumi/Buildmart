"""Agent 2 - Driver: open each planned page on both builds, seconds apart, and record what appears.

Per cell and build: fresh browser context (no shared cart), signed in with the test account's
session only if the page requires login, apply the sample's setup steps, open the page, then
watch every contract region until it settles:

    settled      appeared and stopped changing for SETTLE_QUIET_MS
    stuck        appeared but kept changing until its timeout (e.g. a spinner)
    missing      never appeared before its timeout
    unlocatable  the contract has no usable locator for it
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import Browser, Error as PlaywrightError, Locator, Page, sync_playwright

from agents.discovery.contracts.models import PageContract, Region, Tolerance
from agents.discovery.contracts.profile import SiteProfile
from agents.discovery.session import run_steps, sign_in
from agents.e2e import run_journeys
from agents.e2e.run_journeys import NAVIGATE_TIMEOUT_MS, _navigation_metrics, _route_matches, _url_path

from .models import Build, Cell, CellRun, RegionObservation, SideRun

SETTLE_QUIET_MS = 500
POLL_MS = 100
VIEWPORT = {"width": 1366, "height": 900}
# Sums unexpected layout shifts (Chromium Layout Instability API) from the very first paint.
LAYOUT_SHIFT_JS = """
window.__parityLayoutShift = 0;
new PerformanceObserver((list) => {
  for (const entry of list.getEntries()) if (!entry.hadRecentInput) window.__parityLayoutShift += entry.value;
}).observe({ type: 'layout-shift', buffered: true });
"""


# --- Setup: sign in and put the site into the sample's state ------------------------------

class Sessions:
    """Signs in once per build, on first need, and hands out that session (kept in memory only)."""

    def __init__(self, browser: Browser, profile: SiteProfile, urls: dict[str, str]):
        self.browser, self.profile, self.urls = browser, profile, urls
        self._states: dict[str, dict[str, Any] | Exception] = {}

    def get(self, build: Build) -> dict[str, Any]:
        if self.profile.login is None:
            raise LookupError("page requires login but the site profile has no login section")
        if build not in self._states:
            try:
                self._states[build] = sign_in(self.browser, self.urls[build], self.profile)
            except Exception as error:  # remembered so every later page reports the same cause
                self._states[build] = LookupError(f"signing in failed: {str(error).splitlines()[0]}")
        state = self._states[build]
        if isinstance(state, Exception):
            raise state
        return state


def apply_setup(page: Page, origin: str, sample: dict[str, str], profile: SiteProfile) -> None:
    for key in sample:
        run_steps(page, origin, profile.setup.get(key, []), sample)


# --- Watching regions --------------------------------------------------------------------

def _snapshot(locator: Locator) -> tuple[str, tuple[float, ...]] | None:
    """(text, rounded box) of a visible region, or None if not visible yet."""
    try:
        if locator.count() == 0 or not locator.first.is_visible():
            return None
        box = locator.first.bounding_box()
        rounded = tuple(round(v) for v in box.values()) if box else ()
        return locator.first.inner_text(timeout=500)[:500], rounded
    except PlaywrightError:
        return None  # element detached mid-render; try again next poll


def _details(locator: Locator, observation: RegionObservation) -> None:
    try:
        first = locator.first
        observation.count = locator.count()
        observation.text = first.inner_text(timeout=500)[:500]
        observation.aria = first.aria_snapshot(timeout=1000)[:2000]
        observation.enabled = first.is_enabled(timeout=500)
        box = first.bounding_box()
        observation.box = (box["x"], box["y"], box["width"], box["height"]) if box else None
    except PlaywrightError as error:
        observation.detail = str(error).splitlines()[0]


def watch_regions(page: Page, regions: list[Region], started: float) -> list[RegionObservation]:
    observations = {r.region_id: RegionObservation(region_id=r.region_id, status="missing") for r in regions}
    pending: dict[str, tuple[Region, Locator]] = {}
    for region in regions:
        if not region.locator:
            observations[region.region_id].status = "unlocatable"
            observations[region.region_id].detail = "contract has no locator"
            continue
        try:
            locator = page.locator(region.locator)
            locator.count()  # surfaces invalid selector syntax now
            pending[region.region_id] = (region, locator)
        except PlaywrightError as error:
            observations[region.region_id].status = "unlocatable"
            observations[region.region_id].detail = str(error).splitlines()[0]

    last: dict[str, tuple[str, tuple[float, ...]]] = {}
    last_change: dict[str, float] = {}
    while pending:
        now = (time.perf_counter() - started) * 1000
        for region_id, (region, locator) in list(pending.items()):
            observation = observations[region_id]
            snap = _snapshot(locator)
            if snap is not None:
                if observation.appeared_ms is None:
                    observation.appeared_ms = round(now, 1)
                if snap != last.get(region_id):
                    last[region_id], last_change[region_id] = snap, now
                elif now - last_change[region_id] >= SETTLE_QUIET_MS:
                    observation.status, observation.settled_ms = "settled", round(last_change[region_id], 1)
            if observation.status != "settled" and now >= region.settle_timeout_ms:
                observation.status = "stuck" if observation.appeared_ms is not None else "missing"
            if observation.status in ("settled", "stuck") or now >= region.settle_timeout_ms:
                if observation.appeared_ms is not None:
                    _details(locator, observation)
                del pending[region_id]
        if pending:
            page.wait_for_timeout(POLL_MS)
    return [observations[r.region_id] for r in regions]


# --- One build, one cell -----------------------------------------------------------------

def _file_name(index: int, cell: Cell) -> str:
    return f"{index:03d}-{re.sub(r'[^A-Za-z0-9_-]+', '-', cell.page_id)}.png"


def run_side(browser: Browser, build: Build, base_url: str, cell: Cell, contract: PageContract,
             profile: SiteProfile, shot_path: Path, sessions: Sessions) -> SideRun:
    origin = base_url.rstrip("/")
    side = SideRun(build=build, url=origin + cell.path)
    try:
        storage_state = sessions.get(build) if cell.requires_login else None
    except LookupError as error:
        side.error = f"LookupError: {error}"
        return side
    context = browser.new_context(viewport=VIEWPORT, storage_state=storage_state)
    context.add_init_script(LAYOUT_SHIFT_JS)
    page = context.new_page()
    page.on("console", lambda msg: side.console_errors.append(msg.text.replace(origin, "")) if msg.type == "error" else None)
    page.on("requestfailed", lambda req: side.network_failures.append(f"{req.method} {_url_path(req.url)} failed: {req.failure}"))
    page.on("response", lambda res: side.network_failures.append(f"{res.request.method} {_url_path(res.url)} -> HTTP {res.status}") if res.status >= 400 else None)
    try:
        apply_setup(page, origin, cell.sample, profile)
        side.console_errors.clear()  # only count what the page under test produces
        side.network_failures.clear()
        started = time.perf_counter()
        page.goto(side.url, wait_until="domcontentloaded", timeout=NAVIGATE_TIMEOUT_MS)
        side.regions = watch_regions(page, contract.regions, started)
        side.metrics = _navigation_metrics(page)
        side.layout_shift = page.evaluate("() => window.__parityLayoutShift ?? null")
        side.final_path = _url_path(page.url)
        masks = [page.locator(r.locator) for r in contract.regions
                 if r.tolerance is Tolerance.VOLATILE and (o := side.region(r.region_id)) and o.status != "unlocatable"]
        shot_path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(shot_path), full_page=True, mask=masks)
        side.screenshot = str(shot_path)
    except (PlaywrightError, LookupError, KeyError) as error:
        side.error = f"{type(error).__name__}: {str(error).splitlines()[0] if str(error) else ''}"
    finally:
        context.close()
    return side


# --- Public entry points -------------------------------------------------------------------

def run_cells(cells: list[Cell], contracts: dict[str, PageContract], profile: SiteProfile,
              current_url: str, candidate_url: str, out_dir: Path, headless: bool = True) -> list[CellRun]:
    runs = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        sessions = Sessions(browser, profile, {"current": current_url, "candidate": candidate_url})
        for index, cell in enumerate(cells):
            # Alternate who goes first so cache warm-up doesn't always favour one build.
            current_first = index % 2 == 0
            order: list[tuple[Build, str]] = [("current", current_url), ("candidate", candidate_url)]
            sides = {
                build: run_side(browser, build, url, cell, contracts[cell.page_id], profile,
                                out_dir / build / _file_name(index, cell), sessions)
                for build, url in (order if current_first else order[::-1])
            }
            runs.append(CellRun(cell=cell, current=sides["current"], candidate=sides["candidate"], current_first=current_first))
        browser.close()
    return runs


def run_flows(flow_ids: set[str] | None, routes: set[str], protected_routes: set[str], profile: SiteProfile,
              current_url: str, candidate_url: str, headless: bool = True) -> list[dict]:
    """Run the approved journeys behind the pages' flows on both builds (behaviour outcomes).

    Journeys that visit a protected route start signed in; the rest start signed out.
    """
    from .journeys import journey_to_dict

    specs = [s for s in run_journeys.load_selected_specs()
             if flow_ids is None or s.get("provenance", {}).get("source_flow_id") in flow_ids]
    pairs = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        sessions = Sessions(browser, profile, {"current": current_url, "candidate": candidate_url})
        for spec in specs:
            pair = {"journey_id": spec["id"], "flow_id": spec["provenance"]["source_flow_id"]}
            needs_login = any(
                step.get("action") == "navigate" and any(_route_matches(r, step.get("target", "")) for r in protected_routes)
                for step in spec.get("steps", [])
            )
            for build, url in (("current", current_url), ("candidate", candidate_url)):
                try:
                    storage_state = sessions.get(build) if needs_login else None
                except LookupError as error:
                    pair["error"] = str(error)
                    break
                context = browser.new_context(viewport=VIEWPORT, storage_state=storage_state)
                page = context.new_page()
                errors: list[str] = []
                page.on("console", lambda msg, errors=errors: errors.append(msg.text) if msg.type == "error" else None)
                result = run_journeys.run_journey(page, url, spec, routes, set(), errors, step_pause_ms=0)
                result.final_url = _url_path(page.url)
                pair[build] = journey_to_dict(result)
                context.close()
            pairs.append(pair)
        browser.close()
    return pairs
