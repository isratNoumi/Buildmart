"""Select approved journeys using changed files and discovery dependencies."""

from __future__ import annotations

import argparse
import json
import posixpath
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from agents.discovery.models import ApplicationModel

from .config import DEFAULT_DISCOVERY_DIR, DEFAULT_JOURNEYS_ROOT
from .plan import _load_application_model, _pages_by_id


def _normalize(path: str) -> str:
    return posixpath.normpath(path.replace("\\", "/"))


def changed_files(repo_root: Path, base_ref: str) -> list[str]:
    def git(*args: str) -> list[str]:
        result = subprocess.run(
            ["git", "-C", str(repo_root), *args], check=True, capture_output=True
        )
        return [path for path in result.stdout.decode("utf-8").split("\0") if path]

    paths = git("diff", "--name-only", "-z", "--no-renames", f"{base_ref}...HEAD", "--")
    paths += git("diff", "--name-only", "-z", "--no-renames", "HEAD", "--")
    paths += git("ls-files", "--others", "--exclude-standard", "-z")
    return sorted({_normalize(path) for path in paths})


def source_files_by_node(model: ApplicationModel) -> Callable[[str], set[str]]:
    """Return a function: node name (flow_id, page_id, component...) -> every source file it depends on."""
    pages = _pages_by_id(model)
    dependencies: dict[str, set[str]] = {}
    sources: dict[str, set[str]] = {}

    def register(name: str, record: Any, file: str | None = None) -> None:
        sources.setdefault(name, set()).update(
            _normalize(evidence.file) for evidence in record.evidence if evidence.file
        )
        if file:
            sources[name].add(_normalize(file))

    def link(consumer: str, dependency: str | None) -> None:
        if dependency:
            dependencies.setdefault(consumer, set()).add(dependency)

    for component in model.components:
        register(component.name, component, component.file)
        for child in component.children:
            link(component.name, child)
        if component.parent:
            link(component.parent, component.name)
    for api in model.apis:
        name = f"{api.method} {api.endpoint}"
        register(name, api)
        for consumer in api.used_by:
            link(consumer, name)
    for route in model.routes:
        register(route.route, route, route.source_file)
        link(route.route, route.component)
    for page in model.pages:
        register(page.page_id, page)
        link(page.page_id, page.route)
        link(page.page_id, page.component)
        for dependency in page.components + page.apis:
            link(page.page_id, dependency)
    for element in model.elements:
        if element.component:
            register(element.component, element)
    for dependency in model.dependencies:
        register(dependency.source, dependency)
        link(dependency.source, dependency.target)

    for flow in model.flows:
        register(flow.flow_id, flow)
        for step in flow.steps:
            page = pages.get(step.page)
            link(flow.flow_id, page.page_id if page else step.page)

    def files_for(start: str) -> set[str]:
        visited: set[str] = set()
        pending = [start]
        paths: set[str] = set()
        while pending:
            node = pending.pop()
            if node in visited:
                continue
            visited.add(node)
            paths.update(sources.get(node, set()))
            pending.extend(dependencies.get(node, set()) - visited)
        return paths

    return files_for


def select_pages(files: list[str], discovery_dir: Path = DEFAULT_DISCOVERY_DIR) -> dict[str, Any]:
    """Like select_journeys, but for pages. Unmapped changes conservatively select every page."""
    model = _load_application_model(discovery_dir)
    files_for = source_files_by_node(model)
    page_files = {page.page_id: files_for(page.page_id) for page in model.pages}
    changed = sorted({_normalize(path) for path in files})
    known = set().union(*page_files.values()) if page_files else set()
    unmapped = sorted(set(changed) - known)
    selected, skipped = [], []
    for page_id, paths in page_files.items():
        reasons = [f"Changed dependency: {file}" for file in sorted(set(changed) & paths)]
        if changed and unmapped:
            reasons.append("Conservative fallback: changed files are not mapped by discovery")
        (selected if reasons else skipped).append({"id": page_id, "reasons": reasons})
    return {"changed_files": changed, "unmapped_files": unmapped, "selected": selected, "skipped": skipped}


def select_journeys(
    files: list[str],
    discovery_dir: Path = DEFAULT_DISCOVERY_DIR,
    journeys_root: Path = DEFAULT_JOURNEYS_ROOT,
) -> dict[str, Any]:
    model = _load_application_model(discovery_dir)
    files_for = source_files_by_node(model)
    flow_files = {flow.flow_id: files_for(flow.flow_id) for flow in model.flows}

    changed = sorted({_normalize(path) for path in files})
    known = set().union(*flow_files.values()) if flow_files else set()
    unmapped = sorted(set(changed) - known)
    selected = []
    skipped = []
    for path in sorted(journeys_root.glob("*/*.yaml")):
        spec = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if spec.get("status") != "approved":
            continue
        flow_id = spec.get("provenance", {}).get("source_flow_id")
        affected = sorted(set(changed) & flow_files.get(flow_id, set()))
        reasons = [f"Changed dependency: {file}" for file in affected]
        if changed and unmapped:
            reasons.append("Conservative fallback: changed files are not mapped by discovery")
        if changed and not flow_files.get(flow_id):
            reasons.append("Conservative fallback: journey has no source dependency mapping")
        record = {"id": spec["id"], "name": spec["name"], "reasons": reasons}
        (selected if reasons else skipped).append(record)
    return {
        "changed_files": changed,
        "unmapped_files": unmapped,
        "selected": selected,
        "skipped": skipped,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--base-ref", help="Compare the merge base with HEAD, including local changes")
    source.add_argument("--changed-file", action="append", help="Repository-relative changed path; repeatable")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--discovery-dir", type=Path, default=DEFAULT_DISCOVERY_DIR)
    parser.add_argument("--journeys-root", type=Path, default=DEFAULT_JOURNEYS_ROOT)
    parser.add_argument("--output", type=Path, help="Write the JSON selection report to this file")
    args = parser.parse_args()
    files = changed_files(args.repo_root, args.base_ref) if args.base_ref else args.changed_file
    report = json.dumps(select_journeys(files, args.discovery_dir, args.journeys_root), indent=2)
    if args.output:
        args.output.write_text(report + "\n", encoding="utf-8")
    else:
        print(report)


if __name__ == "__main__":
    main()