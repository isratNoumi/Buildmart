from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agents.discovery.contracts import ContractStatus, PageContract, Region, Tolerance, cli
from agents.discovery.contracts.profile import load_profile
from agents.discovery.contracts.skeleton import build_skeletons
from agents.discovery.contracts.store import approve, draft_path, load_contract, save_draft, write_contract
from agents.discovery.models import ApplicationModel, DynamicElementModel, ElementModel, FlowModel, FlowStep, PageModel


def test_new_contract_is_draft_and_reports_gaps():
    contract = PageContract(
        page_id="article_detail",
        route="/articles/:id",
        regions=[
            Region(region_id="view-count", tolerance=Tolerance.VOLATILE),
            Region(region_id="bookmark-button", tolerance=Tolerance.BEHAVIOR, locator='role=button[name="Bookmark"]', order=3),
        ],
    )
    assert contract.status == "draft"
    assert contract.version == 1
    assert contract.gaps() == [
        "view-count: locator",
        "view-count: format_rule",
    ]


def test_contract_round_trips_through_json():
    contract = PageContract(page_id="settings", route="/settings", regions=[Region(region_id="Header", tolerance=Tolerance.STRUCTURE)])
    assert PageContract.model_validate_json(contract.model_dump_json()) == contract


def _model() -> ApplicationModel:
    return ApplicationModel(
        pages=[PageModel(
            page_id="article_detail", route="/articles/:id", component="ArticlePage",
            components=["Header"], apis=["POST /api/bookmarks"],
            elements=["bookmark-button", "mystery-button"], dynamic_elements=["view-count"],
        )],
        elements=[
            ElementModel(element_type="button", text="Bookmark", selector='role=button[name="Bookmark"]',
                         component="ArticlePage", action="Saves the article to the reading list"),
        ],
        dynamic_elements=[
            DynamicElementModel(element="View count under the article title", selector='role=status[name^="Views"]',
                                reason="Changes as readers open the article"),
        ],
        flows=[
            FlowModel(flow_id="bookmark_article", name="Bookmark", steps=[FlowStep(page="/articles/:id", action="Click Bookmark")]),
            FlowModel(flow_id="login", name="Login", steps=[FlowStep(page="login", action="Sign in")]),
        ],
    )


def test_skeleton_maps_discovery_into_three_groups():
    [contract] = build_skeletons(_model())
    by_id = {r.region_id: r for r in contract.regions}

    assert [r.region_id for r in contract.regions if r.tolerance is Tolerance.STRUCTURE] == ["ArticlePage", "Header"]
    assert by_id["view-count"].tolerance is Tolerance.VOLATILE
    assert by_id["view-count"].locator == 'role=status[name^="Views"]'
    assert by_id["bookmark-button"].tolerance is Tolerance.BEHAVIOR
    assert by_id["bookmark-button"].locator == 'role=button[name="Bookmark"]'
    assert by_id["mystery-button"].locator is None  # unmatched: left as a gap for the agent
    assert contract.flows == ["bookmark_article"]
    assert contract.apis == ["POST /api/bookmarks"]


def test_skeleton_command_never_overwrites_existing_contracts(tmp_path: Path):
    (tmp_path / "application.json").write_text(_model().model_dump_json(), encoding="utf-8")
    assert cli.main(["--discovery-dir", str(tmp_path), "skeleton"]) == 0
    path = tmp_path / "contracts" / "article_detail.json"
    edited = PageContract.model_validate_json(path.read_text(encoding="utf-8"))
    edited.status = ContractStatus.APPROVED
    path.write_text(edited.model_dump_json(), encoding="utf-8")

    cli.main(["--discovery-dir", str(tmp_path), "skeleton"])
    assert PageContract.model_validate_json(path.read_text(encoding="utf-8")).status is ContractStatus.APPROVED


def test_skeleton_uses_selector_ids_and_landmarks():
    model = ApplicationModel(pages=[PageModel(
        page_id="cart", route="/cart", component="CartPage", components=["SiteHeader", "CartPage", "PageFooter"],
        elements=['role=button[name="Proceed to checkout"]'], dynamic_elements=["Cart subtotal"],
    )])
    [contract] = build_skeletons(model)
    by_id = {r.region_id: r for r in contract.regions}
    assert by_id['role=button[name="Proceed to checkout"]'].locator == 'role=button[name="Proceed to checkout"]'
    assert by_id["CartPage"].locator == "role=main"
    assert by_id["SiteHeader"].locator == "role=banner"
    assert by_id["PageFooter"].locator == "role=contentinfo"
    assert by_id["Cart subtotal"].locator is None  # description, not a selector: left for the agent


def test_skeleton_refreshes_untouched_drafts_but_keeps_agent_work(tmp_path: Path):
    (tmp_path / "application.json").write_text(_model().model_dump_json(), encoding="utf-8")
    contracts_dir = tmp_path / "contracts"
    write_contract(contracts_dir, PageContract(page_id="article_detail", route="/old"))
    cli.main(["--discovery-dir", str(tmp_path), "skeleton"])
    assert load_contract(contracts_dir, "article_detail").route == "/articles/:id"  # stale draft refreshed

    write_contract(contracts_dir, PageContract(page_id="article_detail", route="/agent", samples_seen=["id=1"]))
    cli.main(["--discovery-dir", str(tmp_path), "skeleton"])
    assert load_contract(contracts_dir, "article_detail").route == "/agent"  # agent-filled draft kept


# --- Step 3: save_contract tool -------------------------------------------------

def _save_contract(output_dir: Path, args: dict) -> dict:
    from agents.discovery.tools.save_tools import _make_save_tool

    return asyncio.run(_make_save_tool("contract", PageContract, output_dir).handler(args))


def test_save_contract_requires_evidence_and_forces_draft(tmp_path: Path):
    contract = {"page_id": "settings", "route": "/settings", "status": "approved"}
    assert _save_contract(tmp_path, contract)["is_error"] is True

    contract["evidence"] = [{"type": "browser", "observation": "Visited /settings"}]
    assert "is_error" not in _save_contract(tmp_path, contract)
    saved = PageContract.model_validate_json((tmp_path / "raw" / "contract.jsonl").read_text(encoding="utf-8"))
    assert saved.status is ContractStatus.DRAFT


# --- Step 4: contracts mode + site profile ---------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]


def _profile(tmp_path: Path) -> Path:
    path = tmp_path / "site.yaml"
    path.write_text(
        "name: Example Site\n"
        "samples:\n  - {item: '42', locale: fr}\n  - {}\n"
        "find_samples: [an unavailable item]\n"
        "rules: [Never send messages to real users]\n",
        encoding="utf-8",
    )
    return path


def test_profile_loads_with_readable_samples(tmp_path: Path):
    profile = load_profile(_profile(tmp_path))
    assert profile.name == "Example Site"
    assert profile.sample_labels() == ["item=42 locale=fr", "(no inputs: default state)"]


def test_every_shipped_site_profile_is_valid():
    for path in (REPO_ROOT / "baseline" / "site_profiles").glob("*.yaml"):
        assert load_profile(path).name


def test_contracts_mode_requires_a_site_profile():
    from agents.discovery.cli import _parse_args

    with pytest.raises(SystemExit):
        _parse_args(["--mode", "contracts"])
    request = _parse_args(["--mode", "contracts", "--site-profile", "site.yaml"])
    assert request.mode == "contracts"
    assert request.site_profile == Path("site.yaml")


# --- Step 5: agent prompt ---------------------------------------------------------

def test_contracts_prompt_lists_only_drafts_with_samples_and_rules(tmp_path: Path):
    from agents.discovery.agent import _build_contracts_prompt
    from agents.discovery.config import DiscoveryRequest

    (tmp_path / "application.json").write_text(_model().model_dump_json(), encoding="utf-8")
    approved = PageContract(page_id="login", route="/login", status=ContractStatus.APPROVED)
    write_contract(tmp_path / "contracts", approved)

    request = DiscoveryRequest(mode="contracts", baseline_dir=tmp_path, site_profile=_profile(tmp_path))
    prompt = _build_contracts_prompt(request)

    assert "Fill in page contracts for Example Site" in prompt
    assert "article_detail.json - /articles/:id" in prompt  # skeleton written on the fly
    assert "login.json" not in prompt  # approved contracts are left alone
    assert "- item=42 locale=fr" in prompt
    assert "- an unavailable item" in prompt
    assert "Never send messages to real users" in prompt


def test_full_discovery_prompt_is_generic_without_a_profile(tmp_path: Path):
    from agents.discovery.agent import _build_prompt
    from agents.discovery.config import DiscoveryRequest

    assert "Discover the target application" in _build_prompt(DiscoveryRequest())
    named = _build_prompt(DiscoveryRequest(site_profile=_profile(tmp_path)))
    assert "Discover the Example Site application" in named
    assert "Never send messages to real users" in named


# --- Step 6: safe saving ----------------------------------------------------------

def _complete(page_id: str = "settings") -> PageContract:
    return PageContract(
        page_id=page_id, route=f"/{page_id}", samples_seen=["plan=free"],
        regions=[Region(region_id="storage-used", tolerance=Tolerance.VOLATILE, locator='role=status[name^="Storage"]',
                        order=1, format_rule=r"^\d+(\.\d+)? (MB|GB)$")],
    )


def test_save_draft_never_overwrites_an_approved_contract(tmp_path: Path):
    contracts_dir = tmp_path / "contracts"
    write_contract(contracts_dir, PageContract(page_id="settings", route="/settings", version=3, status=ContractStatus.APPROVED))

    path = save_draft(contracts_dir, _complete())

    assert path.name == "settings.draft.json"
    assert load_contract(contracts_dir, "settings").status is ContractStatus.APPROVED
    pending = PageContract.model_validate_json(path.read_text(encoding="utf-8"))
    assert (pending.status, pending.version) == (ContractStatus.DRAFT, 4)


def test_assemble_contracts_keeps_latest_save_and_leaves_application_json_alone(tmp_path: Path):
    from agents.discovery.assemble import assemble_contracts

    run_dir, baseline = tmp_path / "run", tmp_path / "discovery"
    (run_dir / "raw").mkdir(parents=True)
    baseline.mkdir()
    (baseline / "application.json").write_text("ORIGINAL", encoding="utf-8")
    first, second = _complete(), _complete()
    second.samples_seen = ["plan=free", "plan=pro"]
    (run_dir / "raw" / "contract.jsonl").write_text(first.model_dump_json() + "\n" + second.model_dump_json() + "\n", encoding="utf-8")

    assemble_contracts(run_dir, baseline / "contracts")

    assert load_contract(baseline / "contracts", "settings").samples_seen == ["plan=free", "plan=pro"]
    assert (baseline / "application.json").read_text(encoding="utf-8") == "ORIGINAL"


# --- Step 7: approve --------------------------------------------------------------

def test_approve_refuses_gaps_unless_allowed(tmp_path: Path):
    contracts_dir = tmp_path / "contracts"
    write_contract(contracts_dir, PageContract(page_id="settings", route="/settings",
                                               regions=[Region(region_id="Header", tolerance=Tolerance.STRUCTURE)]))
    with pytest.raises(ValueError, match="gap"):
        approve(contracts_dir, "settings")
    assert approve(contracts_dir, "settings", allow_gaps=True).status is ContractStatus.APPROVED


def test_approving_a_waiting_draft_replaces_the_old_version(tmp_path: Path):
    contracts_dir = tmp_path / "contracts"
    write_contract(contracts_dir, PageContract(page_id="settings", route="/settings", status=ContractStatus.APPROVED))
    save_draft(contracts_dir, _complete())

    assert cli.main(["--discovery-dir", str(tmp_path), "approve", "settings"]) == 0

    current = load_contract(contracts_dir, "settings")
    assert (current.status, current.version) == (ContractStatus.APPROVED, 2)
    assert not draft_path(contracts_dir, "settings").exists()


# --- Protected routes -------------------------------------------------------------

def test_skeleton_marks_protected_routes_as_requiring_login():
    from agents.discovery.models import RouteModel

    model = _model()
    model.routes = [RouteModel(route="/articles/:id", protected=True)]
    [contract] = build_skeletons(model)
    assert contract.requires_login is True
    assert build_skeletons(_model())[0].requires_login is False


def test_resolve_fills_samples_and_secrets_but_can_hide_secrets(monkeypatch):
    from agents.discovery.session import resolve

    monkeypatch.setenv("SITE_TEST_PASSWORD", "s3cret")
    assert resolve("{env:SITE_TEST_PASSWORD}") == "s3cret"
    assert resolve("{env:SITE_TEST_PASSWORD}", secrets=False) == "{env:SITE_TEST_PASSWORD}"
    assert resolve("item {id}", {"id": "42"}) == "item 42"
    with pytest.raises(KeyError, match="NOT_SET_ANYWHERE"):
        resolve("{env:NOT_SET_ANYWHERE}")


def test_signed_in_discovery_hides_the_session_file_from_the_agent(tmp_path: Path):
    from agents.discovery.agent import _playwright_args
    from agents.discovery.tools.safety import build_can_use_tool

    session_dir = tmp_path / "session"
    state = session_dir / "storage-state.json"
    assert _playwright_args(state)[-2:] == ["--isolated", f"--storage-state={state}"]
    assert "--storage-state" not in " ".join(_playwright_args(None))

    can_use_tool = build_can_use_tool(tmp_path / "run", secret_dir=session_dir)
    denied = asyncio.run(can_use_tool("Read", {"file_path": str(state)}, None))
    allowed = asyncio.run(can_use_tool("Read", {"file_path": str(tmp_path / "src" / "app.js")}, None))
    assert denied.behavior == "deny" and allowed.behavior == "allow"
