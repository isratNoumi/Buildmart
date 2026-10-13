"""Agent 4 - Judge: classify each surviving diff as expected, cosmetic or regression.

Order of authority (first that applies wins):
    1. human label        output/parity_cache/labels.jsonl  (python -m agents.parity label FINGERPRINT CLASS)
    2. expected changes   --expected-changes FILE    (known intended differences)
    3. cache              output/parity_cache/judgments.jsonl (earlier model answers, keyed by fingerprint)
    4. AI model           one short call per new diff; low confidence -> needs_review
Human labels + cached model answers together form the labelled set for evaluating other models.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from pathlib import Path

from .models import Diff, ExpectedChange, Judgment

SYSTEM_PROMPT = """You review one difference between the current build and a candidate build of a web application.
Both builds were opened with the same inputs seconds apart. Classify the difference:
- expected: an intended change listed in the expected changes
- cosmetic: looks different, but users can see and do everything the same way
- regression: users lose information, functionality, accessibility or speed
Answer with JSON only: {"classification": "expected|cosmetic|regression", "confidence": 0.0-1.0, "reason": "one sentence"}"""

_CLASSES = {"expected", "cosmetic", "regression"}


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def build_prompt(diff: Diff, site_name: str, expected: list[ExpectedChange]) -> str:
    notes = "\n".join(f"- {e.reason}" for e in expected) or "- (none)"
    return (
        f"Site: {site_name}\nPage: {diff.cell_label}\nRegion: {diff.region_id or '(whole page)'} "
        f"[{diff.tolerance or 'page'}] component {diff.component or 'unknown'}\n"
        f"Difference ({diff.kind}): {diff.message}\n"
        f"Current:\n{diff.current[:800]}\nCandidate:\n{diff.candidate[:800]}\n"
        f"Expected changes:\n{notes}"
    )


def parse_answer(text: str) -> Judgment | None:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    try:
        data = json.loads(match.group(0)) if match else None
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("classification") not in _CLASSES:
        return None
    try:
        confidence = min(max(float(data.get("confidence", 0)), 0.0), 1.0)
    except (TypeError, ValueError):
        return None
    return Judgment(classification=data["classification"], confidence=confidence,
                    reason=str(data.get("reason", ""))[:500], source="model")


def ask_claude(prompt: str) -> str:
    """One tool-less model call. Uses the same API key as the discovery agent."""
    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

    from agents.discovery.config import settings

    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    options = ClaudeAgentOptions(system_prompt=SYSTEM_PROMPT, tools=[], max_turns=1,
                                 env={"ANTHROPIC_API_KEY": settings.anthropic_api_key})

    async def run() -> str:
        parts = []
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                parts += [block.text for block in message.content if isinstance(block, TextBlock)]
        return "".join(parts)

    return asyncio.run(run())


class Judge:
    def __init__(self, cache_dir: Path, site_name: str, expected: list[ExpectedChange] | None = None,
                 ask: Callable[[str], str] | None = None, min_confidence: float = 0.7):
        self.cache_path = cache_dir / "judgments.jsonl"
        self.site_name = site_name
        self.expected = expected or []
        self.ask = ask
        self.min_confidence = min_confidence
        self.labels = {r["fingerprint"]: r for r in _read_jsonl(cache_dir / "labels.jsonl")}
        self.cache = {r["fingerprint"]: Judgment.model_validate(r["judgment"]) for r in _read_jsonl(self.cache_path)}

    def judge(self, diff: Diff) -> Judgment:
        fingerprint = diff.fingerprint
        if fingerprint in self.labels:
            label = self.labels[fingerprint]
            return Judgment(classification=label["classification"], confidence=1.0,
                            reason=label.get("note") or "Confirmed by a person", source="human")
        for change in self.expected:
            if change.matches(diff):
                return Judgment(classification="expected", confidence=1.0, reason=change.reason, source="expected_list")
        if fingerprint in self.cache:
            return self.cache[fingerprint].model_copy(update={"source": "cache"})
        if self.ask is None:
            return Judgment(classification="needs_review", reason="AI judge disabled (--no-llm)", source="no_llm")

        try:
            answer = parse_answer(self.ask(build_prompt(diff, self.site_name, self.expected)))
        except Exception as error:  # network/SDK failure must not stop the run
            return Judgment(classification="needs_review", reason=f"AI judge unavailable: {error}", source="no_llm")
        if answer is None:
            answer = Judgment(classification="needs_review", reason="AI answer could not be read", source="model")
        elif answer.confidence < self.min_confidence:
            answer = answer.model_copy(update={
                "classification": "needs_review",
                "reason": f"Low confidence ({answer.confidence:.2f}) suggestion '{answer.classification}': {answer.reason}",
            })
        self.cache[fingerprint] = answer
        _append_jsonl(self.cache_path, {"fingerprint": fingerprint, "diff": diff.model_dump(), "judgment": answer.model_dump()})
        return answer


def add_label(cache_dir: Path, fingerprint: str, classification: str, note: str = "") -> None:
    if classification not in _CLASSES:
        raise ValueError(f"classification must be one of {sorted(_CLASSES)}")
    _append_jsonl(cache_dir / "labels.jsonl", {"fingerprint": fingerprint, "classification": classification, "note": note})
