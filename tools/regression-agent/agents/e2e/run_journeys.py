"""Deterministic, codebase-driven journey runner.

An optional code-change selection report limits execution to impacted journeys.
Route checks against the target app's source still determine runtime applicability.

Usage:
    python -m agents.e2e.run_journeys --base-url http://127.0.0.1:5174 --app-root path/to/app
    python -m agents.e2e.run_journeys --base-url http://127.0.0.1:5174 --app-root path/to/app --headless
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml
from playwright.sync_api import Page, sync_playwright

from ..paths import JOURNEY_ARTIFACTS_DIR
from ..paths import JOURNEYS_DIR as JOURNEYS_ROOT

ROUTE_RE = re.compile(r'<Route\s+path="([^"]+)"\s+element=\{<(\w+)')
QUOTED_RE = re.compile(r"'([^']+)'")
PLACEHOLDER_HINT = "Placeholder"
ASSERT_TIMEOUT_MS = 3000
ACTION_TIMEOUT_MS = 5000
NAVIGATE_TIMEOUT_MS = 15000
STEP_PAUSE_MS = 900  # lets a human watching the headed browser see each step happen
JOURNEY_PAUSE_MS = 1500

# Measured from the Navigation Timing / Paint Timing APIs, relative to navigation start.
NAVIGATION_METRICS_JS = """() => {
  const nav = performance.getEntriesByType('navigation')[0];
  const fcp = performance.getEntriesByName('first-contentful-paint')[0];
  const since = (t) => (nav && t > 0 ? Math.round(t - nav.startTime) : null);
  return {
    ttfb_ms: nav ? since(nav.responseStart) : null,
    dom_content_loaded_ms: nav ? since(nav.domContentLoadedEventEnd) : null,
    load_ms: nav ? since(nav.loadEventEnd) : null,
    first_contentful_paint_ms: fcp ? Math.round(fcp.startTime) : null,
  };
}"""


@dataclass
class StepResult:
    index: int
    action: str
    target: str
    status: str  # PASS | FAIL | BLOCKED
    detail: str = ""
    duration_ms: float = 0.0
    metrics: dict[str, float | None] = field(default_factory=dict)


@dataclass
class JourneyResult:
    journey_id: str
    name: str
    classification: str = "NOT_APPLICABLE"  # NOT_APPLICABLE | PARTIAL | APPLICABLE
    steps: list[StepResult] = field(default_factory=list)
    console_errors: list[str] = field(default_factory=list)
    network_failures: list[str] = field(default_factory=list)
    final_url: str = ""
    screenshot: Path | None = None
    artifacts_dir: Path | None = None

    @property
    def has_failure(self) -> bool:
        return any(s.status == "FAIL" for s in self.steps)

    @property
    def duration_ms(self) -> float:
        return round(sum(s.duration_ms for s in self.steps), 1)


def _iter_journey_files():
    for path in sorted(JOURNEYS_ROOT.glob("*/*.yaml")):
        yield path


def load_selected_specs(selection_report: Path | None = None) -> list[dict[str, Any]]:
    approved = []
    for path in _iter_journey_files():
        spec = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if spec.get("status") == "approved":
            approved.append(spec)
    if selection_report is None:
        return approved
    report = json.loads(selection_report.read_text(encoding="utf-8"))
    records = report.get("selected")
    if not isinstance(records, list) or any(
        not isinstance(record, dict) or not isinstance(record.get("id"), str)
        for record in records
    ):
        raise ValueError("Selection report must contain a 'selected' list of journey IDs")
    selected_ids = {record["id"] for record in records}
    unknown = selected_ids - {spec["id"] for spec in approved}
    if unknown:
        raise ValueError(f"Selected journeys are missing or not approved: {sorted(unknown)}")
    return [spec for spec in approved if spec["id"] in selected_ids]


def scan_app_routes(app_root: Path) -> tuple[set[str], set[str]]:
    """Return (real_routes, placeholder_routes) implemented in the target app's App.jsx."""
    app_jsx = app_root / "src" / "App.jsx"
    text = app_jsx.read_text(encoding="utf-8")
    real, placeholder = set(), set()
    for path_pattern, component in ROUTE_RE.findall(text):
        (placeholder if PLACEHOLDER_HINT in component else real).add(path_pattern)
    return real, placeholder


def _route_matches(route_pattern: str, target: str) -> bool:
    """Match a journey navigate target against a React Router path pattern (':id'-style params)."""
    regex = "^" + re.sub(r":[^/]+", "[^/]+", re.escape(route_pattern).replace(r"\:", ":")) + "$"
    return re.match(regex, target) is not None


def classify_target(target: str, real_routes: set[str], placeholder_routes: set[str]) -> str:
    if any(_route_matches(p, target) for p in real_routes):
        return "real"
    if any(_route_matches(p, target) for p in placeholder_routes):
        return "placeholder"
    return "missing"


def _locate(page: Page, text: str):
    for locator in (
        page.get_by_role("link", name=text, exact=False),
        page.get_by_role("button", name=text, exact=False),
        page.get_by_text(text, exact=False),
    ):
        if locator.count() > 0:
            return locator.first
    return None


def _locate_click_target(page: Page, condition: str):
    """If an assert condition narrates clicking a quoted label (e.g. "Click 'X' button..."),
    resolve that label to a real element so the click can actually be performed instead of
    only checked for as literal visible text."""
    if "click" not in condition.lower():
        return None, None
    for label in QUOTED_RE.findall(condition):
        locator = _locate(page, label)
        if locator is not None:
            return locator, label
    return None, None


def _navigation_metrics(page: Page) -> dict[str, float | None]:
    try:
        page.wait_for_load_state("load", timeout=NAVIGATE_TIMEOUT_MS)
    except Exception:
        pass  # still report whatever timings exist; load_ms stays null
    return page.evaluate(NAVIGATION_METRICS_JS)


def _url_path(url: str) -> str:
    # Path only: baseline and candidate run on different origins, and query strings may hold tokens.
    return urlsplit(url).path or "/"


def run_journey(page: Page, base_url: str, spec: dict[str, Any], real_routes: set[str], placeholder_routes: set[str], console_errors: list[str], step_pause_ms: int = STEP_PAUSE_MS) -> JourneyResult:
    result = JourneyResult(journey_id=spec["id"], name=spec["name"])
    console_errors.clear()  # listener stays registered on the shared page; reset per journey

    in_real_territory = True
    for i, step in enumerate(spec.get("steps", [])):
        action = step.get("action")
        target = step.get("target", "") or step.get("condition", "")

        if not in_real_territory:
            result.steps.append(StepResult(i, action, target, "BLOCKED", "upstream step left implemented territory"))
            continue

        if step_pause_ms:
            page.wait_for_timeout(step_pause_ms)

        started = time.perf_counter()
        try:
            if action == "navigate":
                territory = classify_target(target, real_routes, placeholder_routes)
                if territory == "missing":
                    result.steps.append(StepResult(i, action, target, "BLOCKED", f"route '{target}' not implemented yet"))
                    in_real_territory = False
                    continue
                page.goto(base_url.rstrip("/") + target, timeout=NAVIGATE_TIMEOUT_MS, wait_until="domcontentloaded")
                result.steps.append(StepResult(i, action, target, "PASS", metrics=_navigation_metrics(page)))
                if territory == "placeholder":
                    in_real_territory = False
            elif action == "click":
                locator = _locate(page, target)
                if locator is None:
                    result.steps.append(StepResult(i, action, target, "FAIL", f"no element matching '{target}'"))
                else:
                    locator.click(timeout=ACTION_TIMEOUT_MS)
                    result.steps.append(StepResult(i, action, target, "PASS"))
            elif action == "fill":
                locator = page.get_by_label(target, exact=False)
                if locator.count() == 0:
                    locator = page.get_by_placeholder(target, exact=False)
                if locator.count() == 0:
                    result.steps.append(StepResult(i, action, target, "FAIL", f"no input matching '{target}'"))
                else:
                    locator.first.fill(step.get("value", ""), timeout=ACTION_TIMEOUT_MS)
                    result.steps.append(StepResult(i, action, target, "PASS"))
            elif action == "back":
                page.go_back(timeout=ACTION_TIMEOUT_MS)
                result.steps.append(StepResult(i, action, target, "PASS"))
            elif action == "reload":
                page.reload(timeout=ACTION_TIMEOUT_MS)
                result.steps.append(StepResult(i, action, target, "PASS"))
            elif action == "assert":
                condition = step.get("condition", "")
                click_locator, click_label = _locate_click_target(page, condition)
                if click_locator is not None:
                    click_locator.click(timeout=ACTION_TIMEOUT_MS)
                    result.steps.append(StepResult(i, action, condition, "PASS", f"performed the click on '{click_label}' described in this condition"))
                    continue
                try:
                    page.get_by_text(condition, exact=False).first.wait_for(timeout=ASSERT_TIMEOUT_MS)
                    result.steps.append(StepResult(i, action, condition, "PASS"))
                except Exception:
                    result.steps.append(StepResult(i, action, condition, "BLOCKED", "condition not concretely matchable against current build"))
            else:
                result.steps.append(StepResult(i, action, target, "BLOCKED", f"action '{action}' not yet supported by runner"))
        except Exception as exc:
            result.steps.append(StepResult(i, action, target, "FAIL", str(exc)))
            in_real_territory = False
        finally:
            result.steps[-1].duration_ms = round((time.perf_counter() - started) * 1000, 1)

    result.console_errors = list(console_errors)
    statuses = {s.status for s in result.steps}
    if "PASS" not in statuses and "FAIL" not in statuses:
        result.classification = "NOT_APPLICABLE"
    elif "BLOCKED" in statuses:
        result.classification = "PARTIAL"
    else:
        result.classification = "APPLICABLE"
    return result


def select_and_run(base_url: str, app_root: Path, headless: bool, artifacts_dir: Path, selection_report: Path | None = None, step_pause_ms: int = STEP_PAUSE_MS) -> list[JourneyResult]:
    specs = load_selected_specs(selection_report)
    if not specs:
        return []
    real_routes, placeholder_routes = scan_app_routes(app_root)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    origin = base_url.rstrip("/")
    results = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless, slow_mo=400 if not headless else 0)
        # One context/page/window for the whole run so it stays visible instead of flashing open/closed per journey.
        context = browser.new_context(record_video_dir=str(artifacts_dir) if not headless else None)
        page = context.new_page()
        console_errors: list[str] = []
        network_failures: list[str] = []
        page.on("console", lambda msg: console_errors.append(msg.text.replace(origin, "")) if msg.type == "error" else None)
        page.on("requestfailed", lambda req: network_failures.append(f"{req.method} {_url_path(req.url)} failed: {req.failure}"))
        page.on("response", lambda res: network_failures.append(f"{res.request.method} {_url_path(res.url)} -> HTTP {res.status}") if res.status >= 400 else None)
        for spec in specs:
            journey_dir = artifacts_dir / spec["id"]
            journey_dir.mkdir(parents=True, exist_ok=True)
            network_failures.clear()
            context.tracing.start(screenshots=True, snapshots=True, sources=True)
            result = run_journey(page, base_url, spec, real_routes, placeholder_routes, console_errors, step_pause_ms)
            result.network_failures = list(network_failures)
            result.final_url = _url_path(page.url)
            try:
                page.screenshot(path=str(journey_dir / "final.png"), full_page=True)
                result.screenshot = journey_dir / "final.png"
            except Exception:
                pass  # page may have crashed or closed; the step results already record why
            context.tracing.stop(path=str(journey_dir / "trace.zip"))
            result.artifacts_dir = journey_dir
            results.append(result)
            if step_pause_ms:
                page.wait_for_timeout(JOURNEY_PAUSE_MS)
        context.close()
        browser.close()
    return results


def print_report(results: list[JourneyResult]) -> int:
    exit_code = 0
    for r in results:
        print(f"\n{r.journey_id} {r.name} -> {r.classification}")
        for s in r.steps:
            print(f"  [{s.status:7}] step {s.index} {s.action} {s.target!r}" + (f" ({s.detail})" if s.detail else ""))
        if r.console_errors:
            print(f"  console errors: {r.console_errors}")
        print(f"  trace: playwright show-trace {r.artifacts_dir / 'trace.zip'}")
        if r.has_failure:
            exit_code = 1
    skipped = sum(1 for r in results if r.classification == "NOT_APPLICABLE")
    print(f"\n{len(results)} journey(s) considered, {skipped} not applicable to this build.")
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:5173")
    parser.add_argument("--app-root", required=True, help="Path to the target app's source (its routes are scanned)")
    parser.add_argument("--headless", action="store_true", help="Run without a visible browser window")
    parser.add_argument("--artifacts-dir", default=str(JOURNEY_ARTIFACTS_DIR), help="Where to write per-journey trace.zip/video recordings")
    parser.add_argument("--selection-report", type=Path, help="JSON report from python -m agents.planner.select; run only selected journeys")
    args = parser.parse_args()

    app_root = Path(args.app_root).resolve()
    results = select_and_run(args.base_url, app_root, args.headless, Path(args.artifacts_dir).resolve(), args.selection_report)
    return print_report(results)


if __name__ == "__main__":
    sys.exit(main())
