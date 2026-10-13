"""Which pages aren't on the new build yet, so they are listed as skipped instead of reported as problems.

A page counts as not there when every check of it either
    - lost most of its own content on the new build (comparator's "page_missing"), or
    - needs sign-in and signing in on the new build failed (often: the login page isn't built yet).
A page that is missing on only some checks stays a normal difference: that looks like a bug, not a gap.
"""

from __future__ import annotations

from collections import defaultdict

from .models import CellRun, Diff

CONTENT_MISSING = "Page content not found on the new build (not built yet)"
SIGN_IN_FAILED = "Needs sign-in, and signing in on the new build failed (login page not built yet?)"
MIXED = "Content missing or sign-in failed on the new build"


def _signin_failed(run: CellRun) -> bool:
    return (run.cell.requires_login and run.current.error is None
            and "signing in failed" in (run.candidate.error or ""))


def unavailable_pages(runs: list[CellRun], diffs: list[Diff]) -> dict[str, dict]:
    """page_id -> {"page", "reason", "checks"} for pages the new build doesn't have yet."""
    missing = {(d.page_id, d.cell_label) for d in diffs if d.kind == "page_missing"}
    by_page: dict[str, list[CellRun]] = defaultdict(list)
    for run in runs:
        by_page[run.cell.page_id].append(run)

    pages = {}
    for page_id, page_runs in by_page.items():
        content = [(page_id, r.cell.label) in missing for r in page_runs]
        signin = [_signin_failed(r) for r in page_runs]
        if not all(c or s for c, s in zip(content, signin)):
            continue
        reason = CONTENT_MISSING if all(content) else SIGN_IN_FAILED if all(signin) else MIXED
        pages[page_id] = {"page": page_id, "reason": reason, "checks": len(page_runs)}
    return pages
