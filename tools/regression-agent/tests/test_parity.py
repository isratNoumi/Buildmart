from __future__ import annotations

import functools
import json
import re
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from agents.discovery.contracts.models import ContractStatus, PageContract, Region, Tolerance
from agents.discovery.contracts.profile import SiteProfile
from agents.discovery.contracts.store import write_contract
from agents.discovery.models import ApplicationModel, ComponentModel, Evidence, FlowModel, FlowStep, PageModel
from agents.parity import cli, comparator, planner
from agents.parity.judge import Judge, add_label, parse_answer
from agents.parity.models import Cell, CellRun, Diff, ExpectedChange, Judgment, RegionObservation, SideRun
from agents.parity.reporter import render_html, write_defects


# --- 1. Planner --------------------------------------------------------------------------

def test_fill_route_uses_same_named_sample_keys():
    assert planner.fill_route("/items/:id", {"id": "42", "locale": "fr"}) == "/items/42"
    assert planner.fill_route("/items/:id", {"locale": "fr"}) is None
    assert planner.fill_route("/", {}) == "/"
    assert planner.fill_route("*", {}) == planner.NOT_FOUND_PATH
    assert planner.fill_route("* (not found)", {}) == planner.NOT_FOUND_PATH


def test_plan_keeps_pinned_rotates_the_rest_and_reports_uncovered():
    profile = SiteProfile(name="Example", pinned=[{}], samples=[{"locale": x} for x in "abcdef"])
    contracts = [PageContract(page_id="home", route="/"), PageContract(page_id="item", route="/items/:id")]

    cells, uncovered = planner.plan(contracts, profile, iteration=1, per_page=2)
    assert [c.sample for c in cells if c.pinned] == [{}]
    assert len(cells) == 3
    assert uncovered == ["item (/items/:id): no sample provides id"]
    assert planner.plan(contracts, profile, iteration=1)[0] == cells  # same iteration -> same samples
    rotated = {tuple(sorted(c.sample.items())) for i in range(10) for c in planner.plan(contracts, profile, iteration=i)[0]}
    assert len(rotated) > 3  # different iterations cover different samples


def test_select_pages_follows_changed_files(tmp_path: Path):
    from agents.planner.select import select_pages

    model = ApplicationModel(
        components=[ComponentModel(name="Home", file="src/Home.jsx"), ComponentModel(name="Item", file="src/Item.jsx")],
        pages=[PageModel(page_id="home", route="/", component="Home"), PageModel(page_id="item", route="/items/:id", component="Item")],
    )
    (tmp_path / "application.json").write_text(model.model_dump_json(), encoding="utf-8")
    assert [p["id"] for p in select_pages(["src/Item.jsx"], tmp_path)["selected"]] == ["item"]
    assert len(select_pages(["src/unknown.js"], tmp_path)["selected"]) == 2


# --- 3. Comparator -----------------------------------------------------------------------

CONTRACT = PageContract(page_id="item", route="/items/:id", component="ItemPage", regions=[
    Region(region_id="title", tolerance=Tolerance.STRUCTURE, locator="role=heading"),
    Region(region_id="price", tolerance=Tolerance.VOLATILE, locator="role=status", format_rule=r"^\$\d+\.\d{2}$"),
    Region(region_id="save", tolerance=Tolerance.BEHAVIOR, locator="role=button"),
    Region(region_id="help", tolerance=Tolerance.STRUCTURE, locator="role=link"),
])


def _obs(region_id, text="", y=0, settled=100.0, status="settled", aria=None, enabled=True):
    return RegionObservation(region_id=region_id, status=status, appeared_ms=50.0, settled_ms=settled, text=text,
                             aria=aria if aria is not None else f'- text "{text}"', enabled=enabled, box=(0, y, 100, 20))


def _run(current: list[RegionObservation], candidate: list[RegionObservation], **candidate_extra) -> CellRun:
    cell = Cell(page_id="item", route="/items/:id", path="/items/1", sample={"id": "1"})
    side = lambda build, regions, **extra: SideRun(build=build, url="u", final_path="/items/1", regions=regions, **extra)
    return CellRun(cell=cell, current=side("current", current), candidate=side("candidate", candidate, **candidate_extra),
                   current_first=True)


def _current():
    return [_obs("title", "Drill", 0), _obs("price", "$10.00", 30), _obs("save", "Save", 60), _obs("help", "Help", 90)]


def test_identical_builds_and_changed_prices_produce_no_diffs():
    candidate = _current()
    candidate[1] = _obs("price", "$12.50", 30)  # volatile value change: dropped before any AI
    diffs, notes = comparator.compare_cell(_run(_current(), candidate), CONTRACT, comparator.Thresholds())
    assert diffs == [] and notes == []

    strict = comparator.compare_cell(_run(_current(), candidate), CONTRACT, comparator.Thresholds(strict_values=True))[0]
    assert [d.kind for d in strict] == ["value"]


def test_comparator_finds_each_kind_of_difference():
    candidate = [
        _obs("title", "Drill", 0, aria='- heading "Drill" [level=2]'),  # structure changed
        _obs("price", "Call for price", 30),  # breaks format rule
        _obs("save", "Save", 90, enabled=False, settled=900.0),  # disabled, slower, moved below help
        RegionObservation(region_id="help", status="missing"),
    ]
    current = _current()
    current[0] = _obs("title", "Drill", 0, aria='- heading "Drill" [level=1]')
    diffs, _ = comparator.compare_cell(
        _run(current, candidate, console_errors=["boom"], network_failures=["GET /api -> HTTP 500"]),
        CONTRACT, comparator.Thresholds())
    kinds = {(d.kind, d.region_id) for d in diffs}
    assert {("structure", "title"), ("format", "price"), ("behavior", "save"), ("slow", "save"),
            ("missing", "help"), ("console", None), ("network", None)} <= kinds


def test_region_order_change_is_reported():
    candidate = _current()
    candidate[0], candidate[3] = _obs("title", "Drill", 90), _obs("help", "Help", 0)
    diffs, _ = comparator.compare_cell(_run(_current(), candidate), CONTRACT, comparator.Thresholds())
    assert [d.kind for d in diffs] == ["order"]


def test_unlocatable_regions_become_notes_not_diffs():
    current = _current()[:3] + [RegionObservation(region_id="help", status="unlocatable", detail="bad selector")]
    diffs, notes = comparator.compare_cell(_run(current, current), CONTRACT, comparator.Thresholds())
    assert diffs == [] and "help" in notes[0]


# --- 4. Judge ----------------------------------------------------------------------------

def _diff(cell="item [id=1]", candidate="Call for price") -> Diff:
    return Diff(kind="format", page_id="item", cell_label=cell, region_id="price", message="m",
                current="$10.00", candidate=candidate)


def test_judge_order_of_authority_and_cache(tmp_path: Path):
    calls = []

    def ask(prompt: str) -> str:
        calls.append(prompt)
        return 'Sure: {"classification": "regression", "confidence": 0.9, "reason": "price hidden"}'

    judge = Judge(tmp_path, "Example", ask=ask)
    first = judge.judge(_diff())
    assert (first.classification, first.source) == ("regression", "model")
    again = judge.judge(_diff(cell="item [id=2]"))  # same difference on another sample
    assert again.source == "cache" and len(calls) == 1
    assert Judge(tmp_path, "Example", ask=ask).judge(_diff()).source == "cache"  # persisted

    add_label(tmp_path, _diff().fingerprint, "cosmetic", "fine")
    assert Judge(tmp_path, "Example", ask=ask).judge(_diff()).source == "human"

    expected = [ExpectedChange(region_id="price", reason="Price block redesigned")]
    assert Judge(tmp_path / "x", "Example", expected, ask=ask).judge(_diff()).classification == "expected"


def test_judge_escalates_low_confidence_bad_answers_and_no_llm(tmp_path: Path):
    low = Judge(tmp_path / "a", "E", ask=lambda p: '{"classification": "cosmetic", "confidence": 0.3, "reason": "r"}').judge(_diff())
    assert low.classification == "needs_review" and "cosmetic" in low.reason
    assert Judge(tmp_path / "b", "E", ask=lambda p: "no idea").judge(_diff()).classification == "needs_review"
    assert Judge(tmp_path / "c", "E", ask=None).judge(_diff()).source == "no_llm"

    def broken(prompt):
        raise ConnectionError("offline")
    assert Judge(tmp_path / "d", "E", ask=broken).judge(_diff()).classification == "needs_review"
    assert parse_answer('{"classification": "maybe"}') is None


# --- 5. Reporter -------------------------------------------------------------------------

def test_reporter_escapes_text_and_writes_one_defect_per_fingerprint(tmp_path: Path):
    run = _run(_current(), _current())
    evil = _diff(cell=run.cell.label, candidate="<script>alert(1)</script>")
    judged = [(evil, Judgment(classification="regression", confidence=0.9, reason="r", source="model")),
              (evil.model_copy(update={"cell_label": "item [id=2]"}), Judgment(classification="regression", source="cache"))]
    profile = SiteProfile(name="Example")
    paths = write_defects(tmp_path, judged, [run], profile, {"current": "http://a", "candidate": "http://b"},
                          {"current": "v1", "candidate": "v2"}, {"ItemPage": "src/Item.jsx"})
    assert len(paths) == 1
    defect = json.loads(paths[0].read_text(encoding="utf-8"))
    assert defect["affected_samples"] == ["item [id=1]", "item [id=2]"]
    assert defect["steps_to_reproduce"][-2] == "Open http://b/items/1"

    report = {"site": "<b>x</b>", "versions": {"current": "v1", "candidate": "v2"}, "urls": {"current": "a", "candidate": "b"},
              "iteration": 0, "generated_at": "now", "uncovered": [], "notes": [],
              "summary": {"cells": 1, "diffs": 1, "regression": 1}}
    html = render_html(report, [run], judged, tmp_path)
    assert "<script>alert(1)</script>" not in html and "&lt;b&gt;x&lt;/b&gt;" in html


# --- 2 + all agents: real browser against two local builds ---------------------------------

CURRENT_HTML = """<!doctype html><html><body>
<h1>Welcome</h1><p role="status" aria-label="price">$10.00</p>
<button>Save</button><a href="/help">Help</a><label>Name <input></label>
<script>setTimeout(() => document.body.insertAdjacentHTML('beforeend',
  '<section aria-label="Recommendations"><p>For you</p></section>'), 50)</script>
</body></html>"""
CANDIDATE_HTML = """<!doctype html><html><body>
<h1>Welcome</h1><p role="status" aria-label="price">$12.00</p>
<button disabled>Save</button><label>Name <input></label>
<script>setTimeout(() => document.body.insertAdjacentHTML('beforeend',
  '<section aria-label="Recommendations"><p>For you</p></section>'), 900)</script>
</body></html>"""


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture
def two_builds(tmp_path: Path):
    servers = []
    for name, html in (("current", CURRENT_HTML), ("candidate", CANDIDATE_HTML)):
        root = tmp_path / "sites" / name
        root.mkdir(parents=True)
        (root / "index.html").write_text(html, encoding="utf-8")
        server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_QuietHandler, directory=str(root)))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
    yield [f"http://127.0.0.1:{s.server_address[1]}" for s in servers]
    for server in servers:
        server.shutdown()


def test_all_five_agents_end_to_end_in_a_browser(tmp_path: Path, two_builds):
    playwright = pytest.importorskip("playwright.sync_api")
    try:
        with playwright.sync_playwright() as pw:
            pw.chromium.launch().close()
    except Exception as error:
        pytest.skip(f"Chromium not available: {error}")

    discovery = tmp_path / "discovery"
    discovery.mkdir()
    model = ApplicationModel(components=[ComponentModel(name="HomePage", file="src/Home.jsx")],
                             pages=[PageModel(page_id="home", route="/", component="HomePage")],
                             flows=[FlowModel(flow_id="f", name="f", steps=[FlowStep(page="home", action="x")])])
    (discovery / "application.json").write_text(model.model_dump_json(), encoding="utf-8")
    write_contract(discovery / "contracts", PageContract(
        page_id="home", route="/", component="HomePage", status=ContractStatus.APPROVED,
        evidence=[Evidence(type="test")], regions=[
            Region(region_id="heading", tolerance=Tolerance.STRUCTURE, locator='role=heading[name="Welcome"]', settle_timeout_ms=2000),
            Region(region_id="price", tolerance=Tolerance.VOLATILE, locator='role=status[name="price"]',
                   format_rule=r"^\$\d+\.\d{2}$", settle_timeout_ms=2000),
            Region(region_id="save", tolerance=Tolerance.BEHAVIOR, locator='role=button[name="Save"]', settle_timeout_ms=2000),
            Region(region_id="help", tolerance=Tolerance.STRUCTURE, locator='role=link[name="Help"]', settle_timeout_ms=1500),
            Region(region_id="recommendations", tolerance=Tolerance.STRUCTURE,
                   locator='role=region[name="Recommendations"]', settle_timeout_ms=3000),
        ]))
    profile = tmp_path / "site.yaml"
    profile.write_text("name: Example\npinned:\n  - {name: Ada}\n"
                       "setup:\n  name:\n    - {action: goto, target: /}\n    - {action: fill, target: Name, value: '{name}'}\n",
                       encoding="utf-8")
    out = tmp_path / "run"

    code = cli.main(["run", "--site-profile", str(profile), "--current-url", two_builds[0], "--candidate-url", two_builds[1],
                     "--all", "--discovery-dir", str(discovery), "--no-llm", "--skip-journeys",
                     "--cache-dir", str(tmp_path / "cache"), "--output-dir", str(out)])

    assert code == 0  # nothing is called a regression without a judge or a human
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    kinds = {(d["kind"], d["region_id"]) for d in report["diffs"]}
    assert ("missing", "help") in kinds
    assert ("behavior", "save") in kinds
    assert ("slow", "recommendations") in kinds
    assert not any(region == "price" for _, region in kinds)  # $10.00 vs $12.00 is allowed to differ
    assert all(d["judgment"]["classification"] == "needs_review" for d in report["diffs"])
    assert (out / "report.html").exists() and list((out / "defects").glob("*.json"))
    assert (out / "current" / "000-home.png").exists() and (out / "candidate" / "000-home.png").exists()


# --- Protected routes ----------------------------------------------------------------------

def test_page_missing_on_new_build_is_reported_once():
    candidate = [_obs("title", "Drill", 0)] + [RegionObservation(region_id=r, status="missing") for r in ("price", "save", "help")]
    diffs, _ = comparator.compare_cell(_run(_current(), candidate), CONTRACT, comparator.Thresholds())
    assert [d.kind for d in diffs] == ["page_missing"]


def test_summary_separates_setup_problems_missing_pages_and_real_differences():
    from agents.parity.summary import friendly_region, summarize

    assert friendly_region('role=button[name="Add to cart"]') == '"Add to cart" button'
    assert friendly_region("role=button[name=/^Remove /]") == '"Remove..." button'
    judgment = {"classification": "needs_review", "reason": ""}
    report = {
        "urls": {"current": "https://site.example", "candidate": "http://localhost:5174"},
        "selection": {"mode": "source", "changed_files": ["web/src/Item.jsx"],
                      "selected": [{"id": "item", "reasons": ["web/src/Item.jsx changed"]}],
                      "skipped": [{"id": "home", "reasons": ["Not affected by these changes"]}], "ignored": []},
        "not_on_new_build": [{"page": "checkout", "reason": "Page content not found on the new build (not built yet)", "checks": 1}],
        "compared_pages": ["account", "item"],
        "runs": [],
        "diffs": [
            {"kind": "page_error", "page_id": "account", "cell_label": "account [a]", "candidate": "LookupError: signing in failed: no input",
             "message": "m", "fingerprint": "f1", "judgment": judgment},
            {"kind": "missing", "page_id": "item", "cell_label": "item [a]", "region_id": 'role=button[name="Save"]',
             "current": "Save", "candidate": "missing", "message": "m", "fingerprint": "f3", "judgment": judgment},
            {"kind": "missing", "page_id": "item", "cell_label": "item [b]", "region_id": 'role=button[name="Save"]',
             "current": "Save", "candidate": "missing", "message": "m", "fingerprint": "f3", "judgment": judgment},
        ],
    }
    s = summarize(report)
    assert "Couldn't sign in" in s["setup_problems"][0]["text"]
    assert s["not_on_new_build"][0]["page"] == "checkout"
    assert s["selection"]["why"] == [{"page": "item", "reasons": ["web/src/Item.jsx changed"]}]
    assert s["selection"]["not_affected"] == [{"page": "home", "reason": "Not affected by these changes"}]
    assert len(s["problems"]) == 1 and s["problems"][0]["text"] == '"Save" button is missing on the new build'
    assert s["problems"][0]["samples"] == ["item [a]", "item [b]"]
    assert any("network distance" in c for c in s["cautions"])


def test_markdown_summary_escapes_page_text():
    from agents.parity.summary import summarize, to_markdown

    judgment = {"classification": "needs_review", "reason": ""}
    report = {"compared_pages": ["item"], "runs": [], "diffs": [
        {"kind": "value", "page_id": "item", "cell_label": "item [a]", "region_id": "price", "current": "<img src=x onerror=alert(1)>",
         "candidate": "| pipe", "message": "m", "fingerprint": "f", "judgment": judgment}]}
    markdown = to_markdown(summarize(report))
    assert "Differences" in markdown and "All pages were checked" in markdown
    assert not re.search(r"(?<!\\)<img", markdown) and "\\| pipe" in markdown


def test_nothing_compared_has_its_own_verdict():
    from agents.parity.summary import summarize

    report = {"compared_pages": [], "runs": [], "diffs": [], "journeys": [],
              "not_on_new_build": [{"page": "cart", "reason": "r", "checks": 2}]}
    assert summarize(report)["verdict"] == "nothing"


# --- Pages not on the new build ----------------------------------------------------------

def test_pages_missing_everywhere_or_without_sign_in_are_not_on_the_new_build():
    from agents.parity.availability import CONTENT_MISSING, SIGN_IN_FAILED, unavailable_pages

    gone = [_obs("title", "Drill", 0)] + [RegionObservation(region_id=r, status="missing") for r in ("price", "save", "help")]
    missing_run = _run(_current(), gone)
    diffs, _ = comparator.compare_cell(missing_run, CONTRACT, comparator.Thresholds())
    assert unavailable_pages([missing_run], diffs) == {"item": {"page": "item", "reason": CONTENT_MISSING, "checks": 1}}

    fine_run = _run(_current(), _current())
    fine_run.cell = fine_run.cell.model_copy(update={"sample": {"id": "2"}})
    assert unavailable_pages([missing_run, fine_run], diffs) == {}  # missing on one sample only: a real difference

    login_run = _run(_current(), [], error="LookupError: signing in failed: no input matching 'Email'")
    login_run.cell = login_run.cell.model_copy(update={"page_id": "account", "requires_login": True})
    assert unavailable_pages([login_run], [])["account"]["reason"] == SIGN_IN_FAILED


# --- Changed files -> pages (new build's source) ----------------------------------------

def _write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")


ROUTER_APP = {
    "web/index.html": '<script type="module" src="/src/main.jsx"></script>',
    "web/package.json": "{}",
    "web/src/main.jsx": 'import App from "./App";\nimport "./styles/global.css";',
    "web/src/styles/global.css": "body {}",
    "web/src/App.jsx": ('import { Routes, Route } from "react-router-dom";\nimport Header from "./components/Header";\n'
                        'import HomePage from "./pages/HomePage";\nimport { CartPage } from "./pages/CartPage";\n'
                        'function Soon() { return null; }\n'
                        'export default () => <Routes><Route path="/" element={<HomePage />} />\n'
                        '<Route path="/cart" element={<CartPage />} />\n<Route path="/items/:itemId" element={<Soon />} /></Routes>;'),
    "web/src/components/Header.jsx": "export default () => null;",
    "web/src/components/Card.jsx": 'import { money } from "../utils/format";',
    "web/src/utils/format.js": "export const money = 1;",
    "web/src/pages/HomePage.jsx": 'import Card from "../components/Card";',
    "web/src/pages/CartPage.jsx": 'import { money } from "../utils/format";\nexport function CartPage() {}',
    "web/src/pages/Draft.jsx": "export default () => null;",
    "web/src/pages/HomePage.test.jsx": 'import HomePage from "./HomePage";',
}


def test_source_map_selects_pages_from_routes_and_imports(tmp_path: Path):
    from agents.planner.source_map import select_pages_from_source

    _write(tmp_path, ROUTER_APP)
    model = ApplicationModel(pages=[PageModel(page_id=p, route=r) for p, r in
                                    (("home", "/"), ("cart", "/cart"), ("item", "/items/:id"), ("account", "/account"))])

    def pick(*files):
        result = select_pages_from_source(list(files), tmp_path / "web", tmp_path, model)
        return {s["id"]: s["reasons"] for s in result["selected"]}, result

    assert pick("web/src/pages/HomePage.jsx")[0] == {"home": ["web/src/pages/HomePage.jsx changed"]}
    assert set(pick("web/src/utils/format.js")[0]) == {"home", "cart"}  # shared helper
    assert pick("web/src/components/Card.jsx")[0]["home"] == ["web/src/components/Card.jsx changed (used by this page)"]
    assert set(pick("web/src/components/Header.jsx")[0]) == {"home", "cart", "item", "account"}  # app shell
    assert set(pick("web/src/styles/global.css")[0]) == {"home", "cart", "item", "account"}
    assert set(pick("web/package.json")[0]) == {"home", "cart", "item", "account"}  # build setting
    selected, result = pick("web/src/pages/Draft.jsx", "web/src/pages/HomePage.test.jsx", "backend/main.py")
    assert selected == {} and len(result["ignored"]) == 3
    skipped = {s["id"]: s["reasons"][0] for s in result["skipped"]}
    assert skipped["account"] == "The new build has no route for this page yet"
    assert skipped["home"] == "Not affected by these changes"


def test_pages_without_a_real_route_are_not_built(tmp_path: Path):
    from agents.planner.source_map import pages_not_built

    _write(tmp_path, ROUTER_APP)
    model = ApplicationModel(pages=[PageModel(page_id=p, route=r) for p, r in
                                    (("home", "/"), ("cart", "/cart"), ("item", "/items/:id"), ("account", "/account"))])
    not_built = pages_not_built(tmp_path / "web", model)
    assert set(not_built) == {"item", "account"}
    assert "placeholder" in not_built["item"]["reason"] and "App.jsx" in not_built["item"]["reason"]
    assert pages_not_built(tmp_path / "web" / "src" / "pages", model) == {}  # no router found: can't tell


def test_source_map_reads_react_router_5_routes(tmp_path: Path):
    from agents.planner.source_map import SourceMap

    _write(tmp_path, {"src/index.jsx": 'import App from "./App";',
                      "src/App.jsx": ('import ProductPage from "./pages/ProductPage";\n'
                                      '<Switch><Route exact path="/products/:id" component={ProductPage} /></Switch>'),
                      "src/pages/ProductPage.jsx": "export default () => null;"})
    source = SourceMap(tmp_path)
    assert source.page_files("/products/:productId") == {(tmp_path / "src/pages/ProductPage.jsx").resolve()}
    assert source.page_files("/cart") is None


def test_flows_within_chosen_pages_only():
    from agents.parity.cli import _flows_within

    model = ApplicationModel(
        pages=[PageModel(page_id="home", route="/"), PageModel(page_id="login", route="/login"), PageModel(page_id="cart", route="/cart")],
        flows=[FlowModel(flow_id="browse", name="b", steps=[FlowStep(page="/", action="x"), FlowStep(page="cart", action="y")]),
               FlowModel(flow_id="sign_in", name="s", steps=[FlowStep(page="/login", action="x")])])
    assert _flows_within(model, {"home", "cart"}) == {"browse"}
    assert _flows_within(model, {"home", "cart", "login"}) == {"browse", "sign_in"}


def test_redirect_on_both_builds_is_a_note_not_a_silent_pass():
    gone = [RegionObservation(region_id=r, status="missing") for r in ("title", "price", "save", "help")]
    run = _run(gone, gone)
    run.current.final_path = run.candidate.final_path = "/login"
    diffs, notes = comparator.compare_cell(run, CONTRACT, comparator.Thresholds())
    assert diffs == [] and "ended on /login" in notes[0]


def test_repro_steps_mention_sign_in_but_never_contain_secrets():
    from agents.discovery.contracts.profile import SetupStep
    from agents.parity.reporter import repro_steps

    profile = SiteProfile(name="E", setup={"pin": [SetupStep(action="fill", target="PIN", value="{env:SECRET_PIN}")]})
    run = _run(_current(), _current())
    run.cell = run.cell.model_copy(update={"requires_login": True, "sample": {"pin": "x"}})
    steps = repro_steps(run, profile, "http://b", "http://a")
    assert steps[0].startswith("Sign in") and 'fill PIN "{env:SECRET_PIN}"' in steps


LOGIN_HTML = """<!doctype html><html><body>
<label>Email <input></label><label>Password <input type="password" id="p"></label>
<button onclick="if (document.getElementById('p').value === 's3cret') {
  localStorage.setItem('session', 'ok'); document.body.insertAdjacentHTML('beforeend', '<p>Welcome back</p>') }">Sign in</button>
</body></html>"""
ACCOUNT_HTML = """<!doctype html><html><head>
<script>if (localStorage.getItem('session') !== 'ok') location.replace('/login.html')</script>
</head><body><h1>Your account</h1></body></html>"""


@pytest.fixture
def protected_site(tmp_path: Path):
    root = tmp_path / "protected"
    root.mkdir()
    (root / "login.html").write_text(LOGIN_HTML, encoding="utf-8")
    (root / "account.html").write_text(ACCOUNT_HTML, encoding="utf-8")
    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_QuietHandler, directory=str(root)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_protected_page_is_checked_signed_in_and_flagged_otherwise(tmp_path: Path, protected_site, monkeypatch):
    playwright = pytest.importorskip("playwright.sync_api")
    try:
        with playwright.sync_playwright() as pw:
            pw.chromium.launch().close()
    except Exception as error:
        pytest.skip(f"Chromium not available: {error}")
    monkeypatch.setenv("PARITY_TEST_PASSWORD", "s3cret")

    discovery = tmp_path / "discovery"
    discovery.mkdir()
    (discovery / "application.json").write_text(ApplicationModel().model_dump_json(), encoding="utf-8")
    login_yaml = ("login:\n  steps:\n    - {action: goto, target: /login.html}\n"
                  "    - {action: fill, target: Email, value: tester@example.com}\n"
                  "    - {action: fill, target: Password, value: '{env:PARITY_TEST_PASSWORD}'}\n"
                  "    - {action: click, target: Sign in}\n  success_text: Welcome back\n")

    def run(requires_login: bool, with_login: bool) -> dict:
        write_contract(discovery / "contracts", PageContract(
            page_id="account", route="/account.html", status=ContractStatus.APPROVED, requires_login=requires_login,
            regions=[Region(region_id="heading", tolerance=Tolerance.STRUCTURE,
                            locator='role=heading[name="Your account"]', settle_timeout_ms=1500)]))
        profile = tmp_path / "site.yaml"
        profile.write_text("name: Example\npinned: [{}]\n" + (login_yaml if with_login else ""), encoding="utf-8")
        out = tmp_path / f"run-{requires_login}-{with_login}"
        cli.main(["run", "--site-profile", str(profile), "--current-url", protected_site, "--candidate-url", protected_site,
                  "--all", "--discovery-dir", str(discovery), "--no-llm", "--skip-journeys",
                  "--cache-dir", str(tmp_path / "cache"), "--output-dir", str(out)])
        return json.loads((out / "report.json").read_text(encoding="utf-8"))

    signed_in = run(requires_login=True, with_login=True)
    assert signed_in["notes"] == [] and signed_in["diffs"] == []
    assert signed_in["runs"][0]["candidate"]["regions"][0]["status"] == "settled"
    assert "s3cret" not in json.dumps(signed_in)

    assert "no login section" in run(requires_login=True, with_login=False)["notes"][0]
    assert "ended on /login.html" in run(requires_login=False, with_login=True)["notes"][0]
