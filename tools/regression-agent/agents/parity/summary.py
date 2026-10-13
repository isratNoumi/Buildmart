"""Turn a raw report into plain-language answers, for report.html and the console.

    1. Can this run be trusted?        problems caused by the test setup, not the app
    2. Which pages, and why?           pages picked from the code changes; pages not on the new build yet
    3. What is different?              one entry per unique problem, in plain words
    4. What wasn't checked?            sections without a locator, pages without a sample
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any
from urllib.parse import urlsplit

_ROLE = re.compile(r'role=(\w+)(?:\[name=(?:"([^"]*)"|/\^?(.*?)\$?/)\])?')
_SETUP_ERROR = re.compile(r"ERR_CONNECTION|ERR_NAME_NOT_RESOLVED|ERR_TIMED_OUT|net::ERR_|signing in failed|/@vite/|/@react-refresh"
                          r"|setup for|no login section")

VERDICTS = {
    "unreliable": "This run can't be trusted yet: many checks failed because of the test setup, not the app.",
    "regressions": "Problems found that change what users see or can do.",
    "review": "Differences found. Decide for each one whether it matters.",
    "clean": "No differences found between the two builds on the pages that were compared.",
    "nothing": "Nothing was compared: no page affected by these changes exists on the new build yet.",
}


def friendly_region(region_id: str | None) -> str:
    """'role=button[name="Add to cart"]' -> '"Add to cart" button'. Other names are kept."""
    if not region_id:
        return "Whole page"
    match = _ROLE.match(region_id)
    if not match:
        return region_id
    role, name, pattern = match.groups()
    label = name if name is not None else (f"{pattern.strip()}..." if pattern else "")
    return f'"{label}" {role}' if label else f"{role} section"


def _plain(diff: dict[str, Any]) -> str:
    where = friendly_region(diff.get("region_id"))
    kind, message = diff["kind"], diff.get("message", "")
    if kind == "journey":
        return f"User journey \"{diff['cell_label']}\" behaves differently"
    return {
        "missing": f"{where} is missing on the new build",
        "extra": f"{where} appears only on the new build",
        "stuck": f"{where} never finishes loading on the new build",
        "slow": f"{where} loads slower on the new build" if diff.get("region_id") else f"Page loads slower ({message})",
        "format": f"{where} shows a value in an unexpected format",
        "value": f"{where} shows a different value",
        "structure": f"{where} is built differently (headings, links or labels changed)",
        "behavior": f"{where} behaves differently ({message})",
        "layout": f"{where} changed size",
        "order": "Sections appear in a different order",
        "layout_shift": "Content jumps around more while the page loads",
        "console": "A new error appears in the browser console",
        "network": "A request that worked now fails",
        "navigation": "The page ends up on a different address",
    }.get(kind, message)


def _is_setup(diff: dict[str, Any]) -> bool:
    text = f"{diff.get('candidate', '')} {diff.get('current', '')} {diff.get('message', '')}"
    return diff["kind"] == "page_error" or bool(_SETUP_ERROR.search(text))


def _explain_setup(text: str) -> str:
    if "signing in failed" in text:
        reason = text.split("signing in failed:", 1)[-1].strip().split("\n")[0]
        return (f"Couldn't sign in with the test account ({reason}). Check the profile's login section and the "
                "TEST_USER_EMAIL / TEST_USER_PASSWORD settings.")
    if "no login section" in text:
        return "Some pages need login, but the site profile has no login section."
    if "ERR_CONNECTION_REFUSED" in text or "ERR_CONNECTION_RESET" in text:
        return "A build could not be reached (connection refused or reset). Is its server running and stable?"
    if "/@vite/" in text or "/@react-refresh" in text:
        return "A development server's own files failed to load. Compare a production build (build + preview) instead."
    if "setup for" in text:
        return f"A sample's setup step failed: {text.split('setup for', 1)[-1].strip()[:160]}"
    return text.strip().splitlines()[0][:200]


def _is_local(url: str) -> bool:
    return (urlsplit(url).hostname or "") in ("localhost", "127.0.0.1")


def summarize(report: dict[str, Any]) -> dict[str, Any]:
    diffs = report.get("diffs") or []
    runs = report.get("runs") or []
    urls = report.get("urls") or {}

    setup: dict[str, set[str]] = defaultdict(set)
    problems: dict[str, dict[str, Any]] = {}
    for diff in diffs:
        if _is_setup(diff):
            setup[_explain_setup(diff.get("candidate") or diff.get("message", ""))].add(diff["cell_label"])
        else:
            entry = problems.setdefault(diff["fingerprint"], {
                "fingerprint": diff["fingerprint"],
                "page": diff["page_id"].removeprefix("journey:"),
                "where": friendly_region(diff.get("region_id")),
                "kind": diff["kind"],
                "text": _plain(diff),
                "detail": diff.get("message", ""),
                "before": (diff.get("current") or "")[:200],
                "after": (diff.get("candidate") or "")[:200],
                "verdict": (diff.get("judgment") or {}).get("classification", "needs_review"),
                "reason": (diff.get("judgment") or {}).get("reason", ""),
                "samples": [],
            })
            entry["samples"].append(diff["cell_label"])
    for note in report.get("notes") or []:
        label = note.split(":", 1)[0]
        if "not checked (" in note and "region" in note:
            continue
        if "ended on" in note:
            setup["Page redirected on both builds, so it wasn't checked (does it need login?)"].add(label)
        elif _SETUP_ERROR.search(note) or "both builds failed" in note:
            setup[_explain_setup(note.split(":", 1)[-1])].add(label)

    rank = {"regression": 0, "needs_review": 1, "cosmetic": 2, "expected": 3}
    problem_list = sorted(problems.values(), key=lambda p: (rank.get(p["verdict"], 9), p["page"], p["text"]))

    checked: dict[str, set[str]] = defaultdict(set)
    unchecked: dict[str, set[str]] = defaultdict(set)
    checks_per_page: dict[str, int] = defaultdict(int)
    absent = {p["page"] for p in report.get("not_on_new_build") or []}
    for run in runs:
        page = run["cell"]["page_id"]
        if page in absent:
            continue
        checks_per_page[page] += 1
        for region in run["current"].get("regions") or []:
            (unchecked if region["status"] == "unlocatable" else checked)[page].add(friendly_region(region["region_id"]))
    coverage = [{"page": page, "checks": checks_per_page[page], "checked": sorted(checked[page]),
                 "not_checked": sorted(unchecked[page] - checked[page])} for page in sorted(checks_per_page)]
    sections_total = sum(len(c["checked"]) + len(c["not_checked"]) for c in coverage)
    sections_checked = sum(len(c["checked"]) for c in coverage)

    affected = set().union(*setup.values()) if setup else set()
    regressions = sum(p["verdict"] == "regression" for p in problem_list)
    journeys = report.get("journeys") or []
    if runs and len(affected) > max(1, len(runs) // 10):
        verdict = "unreliable"
    elif regressions:
        verdict = "regressions"
    elif problem_list:
        verdict = "review"
    elif not report.get("compared_pages", coverage) and not journeys:
        verdict = "nothing"
    else:
        verdict = "clean"

    selection = report.get("selection") or {}
    not_on_new_build = report.get("not_on_new_build") or []
    cautions = []
    if urls and _is_local(urls.get("current", "")) != _is_local(urls.get("candidate", "")):
        cautions.append("One build runs on this computer and the other on the internet, so speed differences mostly "
                        "reflect network distance, not code.")
    if sections_total and sections_checked < sections_total / 2:
        cautions.append(f"Only {sections_checked} of {sections_total} page sections could be checked; the rest have no "
                        "locator yet (see 'Not checked').")
    if report.get("draft_pages"):
        cautions.append(f"{len(report['draft_pages'])} page(s) were checked with draft contracts (not reviewed yet): "
                        f"{', '.join(report['draft_pages'])}.")

    return {
        "verdict": verdict,
        "verdict_text": VERDICTS[verdict],
        "stats": {"checks": len(runs), "pages": len(report.get("compared_pages", coverage)), "problems": len(problem_list),
                  "regressions": regressions, "setup_failures": len(affected), "journeys": len(journeys),
                  "sections_checked": sections_checked, "sections_total": sections_total},
        "cautions": cautions,
        "setup_problems": [{"text": text, "count": len(cells), "examples": sorted(cells)[:3]}
                           for text, cells in sorted(setup.items(), key=lambda item: -len(item[1]))],
        "selection": {
            "mode": selection.get("mode", "all"),
            "changed_files": selection.get("changed_files") or [],
            "why": [{"page": s["id"], "reasons": s["reasons"]} for s in selection.get("selected") or []],
            "not_affected": [{"page": s["id"], "reason": "; ".join(s.get("reasons") or [])} for s in selection.get("skipped") or []],
            "ignored_files": selection.get("ignored") or [],
        },
        "not_on_new_build": not_on_new_build,
        "problems": problem_list,
        "coverage": coverage,
        "uncovered": report.get("uncovered") or [],
    }


def _md(text: str) -> str:
    return re.sub(r"([\\`*_{}\[\]<>|])", r"\\\1", str(text)).replace("\n", " ")


def to_markdown(s: dict[str, Any], title: str = "Regression check: current vs new build") -> str:
    """The summary as Markdown, e.g. for a GitHub Actions job summary. All page text is escaped."""
    icon = {"clean": "✅", "nothing": "➖", "review": "🟡", "regressions": "🔴", "unreliable": "⚠️"}[s["verdict"]]
    st = s["stats"]
    lines = [f"## {title}", "", f"**{icon} {_md(s['verdict_text'])}**", "",
             f"{st['pages']} page(s) compared in {st['checks']} check(s), {st['journeys']} journey(s); "
             f"{st['problems']} difference(s), {st['regressions']} regression(s).", ""]
    lines += [f"> {_md(c)}" for c in s["cautions"]]
    if s["setup_problems"]:
        lines += ["", "### Fix first: test setup problems"]
        lines += [f"- {_md(p['text'])} ({p['count']} checks)" for p in s["setup_problems"]]
    sel = s["selection"]
    lines += ["", "### Which pages, and why"]
    if sel["mode"] == "all":
        lines.append("All pages were checked (no change list).")
    else:
        lines.append(f"{len(sel['changed_files'])} changed file(s).")
        lines += [f"- **{_md(w['page'])}**: {_md('; '.join(w['reasons']))}" for w in sel["why"]]
        if sel["not_affected"]:
            lines.append(f"- Not affected: {_md(', '.join(n['page'] for n in sel['not_affected']))}")
        if sel["ignored_files"]:
            lines.append(f"- Ignored files: {_md(', '.join(f['file'] for f in sel['ignored_files']))}")
    if s["not_on_new_build"]:
        lines += ["", "### Not on the new build yet (skipped)", "If any of these should already exist, treat it as a regression."]
        lines += [f"- **{_md(p['page'])}**: {_md(p['reason'])}" for p in s["not_on_new_build"]]
    if s["problems"]:
        lines += ["", "### Differences", "| Verdict | Page | What changed | Current | New |", "|---|---|---|---|---|"]
        lines += [f"| {_md(p['verdict'])} | {_md(p['page'])} | {_md(p['text'])} | {_md(p['before'][:80])} | {_md(p['after'][:80])} |"
                  for p in s["problems"][:50]]
        if len(s["problems"]) > 50:
            lines.append(f"... and {len(s['problems']) - 50} more in report.html")
    lines += ["", "Full report with screenshots: download the run's report artifact and open report.html."]
    return "\n".join(lines) + "\n"
