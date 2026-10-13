"""Agent 3 - Comparator: deterministic diff of the two builds. No AI.

Per region, by the contract's tolerance class:
    volatile   presence + format_rule only (exact value too with strict_values)
    structure  accessibility snapshot must match (digits masked)
    behavior   must exist, be enabled the same way, have the same accessible name
Every region:  missing/extra, stuck, slower to settle, size change.
Every page:    region order, navigation timings, layout shift, new console errors / failed requests.
Journeys:      the behaviour outcome of each linked journey (parity.journeys).
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from agents.discovery.contracts.models import PageContract, Region, Tolerance
from .journeys import compare_journey

from .models import CellRun, Diff, RegionObservation, SideRun, normalize_aria

PRESENT = ("settled", "stuck")
# Present on every page, even a "not found" page or a placeholder, so they can't show the page itself exists.
SITE_WIDE_LOCATORS = {"role=banner", "role=contentinfo", "role=navigation", "role=main"}


@dataclass
class Thresholds:
    slower_pct: float = 20.0
    slower_min_ms: float = 100.0
    layout_shift: float = 0.05
    size_px: float = 8.0
    size_pct: float = 10.0
    strict_values: bool = False  # both builds share one backend, so volatile values must match too


def _slower(current: float | None, candidate: float | None, t: Thresholds) -> bool:
    if current is None or candidate is None:
        return False
    delta = candidate - current
    return delta >= t.slower_min_ms and (current <= 0 or delta / current * 100 >= t.slower_pct)


def _size_changed(current: RegionObservation, candidate: RegionObservation, t: Thresholds) -> bool:
    if not current.box or not candidate.box:
        return False
    for before, after in zip(current.box[2:], candidate.box[2:]):
        delta = abs(after - before)
        if delta >= t.size_px and (before <= 0 or delta / before * 100 >= t.size_pct):
            return True
    return False


def _short_diff(before: str, after: str, limit: int = 20) -> str:
    lines = difflib.unified_diff(before.splitlines(), after.splitlines(), "current", "candidate", lineterm="", n=1)
    return "\n".join(list(lines)[:limit])


def _matches(rule: str | None, text: str) -> bool:
    return rule is None or re.search(rule, text.strip()) is not None


def _compare_region(region: Region, c: RegionObservation, k: RegionObservation, t: Thresholds) -> list[tuple[str, str, str, str]]:
    """(kind, message, current, candidate) tuples for one region."""
    found = []
    if c.status in PRESENT and k.status not in PRESENT:
        return [("missing", f"Present on current, {k.status} on candidate", c.text, k.detail or k.status)]
    if k.status in PRESENT and c.status not in PRESENT:
        return [("extra", f"Not on current ({c.status}), present on candidate", c.detail or c.status, k.text)]
    if c.status not in PRESENT:
        return []  # absent on both: nothing to compare
    if c.status == "settled" and k.status == "stuck":
        found.append(("stuck", "Never settled on candidate (kept changing)", c.text, k.text))
    if _slower(c.settled_ms, k.settled_ms, t):
        found.append(("slow", f"Settled {k.settled_ms - c.settled_ms:.0f} ms later", f"{c.settled_ms} ms", f"{k.settled_ms} ms"))
    if _size_changed(c, k, t):
        found.append(("layout", "Region size changed", f"{c.box}", f"{k.box}"))

    if region.tolerance is Tolerance.VOLATILE:
        if _matches(region.format_rule, c.text) and not _matches(region.format_rule, k.text):
            found.append(("format", f"Candidate value breaks format rule {region.format_rule}", c.text, k.text))
        elif t.strict_values and c.text.strip() != k.text.strip():
            found.append(("value", "Same sample, same backend, different value", c.text, k.text))
    elif region.tolerance is Tolerance.STRUCTURE:
        if normalize_aria(c.aria) != normalize_aria(k.aria):
            found.append(("structure", "Accessibility tree differs:\n" + _short_diff(c.aria, k.aria), c.aria, k.aria))
    else:
        if c.enabled != k.enabled:
            found.append(("behavior", f"Enabled: {c.enabled} -> {k.enabled}", str(c.enabled), str(k.enabled)))
        elif normalize_aria(c.aria) != normalize_aria(k.aria):
            found.append(("behavior", "Control's accessible name/role differs", c.aria, k.aria))
    return found


def _order(side: SideRun, region_ids: list[str]) -> list[str]:
    boxes = {r.region_id: r.box for r in side.regions if r.region_id in region_ids and r.box}
    return sorted(boxes, key=lambda rid: (round(boxes[rid][1]), round(boxes[rid][0])))


def _any_present(side: SideRun) -> bool:
    return any(r.status in PRESENT for r in side.regions)


def _main_shrank(contract: PageContract, current: SideRun, candidate: SideRun) -> bool:
    """The page's main area holds only a fraction of its text on the candidate (e.g. a 'coming soon' placeholder)."""
    main = next((r.region_id for r in contract.regions if r.locator == "role=main"), None)
    c, k = (current.region(main), candidate.region(main)) if main else (None, None)
    if not c or not k or c.status not in PRESENT:
        return False
    before, after = len(c.text.strip()), len(k.text.strip()) if k.status in PRESENT else 0
    return before >= 80 and after < 0.25 * before


def compare_cell(run: CellRun, contract: PageContract, t: Thresholds) -> tuple[list[Diff], list[str]]:
    """Return (diffs, notes). Notes are coverage gaps, not differences between builds."""
    current, candidate = run.current, run.candidate
    diffs: list[Diff] = []
    notes: list[str] = []

    def add(kind: str, message: str, before: str = "", after: str = "", region: Region | None = None) -> None:
        diffs.append(Diff(kind=kind, page_id=run.cell.page_id, cell_label=run.cell.label, message=message,
                          region_id=region.region_id if region else None,
                          tolerance=region.tolerance.value if region else None,
                          component=region.component if region else contract.component,
                          current=before, candidate=after))

    if current.error or candidate.error:
        if (current.error is None) != (candidate.error is None):
            add("page_error", "Page could not be checked on one build", current.error or "ok", candidate.error or "ok")
        else:
            notes.append(f"{run.cell.label}: both builds failed: {current.error}")
        return diffs, notes

    # Both builds bounced elsewhere (typically a sign-in page): equal, but nothing was checked.
    if (current.final_path == candidate.final_path != run.cell.path
            and not _any_present(current) and not _any_present(candidate)):
        notes.append(f"{run.cell.label}: both builds ended on {current.final_path} instead of {run.cell.path}; "
                     "page not checked (does it require login? set requires_login and a login section)")
        return diffs, notes

    if current.final_path != candidate.final_path:
        add("navigation", "Ended on a different page", current.final_path, candidate.final_path)

    # Most of the page's own content absent on the candidate: report the page once, not every region.
    own = [r for r in contract.regions if r.locator not in SITE_WIDE_LOCATORS]
    shown = [r for r in own if (c := current.region(r.region_id)) and c.status in PRESENT]
    gone = [r for r in shown if (k := candidate.region(r.region_id)) is None or k.status not in PRESENT]
    if (shown and len(gone) >= 0.75 * len(shown)) or (_main_shrank(contract, current, candidate)
                                                       and (not shown or len(gone) >= 0.5 * len(shown))):
        add("page_missing", "Page content not found on the new build (not built yet, or broken)",
            f"{len(shown)} sections shown", f"{len(shown) - len(gone)} of them shown")
        return diffs, notes

    for region in contract.regions:
        c, k = current.region(region.region_id), candidate.region(region.region_id)
        if c is None or k is None:
            continue
        if c.status == "unlocatable":
            notes.append(f"{run.cell.label}: region '{region.region_id}' not checked ({c.detail})")
            continue
        for kind, message, before, after in _compare_region(region, c, k, t):
            add(kind, message, before, after, region)

    common = [r.region_id for r in current.regions
              if r.status in PRESENT and (k := candidate.region(r.region_id)) and k.status in PRESENT]
    before_order, after_order = _order(current, common), _order(candidate, common)
    if before_order != after_order:
        add("order", "Regions appear in a different top-to-bottom order", " > ".join(before_order), " > ".join(after_order))

    for metric, before in current.metrics.items():
        after = candidate.metrics.get(metric)
        if _slower(before, after, t):
            add("slow", f"Page {metric} slower by {after - before:.0f} ms", f"{before} ms", f"{after} ms")
    if (current.layout_shift or 0) + t.layout_shift < (candidate.layout_shift or 0):
        add("layout_shift", "More layout shift while loading", f"{current.layout_shift}", f"{candidate.layout_shift}")
    for error in sorted(set(candidate.console_errors) - set(current.console_errors)):
        add("console", "New console error", "", error)
    for failure in sorted(set(candidate.network_failures) - set(current.network_failures)):
        add("network", "New failed request", "", failure)
    return diffs, notes


def compare_flows(pairs: list[dict], t: Thresholds) -> list[Diff]:
    diffs = []
    for pair in pairs:
        if "error" in pair:
            continue  # reported as a note by the caller
        result = compare_journey(pair["current"], pair["candidate"], t.slower_pct, t.slower_min_ms)
        if result["verdict"] in ("UNCHANGED", "IMPROVED"):
            continue
        diffs.append(Diff(
            kind="journey", page_id=f"journey:{pair['journey_id']}", cell_label=f"{pair['journey_id']} {result['name']}",
            message=f"{result['verdict']}: " + "; ".join(result["findings"][:5]),
            current=pair["current"]["classification"], candidate=pair["candidate"]["classification"],
        ))
    return diffs
