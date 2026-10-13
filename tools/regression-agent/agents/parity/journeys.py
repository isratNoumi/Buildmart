"""Compare one journey's run on the current build with its run on the candidate build."""

from __future__ import annotations

from dataclasses import asdict
from itertools import zip_longest
from typing import Any

from agents.e2e import run_journeys

TIMING_KEYS = ("ttfb_ms", "dom_content_loaded_ms", "load_ms", "first_contentful_paint_ms")
# Most severe first; a journey gets the first verdict whose condition holds.
VERDICTS = ("REGRESSION", "BEHAVIOUR_CHANGE", "SLOWER", "IMPROVED", "UNCHANGED")


def journey_to_dict(result: run_journeys.JourneyResult) -> dict[str, Any]:
    data = asdict(result)
    data["duration_ms"] = result.duration_ms
    for key in ("screenshot", "artifacts_dir"):
        data[key] = str(data[key]) if data[key] else None
    return data


def _completed(journey: dict[str, Any]) -> bool:
    return bool(journey["steps"]) and all(step["status"] == "PASS" for step in journey["steps"])


def _step_change(baseline: str | None, candidate: str | None) -> str:
    if baseline == candidate:
        return "same"
    if baseline == "PASS":
        return "regressed"
    if candidate == "PASS":
        return "fixed"
    return "changed"


def _side(step: dict[str, Any] | None) -> dict[str, Any] | None:
    return {key: step[key] for key in ("status", "duration_ms", "detail")} if step else None


def _timing(metric: str, baseline: float | None, candidate: float | None, threshold_pct: float, min_ms: float) -> dict[str, Any] | None:
    if baseline is None or candidate is None:
        return None
    delta = candidate - baseline
    pct = delta / baseline * 100 if baseline else None
    significant = abs(delta) >= min_ms and (pct is None or abs(pct) >= threshold_pct)
    return {
        "metric": metric,
        "baseline_ms": baseline,
        "candidate_ms": candidate,
        "delta_ms": round(delta, 1),
        "delta_pct": round(pct, 1) if pct is not None else None,
        "trend": ("slower" if delta > 0 else "faster") if significant else "same",
    }


def compare_journey(baseline: dict[str, Any], candidate: dict[str, Any], threshold_pct: float = 20.0, min_ms: float = 100.0) -> dict[str, Any]:
    steps = []
    timings = []
    for base_step, cand_step in zip_longest(baseline["steps"], candidate["steps"]):
        ref = base_step or cand_step
        steps.append({
            "index": ref["index"],
            "action": ref["action"],
            "target": ref["target"],
            "baseline": _side(base_step),
            "candidate": _side(cand_step),
            "change": _step_change(base_step and base_step["status"], cand_step and cand_step["status"]),
        })
        if base_step and cand_step and ref["action"] == "navigate":
            for key in TIMING_KEYS:
                timing = _timing(f"{ref['target']} {key}", base_step["metrics"].get(key), cand_step["metrics"].get(key), threshold_pct, min_ms)
                if timing:
                    timings.append(timing)
    total = _timing("journey duration_ms", baseline["duration_ms"], candidate["duration_ms"], threshold_pct, min_ms)
    if total:
        timings.insert(0, total)

    base_console, cand_console = set(baseline["console_errors"]), set(candidate["console_errors"])
    base_network, cand_network = set(baseline["network_failures"]), set(candidate["network_failures"])
    diff = {
        "new_console_errors": sorted(cand_console - base_console),
        "resolved_console_errors": sorted(base_console - cand_console),
        "new_network_failures": sorted(cand_network - base_network),
        "resolved_network_failures": sorted(base_network - cand_network),
    }

    findings: list[str] = []
    changes = {step["change"] for step in steps}
    base_done, cand_done = _completed(baseline), _completed(candidate)
    if base_done and not cand_done:
        findings.append("User can no longer complete the journey on the candidate build")
    elif cand_done and not base_done:
        findings.append("Journey now completes on the candidate build")
    for step in steps:
        if step["change"] != "same":
            base_status = step["baseline"]["status"] if step["baseline"] else "missing"
            cand_status = step["candidate"]["status"] if step["candidate"] else "missing"
            detail = (step["candidate"] or {}).get("detail") or ""
            findings.append(f"Step {step['index']} {step['action']} '{step['target']}': {base_status} -> {cand_status}" + (f" ({detail})" if detail else ""))
    if baseline["classification"] != candidate["classification"]:
        findings.append(f"Classification {baseline['classification']} -> {candidate['classification']}")
    if baseline["final_url"] != candidate["final_url"]:
        findings.append(f"Journey ends on '{candidate['final_url']}' instead of '{baseline['final_url']}'")
    findings += [f"New console error: {e}" for e in diff["new_console_errors"]]
    findings += [f"New network failure: {e}" for e in diff["new_network_failures"]]
    findings += [
        f"{t['metric']} {t['trend']}: {t['baseline_ms']} -> {t['candidate_ms']} ms ({t['delta_ms']:+} ms)"
        for t in timings if t["trend"] != "same"
    ]

    trends = {t["trend"] for t in timings}
    conditions = {
        "REGRESSION": "regressed" in changes or (base_done and not cand_done),
        "BEHAVIOUR_CHANGE": "changed" in changes
        or baseline["classification"] != candidate["classification"]
        or baseline["final_url"] != candidate["final_url"]
        or bool(diff["new_console_errors"] or diff["new_network_failures"]),
        "SLOWER": "slower" in trends,
        "IMPROVED": "fixed" in changes or "faster" in trends
        or bool(diff["resolved_console_errors"] or diff["resolved_network_failures"]),
        "UNCHANGED": True,
    }
    return {
        "journey_id": baseline["journey_id"],
        "name": baseline["name"],
        "verdict": next(v for v in VERDICTS if conditions[v]),
        "findings": findings,
        "baseline": {key: baseline[key] for key in ("classification", "duration_ms", "final_url", "screenshot", "artifacts_dir")} | {"completed": base_done},
        "candidate": {key: candidate[key] for key in ("classification", "duration_ms", "final_url", "screenshot", "artifacts_dir")} | {"completed": cand_done},
        "steps": steps,
        "timings": timings,
        **diff,
    }
