"""Data passed between the five agents. Plain pydantic models so every stage can be saved as JSON."""

from __future__ import annotations

import hashlib
import re
from typing import Literal

from pydantic import BaseModel, Field

from agents.discovery.contracts.profile import sample_label

Build = Literal["current", "candidate"]
RegionStatus = Literal["settled", "stuck", "missing", "unlocatable"]
Classification = Literal["expected", "cosmetic", "regression", "needs_review"]


def normalize(text: str) -> str:
    """Collapse whitespace and mask digits so counts, prices and ids don't count as differences."""
    return re.sub(r"\d+", "#", re.sub(r"\s+", " ", text)).strip()


def normalize_aria(snapshot: str) -> str:
    """Mask digits only in quoted text, keeping attributes such as [level=1] significant."""
    return re.sub(r'"[^"]*"', lambda m: re.sub(r"\d+", "#", m.group(0)), re.sub(r"[ \t]+", " ", snapshot)).strip()


class Cell(BaseModel):
    """One planned check: one page type opened with one sample."""

    page_id: str
    route: str
    path: str  # route with its parameters filled in
    sample: dict[str, str] = Field(default_factory=dict)
    pinned: bool = False
    requires_login: bool = False

    @property
    def label(self) -> str:
        return f"{self.page_id} [{sample_label(self.sample)}]"


class RegionObservation(BaseModel):
    """What one build showed for one contract region."""

    region_id: str
    status: RegionStatus
    appeared_ms: float | None = None  # since navigation started
    settled_ms: float | None = None  # when it last changed before going quiet
    text: str = ""
    aria: str = ""  # accessibility snapshot of the region
    enabled: bool | None = None
    box: tuple[float, float, float, float] | None = None  # x, y, width, height
    count: int = 0
    detail: str = ""


class SideRun(BaseModel):
    """One build's result for one cell."""

    build: Build
    url: str
    final_path: str = ""
    regions: list[RegionObservation] = Field(default_factory=list)
    metrics: dict[str, float | None] = Field(default_factory=dict)  # navigation timings in ms
    layout_shift: float | None = None
    console_errors: list[str] = Field(default_factory=list)
    network_failures: list[str] = Field(default_factory=list)
    screenshot: str | None = None
    error: str | None = None  # setup or navigation failure

    def region(self, region_id: str) -> RegionObservation | None:
        return next((r for r in self.regions if r.region_id == region_id), None)


class CellRun(BaseModel):
    cell: Cell
    current: SideRun
    candidate: SideRun
    current_first: bool


class Diff(BaseModel):
    """A difference that survived deterministic filtering and needs a judgment."""

    kind: str  # missing, extra, stuck, slow, format, value, structure, behavior, layout, order, ...
    page_id: str
    cell_label: str
    region_id: str | None = None
    tolerance: str | None = None
    component: str | None = None
    message: str
    current: str = ""
    candidate: str = ""

    @property
    def fingerprint(self) -> str:
        """Same difference on another sample or run -> same fingerprint, so judgments are reused."""
        key = "|".join([self.page_id, self.region_id or "", self.kind, normalize(self.current), normalize(self.candidate)])
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


class Judgment(BaseModel):
    classification: Classification
    confidence: float = 0.0
    reason: str = ""
    source: Literal["human", "expected_list", "cache", "model", "no_llm"]


class ExpectedChange(BaseModel):
    """A known, intended difference. Empty fields match anything."""

    page_id: str | None = None
    region_id: str | None = None
    kind: str | None = None
    reason: str

    def matches(self, diff: Diff) -> bool:
        return all(
            want is None or want == have
            for want, have in ((self.page_id, diff.page_id), (self.region_id, diff.region_id), (self.kind, diff.kind))
        )
