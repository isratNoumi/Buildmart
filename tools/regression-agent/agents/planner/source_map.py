"""Which pages can a code change affect? Read from the new build's source, no AI.

    1. Routes:  <Route path="/cart" element={<CartPage/>}> (React Router 6) or component={CartPage} (5)
                matched to discovery pages by route, e.g. "/products/:id".
    2. Imports: everything a page's component imports (directly or not) belongs to that page.
    3. Shell:   files the app loads outside any page (entry, router, header, global CSS) affect every page.
    4. Build:   package.json, vite config, index.html and public/ affect every page.
    5. Others:  source files nothing imports (tests, unused drafts) can't affect the built app.
"""

from __future__ import annotations

import posixpath
import re
from pathlib import Path
from typing import Any

from agents.discovery.models import ApplicationModel

CODE_EXT = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")
STYLE_EXT = (".css", ".scss", ".sass", ".less")
SKIP_DIRS = {"node_modules", "dist", "build", ".git", "coverage"}

_IMPORT = re.compile(r"""(?:import|export)\s[^'"`;]*?\bfrom\s*['"]([^'"]+)['"]"""
                     r"""|\bimport\s*['"]([^'"]+)['"]"""
                     r"""|\bimport\(\s*['"]([^'"]+)['"]\s*\)"""
                     r"""|\brequire\(\s*['"]([^'"]+)['"]\s*\)""")
_CSS_IMPORT = re.compile(r"""@import\s+(?:url\()?\s*['"]([^'"]+)['"]""")
_DEFAULT_IMPORT = re.compile(r"""\bimport\s+([A-Za-z_$][\w$]*)\s*(?:,\s*\{[^}]*\})?\s*from\s*['"]([^'"]+)['"]""")
_NAMED_IMPORT = re.compile(r"""\bimport\s*(?:[A-Za-z_$][\w$]*\s*,\s*)?\{([^}]*)\}\s*from\s*['"]([^'"]+)['"]""")
_LAZY_IMPORT = re.compile(r"""\b([A-Za-z_$][\w$]*)\s*=\s*(?:React\.)?lazy\(\s*\(\)\s*=>\s*import\(\s*['"]([^'"]+)['"]""")
_ROUTE_TAG = re.compile(r"<Route\b")
_ROUTE_PATH = re.compile(r"""\bpath\s*=\s*\{?\s*['"`]([^'"`]+)['"`]""")
_ROUTE_COMPONENT = re.compile(r"""\belement\s*=\s*\{\s*<\s*([A-Z][\w$]*)|\bcomponent\s*=\s*\{\s*([A-Z][\w$]*)""")
_CHILD_COMPONENT = re.compile(r"""<\s*([A-Z][\w$]*)""")
_OBJECT_ROUTE = re.compile(r"""\bpath\s*:\s*['"`]([^'"`]+)['"`][^{}]*?\b(?:element\s*:\s*<\s*|Component\s*:\s*)([A-Z][\w$]*)""", re.S)
_SCRIPT_SRC = re.compile(r"""<script\b[^>]*\bsrc\s*=\s*['"]([^'"]+)['"]""")
_PARAM = re.compile(r":\w+")


def normalize_route(route: str) -> str:
    """'/products/:id/' -> '/products/:'; catch-alls ('*', '* (not found)') -> '*'."""
    route = route.strip()
    if not route.startswith("/"):
        return "*"
    return _PARAM.sub(":", route.rstrip("/") or "/")


class SourceMap:
    def __init__(self, source_dir: Path):
        self.root = source_dir.resolve()
        self.files = [p for p in self.root.rglob("*")
                      if p.is_file() and p.suffix in CODE_EXT + STYLE_EXT
                      and not SKIP_DIRS & set(p.relative_to(self.root).parts[:-1])]
        self._texts: dict[Path, str] = {}
        self.imports = {f: self._imports_of(f) for f in self.files}
        self.entry = self._entry()
        self.routes = self._routes()  # normalized route -> (component file, router file)
        page_files = {file for file, router in self.routes.values() if file != router}
        self.shell = self._closure([self.entry], stop=page_files) if self.entry else set()

    def text(self, file: Path) -> str:
        if file not in self._texts:
            self._texts[file] = file.read_text(encoding="utf-8", errors="replace")
        return self._texts[file]

    def resolve(self, spec: str, importer: Path) -> Path | None:
        if spec.startswith("."):
            base = importer.parent / spec
        elif spec.startswith("/"):
            base = self.root / spec.lstrip("/")
        else:
            return None  # package import
        for candidate in (base, *(base.with_name(base.name + ext) for ext in CODE_EXT + STYLE_EXT),
                          *(base / f"index{ext}" for ext in CODE_EXT)):
            if candidate.is_file():
                return candidate.resolve()
        return None

    def _imports_of(self, file: Path) -> set[Path]:
        pattern = _CSS_IMPORT if file.suffix in STYLE_EXT else _IMPORT
        specs = (next(group for group in match.groups() if group) for match in pattern.finditer(self.text(file)))
        return {target for spec in specs if (target := self.resolve(spec, file))}

    def _entry(self) -> Path | None:
        html = self.root / "index.html"
        if html.is_file():
            for src in _SCRIPT_SRC.findall(html.read_text(encoding="utf-8", errors="replace")):
                if target := self.resolve(src if src.startswith((".", "/")) else "./" + src, html):
                    return target
        for name in ("src/main", "src/index", "main", "index"):
            if target := self.resolve("./" + name, self.root / "_"):
                return target
        return None

    def _component_file(self, name: str, file: Path) -> Path:
        text = self.text(file)
        for regex in (_DEFAULT_IMPORT, _LAZY_IMPORT):
            for local, spec in regex.findall(text):
                if local == name:
                    return self.resolve(spec, file) or file
        for names, spec in _NAMED_IMPORT.findall(text):
            locals_ = [part.split(" as ")[-1].strip() for part in names.split(",")]
            if name in locals_:
                return self.resolve(spec, file) or file
        return file  # defined in the router file itself (e.g. a placeholder)

    def _routes(self) -> dict[str, tuple[Path, Path]]:
        routes: dict[str, tuple[Path, Path]] = {}
        for file in self.files:
            if file.suffix not in CODE_EXT:
                continue
            text = self.text(file)
            starts = [m.start() for m in _ROUTE_TAG.finditer(text)] + [len(text)]
            for start, end in zip(starts, starts[1:]):
                tag = text[start:min(end, start + 400)]
                path, component = _ROUTE_PATH.search(tag), _ROUTE_COMPONENT.search(tag)
                name = (component.group(1) or component.group(2)) if component else None
                if not name:
                    child = _CHILD_COMPONENT.search(tag, tag.find(">") + 1)
                    name = child.group(1) if child and child.group(1) != "Route" else None
                if path and name:
                    routes.setdefault(normalize_route(path.group(1)), (self._component_file(name, file), file))
            for path, name in _OBJECT_ROUTE.findall(text):
                routes.setdefault(normalize_route(path), (self._component_file(name, file), file))
        return routes

    def _closure(self, starts: list[Path], stop: set[Path] = frozenset()) -> set[Path]:
        seen: set[Path] = set()
        pending = list(starts)
        while pending:
            file = pending.pop()
            if file in seen:
                continue
            seen.add(file)
            pending.extend(t for t in self.imports.get(file, ()) if t not in seen and t not in stop)
        return seen

    def page_files(self, route: str) -> set[Path] | None:
        """Files behind one page's route, or None if the new build has no such route."""
        found = self.routes.get(normalize_route(route))
        if found is None:
            return None
        component, router = found
        return {router} if component == router else self._closure([component])


def pages_not_built(source_dir: Path, model: ApplicationModel) -> dict[str, dict]:
    """page_id -> {"page", "reason", "checks"} for pages the new build's router doesn't really have yet:
    no route, or a component defined inside the router file itself (a placeholder)."""
    source = SourceMap(source_dir)
    if not source.routes:
        return {}  # no routes understood: can't tell, so let the browser check decide
    pages = {}
    for page in model.pages:
        found = source.routes.get(normalize_route(page.route))
        if found is None:
            reason = "The new build has no route for this page yet"
        elif found[0] == found[1]:
            reason = f"Only a placeholder in the new build's router ({found[1].name}), not built yet"
        else:
            continue
        pages[page.page_id] = {"page": page.page_id, "reason": reason, "checks": 0}
    return pages


def select_pages_from_source(files: list[str], source_dir: Path, repo_root: Path,
                             model: ApplicationModel) -> dict[str, Any]:
    """Like planner.select.select_pages, but read from the new build's code instead of discovery evidence."""
    source = SourceMap(source_dir)
    prefix = source.root.relative_to(repo_root.resolve()).as_posix()
    changed = sorted({posixpath.normpath(f.replace("\\", "/")) for f in files})
    pages = {p.page_id: source.page_files(p.route) for p in model.pages}
    own_file = {p.page_id: source.routes.get(normalize_route(p.route), (None,))[0] for p in model.pages}

    everywhere: list[str] = []
    per_page: dict[str, list[str]] = {page_id: [] for page_id in pages}
    ignored: list[dict[str, str]] = []
    for name in changed:
        if prefix != "." and not (name == prefix or name.startswith(prefix + "/")):
            ignored.append({"file": name, "reason": f"Outside the new build's frontend ({prefix}/)"})
            continue
        path = (repo_root / name).resolve()
        inner = path.relative_to(source.root).as_posix()
        users = [page_id for page_id, deps in pages.items() if deps and path in deps]
        if path in source.shell:
            everywhere.append(f"{name} is loaded on every page (app shell)")
        elif users:
            for page_id in users:
                per_page[page_id].append(f"{name} changed" + ("" if path == own_file[page_id] else " (used by this page)"))
        elif path.suffix not in CODE_EXT + STYLE_EXT or "/" not in inner or inner.startswith("public/"):
            everywhere.append(f"{name} is a build setting or static file used by every page")
        elif not path.exists():
            everywhere.append(f"{name} was deleted; the pages that used it can't be told apart")
        else:
            ignored.append({"file": name, "reason": "Not imported by the app, so it can't change any page"})

    selected, skipped = [], []
    for page_id, deps in pages.items():
        reasons = everywhere + per_page[page_id]
        if reasons:
            selected.append({"id": page_id, "reasons": reasons})
        elif deps is None:
            skipped.append({"id": page_id, "reasons": ["The new build has no route for this page yet"]})
        else:
            skipped.append({"id": page_id, "reasons": ["Not affected by these changes"]})
    return {"mode": "source", "source_dir": prefix, "changed_files": changed,
            "selected": selected, "skipped": skipped, "ignored": ignored}
