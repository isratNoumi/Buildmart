from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from agents.discovery.models import (
    ApplicationModel,
    ApiModel,
    ComponentModel,
    ElementModel,
    Evidence,
    FlowModel,
    FlowStep,
    Importance,
    PageModel,
    RouteModel,
)
from agents.planner.plan import generate_candidates
from agents.planner.select import changed_files, select_journeys


@pytest.fixture
def discovery_dir(tmp_path: Path) -> Path:
    model = ApplicationModel(
        routes=[RouteModel(route="/login", component="LoginPage")],
        pages=[
            PageModel(
                page_id="login",
                route="/login",
                component="LoginPage",
                elements=["submit"],
            )
        ],
        elements=[
            ElementModel(
                element_type="button",
                text="Sign in",
                selector="role=button[name='Sign in']",
                component="LoginPage",
                action="submit login form",
            )
        ],
        flows=[
            FlowModel(
                flow_id="flow-login",
                name="User logs in",
                importance=Importance.HIGH,
                steps=[FlowStep(page="login", action="submit login form")],
                evidence=[Evidence(type="dom", observation="Redirects to /account on success")],
            )
        ],
    )
    out_dir = tmp_path / "discovery"
    out_dir.mkdir()
    (out_dir / "application.json").write_text(json.dumps(model.model_dump(mode="json")), encoding="utf-8")
    return out_dir


def test_generate_candidates_writes_approved_spec(tmp_path: Path, discovery_dir: Path):
    journeys_root = tmp_path / "journeys"
    written = generate_candidates(discovery_dir=discovery_dir, journeys_root=journeys_root)

    assert len(written) == 1
    spec = yaml.safe_load(written[0].read_text(encoding="utf-8"))

    assert spec["id"] == "J001"
    assert spec["status"] == "approved"
    assert spec["area"] == "login"
    assert spec["steps"][0] == {"action": "navigate", "target": "/login"}
    assert spec["steps"][1]["action"] == "click"
    assert spec["steps"][1]["target"] == "role=button[name='Sign in']"
    assert spec["assertions"] == ["Redirects to /account on success"]
    assert spec["provenance"]["source_flow_id"] == "flow-login"

    registry = yaml.safe_load((journeys_root / "registry.yaml").read_text(encoding="utf-8"))
    assert registry["J001"]["area"] == "login"
    assert registry["J001"]["status"] == "approved"


@pytest.mark.parametrize("route, area", [
    ("/", "home"), ("/settings/profile", "settings"), ("/Order-Confirmation/:id", "order-confirmation"),
    ("/:slug", "other"), ("*", "other"),
])
def test_area_comes_from_first_route_segment(route: str, area: str):
    from agents.planner.plan import _area_for_route

    assert _area_for_route(route) == area


def test_change_selection_follows_shared_components_and_apis(tmp_path: Path, discovery_dir: Path):
    model_path = discovery_dir / "application.json"
    model = ApplicationModel.model_validate_json(model_path.read_text(encoding="utf-8"))
    model.components = [
        ComponentModel(name="LoginPage", file="frontend/src/Login.jsx", children=["Form"]),
        ComponentModel(name="Form", file="frontend/src/Form.jsx", children=["Form"]),
        ComponentModel(name="ProductPage", file="frontend/src/Product.jsx"),
    ]
    model.apis = [ApiModel(method="POST", endpoint="/login", used_by=["Form"], evidence=[
        Evidence(type="source", file="backend/auth.py")
    ])]
    model.pages.append(PageModel(page_id="products", route="/products", component="ProductPage"))
    model.flows.append(FlowModel(flow_id="products", name="Browse", steps=[
        FlowStep(page="/products", action="Browse products")
    ]))
    model_path.write_text(model.model_dump_json(), encoding="utf-8")
    journeys_root = tmp_path / "journeys"
    generate_candidates(discovery_dir, journeys_root)

    for changed in ["frontend/src/Login.jsx", "frontend\\src\\Form.jsx", "backend/auth.py"]:
        report = select_journeys([changed], discovery_dir, journeys_root)
        assert [journey["id"] for journey in report["selected"]] == ["J001"]
        assert [journey["id"] for journey in report["skipped"]] == ["J002"]
        assert report["unmapped_files"] == []

    report = select_journeys(["new-module.py"], discovery_dir, journeys_root)
    assert len(report["selected"]) == 2
    assert report["unmapped_files"] == ["new-module.py"]
    assert select_journeys([], discovery_dir, journeys_root)["selected"] == []


def test_unmapped_journey_is_selected_but_retired_is_not(tmp_path: Path, discovery_dir: Path):
    journeys_root = tmp_path / "journeys"
    written = generate_candidates(discovery_dir, journeys_root)
    spec = yaml.safe_load(written[0].read_text(encoding="utf-8"))
    spec["provenance"]["source_flow_id"] = "missing"
    written[0].write_text(yaml.safe_dump(spec), encoding="utf-8")
    assert len(select_journeys(["unknown.py"], discovery_dir, journeys_root)["selected"]) == 1
    spec["status"] = "retired"
    written[0].write_text(yaml.safe_dump(spec), encoding="utf-8")
    assert select_journeys(["unknown.py"], discovery_dir, journeys_root)["selected"] == []


def test_git_selection_includes_committed_local_and_untracked_changes(monkeypatch, tmp_path: Path):
    import subprocess

    calls = []
    outputs = [b"src/deleted.jsx\0src/renamed.jsx\0", b"src/local.jsx\0", b"src/new.jsx\0"]

    def fake_run(command, **kwargs):
        calls.append(command)
        assert kwargs["check"] is True
        return subprocess.CompletedProcess(command, 0, stdout=outputs[len(calls) - 1])

    monkeypatch.setattr("agents.planner.select.subprocess.run", fake_run)
    assert changed_files(tmp_path, "main") == [
        "src/deleted.jsx", "src/local.jsx", "src/new.jsx", "src/renamed.jsx"
    ]
    assert "main...HEAD" in calls[0]
    assert "--no-renames" in calls[0]
    assert "HEAD" in calls[1]
    assert "--others" in calls[2]


def test_runner_honors_selection_report(monkeypatch, tmp_path: Path, discovery_dir: Path):
    from agents.e2e import run_journeys

    journeys_root = tmp_path / "journeys"
    generate_candidates(discovery_dir, journeys_root)
    monkeypatch.setattr(run_journeys, "JOURNEYS_ROOT", journeys_root)
    report_path = tmp_path / "selection.json"
    report_path.write_text(json.dumps({"selected": [{"id": "J001"}]}), encoding="utf-8")
    assert [spec["id"] for spec in run_journeys.load_selected_specs(report_path)] == ["J001"]
    report_path.write_text(json.dumps({"selected": []}), encoding="utf-8")
    assert run_journeys.select_and_run("https://example.test", tmp_path, True, tmp_path, report_path) == []
    report_path.write_text(json.dumps({"selected": [{"id": "J999"}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="missing or not approved"):
        run_journeys.load_selected_specs(report_path)
    report_path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="selected"):
        run_journeys.load_selected_specs(report_path)
