from __future__ import annotations

from agents.parity.journeys import compare_journey


def _step(index, action, target, status="PASS", duration=50.0, metrics=None, detail=""):
    return {"index": index, "action": action, "target": target, "status": status,
            "detail": detail, "duration_ms": duration, "metrics": metrics or {}}


def _journey(steps, **overrides):
    journey = {
        "journey_id": "J001", "name": "Login", "classification": "APPLICABLE", "steps": steps,
        "console_errors": [], "network_failures": [], "final_url": "/account",
        "screenshot": None, "artifacts_dir": None,
        "duration_ms": sum(s["duration_ms"] for s in steps),
    }
    journey.update(overrides)
    return journey


def _nav(load_ms, duration=200.0):
    return _step(0, "navigate", "/login", duration=duration, metrics={"load_ms": load_ms, "ttfb_ms": 20})


def test_identical_runs_are_unchanged():
    base = _journey([_nav(400), _step(1, "click", "Sign in")])
    result = compare_journey(base, base)
    assert result["verdict"] == "UNCHANGED"
    assert result["findings"] == []


def test_step_failure_is_regression_with_finding():
    base = _journey([_nav(400), _step(1, "click", "Sign in")])
    cand = _journey([_nav(400), _step(1, "click", "Sign in", "FAIL", detail="no element matching 'Sign in'")],
                    classification="APPLICABLE", final_url="/login")
    result = compare_journey(base, cand)
    assert result["verdict"] == "REGRESSION"
    assert result["steps"][1]["change"] == "regressed"
    assert "User can no longer complete the journey on the candidate build" in result["findings"]
    assert any("PASS -> FAIL" in f for f in result["findings"])
    assert any("ends on '/login'" in f for f in result["findings"])


def test_loading_time_respects_thresholds():
    base = _journey([_nav(400)])
    slower = compare_journey(base, _journey([_nav(900)]), threshold_pct=20, min_ms=100)
    assert slower["verdict"] == "SLOWER"
    load = next(t for t in slower["timings"] if t["metric"] == "/login load_ms")
    assert (load["delta_ms"], load["delta_pct"], load["trend"]) == (500, 125.0, "slower")

    noise = compare_journey(base, _journey([_nav(450)]), threshold_pct=20, min_ms=100)
    assert noise["verdict"] == "UNCHANGED"

    faster = compare_journey(base, _journey([_nav(100)]), threshold_pct=20, min_ms=100)
    assert faster["verdict"] == "IMPROVED"


def test_new_console_error_and_network_failure_are_behaviour_changes():
    base = _journey([_nav(400)])
    cand = _journey([_nav(400)], console_errors=["boom"], network_failures=["GET /api/me -> HTTP 500"])
    result = compare_journey(base, cand)
    assert result["verdict"] == "BEHAVIOUR_CHANGE"
    assert result["new_console_errors"] == ["boom"]
    assert result["new_network_failures"] == ["GET /api/me -> HTTP 500"]
