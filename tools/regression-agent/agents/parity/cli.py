"""Run the five parity agents end to end.

    python -m agents.parity run --site-profile baseline/site_profiles/<site>.yaml \\
        --current-url http://127.0.0.1:5173 --candidate-url http://127.0.0.1:5174 --all
    python -m agents.parity run ... --base-ref main --repo-root path/to/repo --source-dir path/to/repo/react19
                                                    # only pages affected by the changes since main
    python -m agents.parity label FINGERPRINT regression --note "price missing"  # confirm a judgment
    python -m agents.parity summary-md output/parity_runs/<run>    # Markdown summary (e.g. for GitHub)
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import yaml

from agents.discovery.contracts.models import ContractStatus
from agents.discovery.contracts.profile import load_profile
from agents.discovery.contracts.store import load_all
from agents.discovery.models import ApplicationModel
from agents.planner.select import changed_files, select_pages
from agents.planner.source_map import pages_not_built, select_pages_from_source

from ..paths import DISCOVERY_DIR, EXPECTED_CHANGES_FILE, PARITY_CACHE_DIR, PARITY_RUNS_DIR
from . import availability, comparator, driver, planner, reporter, summary
from .judge import Judge, add_label, ask_claude
from .models import ExpectedChange

DEFAULT_CACHE_DIR = PARITY_CACHE_DIR


def _selected_pages(args: argparse.Namespace, model: ApplicationModel) -> tuple[set[str] | None, dict]:
    if args.all:
        return None, {"mode": "all"}
    files = changed_files(args.repo_root, args.base_ref) if args.base_ref else args.changed_file
    if args.source_dir:
        selection = select_pages_from_source(files, args.source_dir, args.repo_root, model)
    else:
        selection = select_pages(files, args.discovery_dir)
    return {s["id"] for s in selection["selected"]}, selection


def _flows_within(model: ApplicationModel, page_ids: set[str]) -> set[str]:
    """Flows whose every step is on one of these pages (journeys through other pages would fail for that reason)."""
    page_of = {key: p.page_id for p in model.pages for key in (p.page_id, p.route)}
    return {f.flow_id for f in model.flows if f.steps and all(page_of.get(s.page) in page_ids for s in f.steps)}


def _load_expected(path: Path | None) -> list[ExpectedChange]:
    path = path or (EXPECTED_CHANGES_FILE if EXPECTED_CHANGES_FILE.exists() else None)
    if path is None:
        return []
    return [ExpectedChange.model_validate(e) for e in yaml.safe_load(path.read_text(encoding="utf-8")) or []]


def cmd_run(args: argparse.Namespace) -> int:
    profile = load_profile(args.site_profile)
    app_json = args.discovery_dir / "application.json"
    model = ApplicationModel.model_validate_json(app_json.read_text(encoding="utf-8")) if app_json.exists() else ApplicationModel()
    contracts = {c.page_id: c for c in load_all(args.discovery_dir / "contracts")}
    known_pages = set(contracts) | {p.page_id for p in model.pages}
    if args.page and set(args.page) - known_pages:
        print(f"Unknown page(s): {', '.join(sorted(set(args.page) - known_pages))}. Known: {', '.join(sorted(known_pages))}")
        return 2

    # 1. Planner: pages affected by the changes (or all), minus pages the new build's code doesn't have yet
    page_ids, selection = _selected_pages(args, model)
    if args.page:
        page_ids = set(args.page) if page_ids is None else page_ids & set(args.page)
    not_built = pages_not_built(args.source_dir, model) if args.source_dir else {}
    not_built = {p: info for p, info in not_built.items() if page_ids is None or p in page_ids}
    if not_built:
        page_ids = (known_pages if page_ids is None else page_ids) - set(not_built)
        print(f"Not built in the new build's code, skipped: {', '.join(sorted(not_built))}")
    cells, uncovered = planner.plan(list(contracts.values()), profile, page_ids, args.iteration, args.per_page)
    drafts = sorted({c.page_id for c in cells if contracts[c.page_id].status is ContractStatus.DRAFT})
    print(f"Planner: {len(cells)} page checks over {len({c.page_id for c in cells})} pages; {len(uncovered)} not coverable.")

    now = datetime.now(timezone.utc)
    out_dir = (args.output_dir or PARITY_RUNS_DIR / now.strftime("%Y-%m-%dT%H-%M-%S")).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # 2+3. Driver and Comparator on pages, then drop the pages the new build doesn't have yet
    print("Driver: running both builds ...")
    runs = driver.run_cells(cells, contracts, profile, args.current_url, args.candidate_url, out_dir, not args.headed) if cells else []
    thresholds = comparator.Thresholds(slower_pct=args.slower_threshold_pct, slower_min_ms=args.slower_min_ms,
                                       strict_values=args.strict_values)
    diffs, notes = [], []
    for run in runs:
        cell_diffs, cell_notes = comparator.compare_cell(run, contracts[run.cell.page_id], thresholds)
        diffs += cell_diffs
        notes += cell_notes
    missing = {**not_built, **availability.unavailable_pages(runs, diffs)}
    diffs = [d for d in diffs if d.page_id not in missing]
    notes = [n for n in notes if n.split(" [", 1)[0] not in missing]
    compared = {r.cell.page_id for r in runs} - set(missing)
    if missing:
        print(f"Not on the new build yet: {', '.join(sorted(missing))}")

    # Journeys only through compared pages: others would fail just because a page isn't there yet
    flow_ids = _flows_within(model, compared)
    routes = {c.route for c in contracts.values()} | {r.route for r in model.routes}
    protected = {c.route for c in contracts.values() if c.requires_login} | {r.route for r in model.routes if r.protected}
    pairs = [] if args.skip_journeys or not flow_ids else driver.run_flows(
        flow_ids, routes, protected, profile, args.current_url, args.candidate_url, not args.headed)
    diffs += comparator.compare_flows(pairs, thresholds)
    notes += [f"journey {p['journey_id']} not checked: {p['error']}" for p in pairs if "error" in p]
    print(f"Comparator: {len(diffs)} differences survived filtering.")

    # 4. Judge
    judge = Judge(args.cache_dir, profile.name, _load_expected(args.expected_changes), ask=None if args.no_llm else ask_claude)
    judged = [(diff, judge.judge(diff)) for diff in diffs]

    # 5. Reporter
    versions = {"current": args.current_label, "candidate": args.candidate_label}
    urls = {"current": args.current_url, "candidate": args.candidate_url}
    component_files = {c.name: c.file for c in model.components}
    defects = reporter.write_defects(out_dir, judged, runs, profile, urls, versions, component_files)
    counts = Counter(j.classification for _, j in judged)
    report = {
        "generated_at": now.isoformat(), "site": profile.name, "urls": urls, "versions": versions,
        "iteration": args.iteration, "selection": selection, "uncovered": uncovered, "notes": notes,
        "not_on_new_build": sorted(missing.values(), key=lambda p: p["page"]),
        "compared_pages": sorted(compared), "draft_pages": drafts,
        "journeys_skipped": 0 if args.skip_journeys else len(model.flows) - len(flow_ids),
        "summary": {"cells": len(runs), "diffs": len(diffs), "defects": len(defects), **counts},
        "runs": [r.model_dump() for r in runs], "journeys": pairs,
        "diffs": [{**d.model_dump(), "fingerprint": d.fingerprint, "judgment": j.model_dump()} for d, j in judged],
    }
    (out_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (out_dir / "report.html").write_text(reporter.render_html(report, runs, judged, out_dir), encoding="utf-8")

    print(f"Judge: {dict(counts) or 'no differences'}")
    print(f"Reporter: {len(defects)} defect file(s); report: {out_dir / 'report.html'}")
    return 1 if counts.get("regression") else 0


def cmd_summary_md(run_dir: Path) -> int:
    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    sys.stdout.reconfigure(encoding="utf-8")  # the summary has emoji; Windows consoles default to cp1252
    print(summary.to_markdown(summary.summarize(report)))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Plan, drive, compare, judge and report")
    run.add_argument("--site-profile", type=Path, required=True)
    run.add_argument("--current-url", required=True)
    run.add_argument("--candidate-url", required=True)
    run.add_argument("--current-label", default="current", help="Version tag recorded on every result")
    run.add_argument("--candidate-label", default="candidate")
    source = run.add_mutually_exclusive_group(required=True)
    source.add_argument("--all", action="store_true", help="Check every page")
    source.add_argument("--base-ref", help="Only pages affected by changes since this git ref (plus uncommitted ones)")
    source.add_argument("--changed-file", action="append", help="Only pages affected by this file; repeatable")
    run.add_argument("--repo-root", type=Path, default=Path.cwd(), help="Git repo of the app; changed paths are relative to it")
    run.add_argument("--source-dir", type=Path, help="The new build's frontend folder (e.g. repo/react19): pages are "
                                                      "selected from its routes and imports, and pages it has no "
                                                      "real route for are skipped without opening a browser")
    run.add_argument("--discovery-dir", type=Path, default=DISCOVERY_DIR)
    run.add_argument("--page", action="append", help="Only compare this page (repeatable)")
    run.add_argument("--iteration", type=int, default=0, help="Seed for sample rotation; record it to reproduce")
    run.add_argument("--per-page", type=int, default=2, help="Rotated samples per page, on top of pinned ones")
    run.add_argument("--strict-values", action="store_true", help="Volatile values must also be equal (same backend)")
    run.add_argument("--expected-changes", type=Path, help="YAML list of {page_id, region_id, kind, reason}; "
                                                             "default baseline/expected_changes.yaml if present")
    run.add_argument("--no-llm", action="store_true", help="Skip the AI judge; surviving diffs need review")
    run.add_argument("--skip-journeys", action="store_true", help="Don't run linked journeys for behaviour")
    run.add_argument("--headed", action="store_true")
    run.add_argument("--slower-threshold-pct", type=float, default=20.0)
    run.add_argument("--slower-min-ms", type=float, default=100.0)
    run.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    run.add_argument("--output-dir", type=Path)

    label = sub.add_parser("label", help="Record a human judgment for a diff fingerprint")
    label.add_argument("fingerprint")
    label.add_argument("classification", choices=["expected", "cosmetic", "regression"])
    label.add_argument("--note", default="")
    label.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)

    markdown = sub.add_parser("summary-md", help="Print a run's plain-language summary as Markdown")
    markdown.add_argument("run_dir", type=Path)

    args = parser.parse_args(argv)
    if args.command == "label":
        add_label(args.cache_dir, args.fingerprint, args.classification, args.note)
        print(f"Labelled {args.fingerprint} as {args.classification}")
        return 0
    if args.command == "summary-md":
        return cmd_summary_md(args.run_dir)
    return cmd_run(args)
