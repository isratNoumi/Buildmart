"""Agent 5 - Reporter: report.json, report.html (side by side) and one defect file per unique problem.

Defect files are shaped like Jira issues so they can be filed automatically later.
"""

from __future__ import annotations

import json
import os
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import quote

from agents.discovery.contracts.profile import SiteProfile
from agents.discovery.session import resolve

from .models import CellRun, Diff, Judgment
from .summary import friendly_region, summarize

COLOURS = {"regression": "#b42318", "needs_review": "#b54708", "cosmetic": "#475467", "expected": "#067647",
           "settled": "#067647", "stuck": "#b54708", "missing": "#b42318", "unlocatable": "#98a2b3"}
VERDICT_STYLE = {"unreliable": ("#fff4e5", "#b54708"), "regressions": ("#fdecea", "#b42318"),
                 "review": ("#fff8e1", "#8a5a00"), "clean": ("#e7f6ee", "#067647"), "nothing": ("#f2f4f7", "#475467")}
LABELS = {"regression": "Regression", "needs_review": "Needs review", "cosmetic": "Cosmetic", "expected": "Expected"}
STYLE = """
body{font-family:Inter,"Segoe UI",system-ui,sans-serif;margin:0;color:#101828;background:#f5f6fa;line-height:1.5}
main{max-width:1100px;margin:0 auto;padding:28px 24px 60px}
h1{margin:0 0 4px;font-size:24px}h2{margin:0 0 10px;font-size:18px}h3{margin:14px 0 6px;font-size:14px}
section{background:#fff;border:1px solid #e3e6ee;border-radius:12px;padding:18px 20px;margin:16px 0}
.verdict{border-radius:12px;padding:18px 20px;margin:18px 0;font-size:17px;font-weight:600}
.stats{display:flex;flex-wrap:wrap;gap:8px 22px;margin-top:8px;font-size:14px;font-weight:400}
.caution{background:#fff8e1;border-radius:10px;padding:10px 14px;margin:8px 0}
.item{border-top:1px solid #e3e6ee;padding:12px 0}.item:first-of-type{border-top:0}
.item strong{font-size:15px}.muted{color:#5d6680;font-size:13px}
.change{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:8px 0}
.change div{background:#f5f6fa;border-radius:8px;padding:8px 10px;font-size:13px;white-space:pre-wrap;word-break:break-word}
.badge{display:inline-block;color:#fff;border-radius:999px;padding:1px 10px;font-size:12px;font-weight:600;margin-right:6px}
table{border-collapse:collapse;width:100%;font-size:13px;margin:8px 0}
th,td{border-bottom:1px solid #e3e6ee;padding:6px 8px;text-align:left;vertical-align:top}th{color:#5d6680;font-weight:600}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:16px}.cols img{width:100%;border:1px solid #e3e6ee;border-radius:8px}
details{margin-top:6px}summary{cursor:pointer;color:#4f46e5;font-size:13px}
pre{white-space:pre-wrap;font-size:12px;margin:6px 0}
"""


def _badge(text: str) -> str:
    return f'<span class="badge" style="background:{COLOURS.get(text, "#475467")}">{escape(text)}</span>'


def _img(path: str | None, out_dir: Path, label: str) -> str:
    if not path:
        return f"<div><h4>{escape(label)}</h4><p class='muted'>No screenshot</p></div>"
    src = quote(os.path.relpath(path, out_dir).replace("\\", "/"))
    return f'<div><h4>{escape(label)}</h4><img src="{escape(src)}" alt="{escape(label)} screenshot"></div>'


def _ms(value: float | None) -> str:
    return "" if value is None else f"{value:,.0f}"


def repro_steps(run: CellRun, profile: SiteProfile, candidate_url: str, current_url: str) -> list[str]:
    steps = ["Sign in as the site profile's test account"] if run.cell.requires_login else []
    for key in run.cell.sample:
        for step in profile.setup.get(key, []):
            # secrets=False keeps {env:NAME} placeholders: passwords never reach a defect file
            value = f' "{resolve(step.value or "", run.cell.sample, secrets=False)}"' if step.action == "fill" else ""
            steps.append(f"{step.action} {resolve(step.target, run.cell.sample, secrets=False)}{value}")
    steps.append(f"Open {candidate_url.rstrip('/')}{run.cell.path}")
    steps.append(f"Compare with {current_url.rstrip('/')}{run.cell.path}")
    return steps


def _timeline(run: CellRun) -> list[dict[str, Any]]:
    rows = []
    for c in run.current.regions:
        k = run.candidate.region(c.region_id)
        rows.append({"region": c.region_id,
                     "current": {"status": c.status, "appeared_ms": c.appeared_ms, "settled_ms": c.settled_ms},
                     "candidate": {"status": k.status if k else "missing",
                                   "appeared_ms": k.appeared_ms if k else None, "settled_ms": k.settled_ms if k else None}})
    return rows


def write_defects(out_dir: Path, judged: list[tuple[Diff, Judgment]], runs: list[CellRun], profile: SiteProfile,
                  urls: dict[str, str], versions: dict[str, str], component_files: dict[str, str]) -> list[Path]:
    """One file per unique fingerprint judged regression or needs_review."""
    runs_by_label = {r.cell.label: r for r in runs}
    grouped: dict[str, tuple[Diff, Judgment, list[str]]] = {}
    for diff, judgment in judged:
        if judgment.classification in ("regression", "needs_review"):
            grouped.setdefault(diff.fingerprint, (diff, judgment, []))[2].append(diff.cell_label)
    folder = out_dir / "defects"
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for fingerprint, (diff, judgment, cells) in grouped.items():
        run = runs_by_label.get(diff.cell_label)
        defect = {
            "summary": f"[parity] {diff.page_id}: {diff.kind}" + (f" in {diff.region_id}" if diff.region_id else ""),
            "issue_type": "Bug",
            "priority": "High" if judgment.classification == "regression" else "Medium",
            "labels": ["parity", diff.kind, judgment.classification],
            "status_hint": "needs human review" if judgment.classification == "needs_review" else "confirmed by judge",
            "description": f"{diff.message}\n\nJudge ({judgment.source}, confidence {judgment.confidence:.2f}): {judgment.reason}",
            "expected_current": diff.current,
            "actual_candidate": diff.candidate,
            "steps_to_reproduce": repro_steps(run, profile, urls["candidate"], urls["current"]) if run else [],
            "affected_samples": cells,
            "suspected_component": diff.component,
            "suspected_file": component_files.get(diff.component or ""),
            "region_timeline": _timeline(run) if run else [],
            "attachments": [s for s in ((run.current.screenshot, run.candidate.screenshot) if run else ()) if s],
            "versions": versions,
            "fingerprint": fingerprint,
        }
        path = folder / f"{fingerprint}.json"
        path.write_text(json.dumps(defect, indent=2) + "\n", encoding="utf-8")
        paths.append(path)
    return paths


def _cell_html(run: CellRun, out_dir: Path) -> str:
    rows = "".join(
        f"<tr><td>{escape(friendly_region(r['region']))}</td>"
        + "".join(f"<td>{_badge(r[side]['status'])} {_ms(r[side]['settled_ms'])}{' ms' if r[side]['settled_ms'] is not None else ''}</td>"
                  for side in ("current", "candidate"))
        + "</tr>"
        for r in _timeline(run)
    )
    errors = "".join(f"<p class='caution'>{escape(s.build)} build: {escape(s.error)}</p>" for s in (run.current, run.candidate) if s.error)
    return f"""
<details><summary>{escape(run.cell.label)} - {escape(run.cell.path)}</summary>{errors}
<div class="cols">{_img(run.current.screenshot, out_dir, 'Current build')}{_img(run.candidate.screenshot, out_dir, 'New build')}</div>
<h3>When each section finished loading</h3>
<table><tr><th>Section</th><th>Current build</th><th>New build</th></tr>{rows}</table>
</details>"""


def _list(items: list[str]) -> str:
    return "<ul>" + "".join(f"<li>{item}</li>" for item in items) + "</ul>"


def render_html(report: dict[str, Any], runs: list[CellRun], judged: list[tuple[Diff, Judgment]], out_dir: Path) -> str:
    full = {**report,
            "diffs": report.get("diffs") or [{**d.model_dump(), "fingerprint": d.fingerprint, "judgment": j.model_dump()}
                                             for d, j in judged],
            "runs": report.get("runs") or [r.model_dump() for r in runs]}
    s = summarize(full)
    st = s["stats"]
    background, colour = VERDICT_STYLE[s["verdict"]]
    versions, urls = report.get("versions", {}), report.get("urls", {})

    sections = []
    if s["setup_problems"]:
        sections.append("<section><h2>1. Fix the test setup first</h2><p class='muted'>These checks failed because of "
                        "the test environment, not the app, so their pages weren't really compared.</p>" + "".join(
            f"<div class='item'><strong>{escape(p['text'])}</strong><div class='muted'>{p['count']} checks, e.g. "
            f"{escape(', '.join(p['examples']))}</div></div>" for p in s["setup_problems"]) + "</section>")
    sel = s["selection"]
    body = ""
    if sel["mode"] == "all":
        body += "<p class='muted'>All pages were checked (no change list given).</p>"
    else:
        body += f"<p class='muted'>{len(sel['changed_files'])} changed file(s) decided which pages to check.</p>" + "".join(
            f"<div class='item'><strong>{escape(w['page'])}</strong><div class='muted'>{escape('; '.join(w['reasons']))}</div></div>"
            for w in sel["why"])
        if sel["not_affected"]:
            body += f"<p class='muted'>Not checked: {escape(', '.join(n['page'] + ' (' + n['reason'] + ')' for n in sel['not_affected']))}</p>"
        if sel["ignored_files"]:
            body += f"<p class='muted'>Ignored files: {escape(', '.join(f['file'] for f in sel['ignored_files']))}</p>"
    if s["not_on_new_build"]:
        body += ("<h3>Not on the new build yet (skipped)</h3><p class='muted'>If any of these should already exist, "
                 "treat it as a regression.</p>" + "".join(
            f"<div class='item'><strong>{escape(p['page'])}</strong><div class='muted'>{escape(p['reason'])}"
            + (f" ({p['checks']} checks)" if p["checks"] else "") + "</div></div>" for p in s["not_on_new_build"]))
    sections.append(f"<section><h2>2. Which pages, and why</h2>{body}</section>")

    problems = "".join(
        f"<div class='item'>{_badge(p['verdict'])}<strong>{escape(p['text'])}</strong>"
        f"<div class='muted'>Page: {escape(p['page'])} - seen in {len(p['samples'])} check(s)</div>"
        + (f"<div class='change'><div><b>Current build</b><br>{escape(p['before'])}</div>"
           f"<div><b>New build</b><br>{escape(p['after'])}</div></div>" if p["before"] or p["after"] else "")
        + (f"<div class='muted'>Judge: {escape(p['reason'])}</div>" if p["reason"] else "")
        + f"<details><summary>Technical details</summary><pre>{escape(p['detail'])}\nkind: {escape(p['kind'])}"
        f"\nfingerprint: {escape(p['fingerprint'])}\nchecks: {escape(', '.join(p['samples']))}</pre></details></div>"
        for p in s["problems"]
    ) or "<p class='muted'>No differences in the app on the pages that were compared.</p>"
    sections.append(f"<section><h2>3. Differences in the app ({st['problems']})</h2>{problems}</section>")

    coverage = "".join(
        f"<tr><td>{escape(c['page'])}</td><td>{c['checks']}</td><td>{len(c['checked'])} of "
        f"{len(c['checked']) + len(c['not_checked'])}</td><td>{escape(', '.join(c['not_checked'])) or '-'}</td></tr>"
        for c in s["coverage"])
    uncovered = _list([escape(u) for u in s["uncovered"]]) if s["uncovered"] else ""
    sections.append("<section><h2>4. What was checked</h2><table><tr><th>Page</th><th>Checks</th><th>Sections checked</th>"
                    f"<th>Not checked (no locator yet)</th></tr>{coverage}</table>{uncovered}</section>")
    absent = {p["page"] for p in s["not_on_new_build"]}
    shown = [r for r in runs if r.cell.page_id not in absent]
    sections.append(f"<section><h2>5. Page by page</h2><p class='muted'>Screenshots side by side (prices and stock "
                    f"hidden) and when each section finished loading.</p>{''.join(_cell_html(r, out_dir) for r in shown)}</section>")

    cautions = "".join(f"<div class='caution'>{escape(c)}</div>" for c in s["cautions"])
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Comparison report - {escape(report['site'])}</title><style>{STYLE}</style></head>
<body><main>
<h1>Comparison report: {escape(report['site'])}</h1>
<p class="muted">Current build {escape(versions.get('current', ''))} ({escape(urls.get('current', ''))}) vs new build
{escape(versions.get('candidate', ''))} ({escape(urls.get('candidate', ''))}) - {escape(report.get('generated_at', ''))}</p>
<div class="verdict" style="background:{background};color:{colour}">{escape(s['verdict_text'])}
<div class="stats"><span>{st['checks']} checks on {st['pages']} pages</span><span>{st['problems']} differences</span>
<span>{st['regressions']} regressions</span><span>{st['setup_failures']} checks hit setup problems</span>
<span>{st['sections_checked']} of {st['sections_total']} page sections checked</span></div></div>
{cautions}
{''.join(sections)}
</main></body></html>
"""
