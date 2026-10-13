"""The shape of a page contract: a checklist of what must be on one page type."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from ..models import Evidence


class Tolerance(str, Enum):
    """How strictly a region is compared between the current and candidate builds."""

    VOLATILE = "volatile"  # value may vary by sample or day: check presence and format
    STRUCTURE = "structure"  # must match: headings, order, links, forms
    BEHAVIOR = "behavior"  # interacting must give the same outcome


class ContractStatus(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"


class Region(BaseModel):
    """One visible section of a page."""

    region_id: str
    tolerance: Tolerance
    description: str | None = None
    locator: str | None = None  # semantic selector, e.g. role=button[name="Save"]
    component: str | None = None
    order: int | None = None  # top-to-bottom position on the page
    format_rule: str | None = None  # regex the visible text must match (volatile regions)
    expected_outcome: str | None = None  # what interacting must produce (behavior regions)
    optional: bool = False  # only shown for some samples, e.g. an "unavailable" notice
    settle_timeout_ms: int = 5000
    evidence: list[Evidence] = Field(default_factory=list)


class PageContract(BaseModel):
    """Everything that must hold for one page type, across all samples from the site profile."""

    page_id: str
    route: str
    component: str | None = None
    requires_login: bool = False  # checked with the site profile's test account session
    version: int = 1
    status: ContractStatus = ContractStatus.DRAFT
    regions: list[Region] = Field(default_factory=list)
    flows: list[str] = Field(default_factory=list)  # flow_ids that pass through this page
    apis: list[str] = Field(default_factory=list)
    samples_seen: list[str] = Field(default_factory=list)  # key=value labels from the site profile
    evidence: list[Evidence] = Field(default_factory=list)

    def gaps(self) -> list[str]:
        """Missing details the comparison needs. Order, outcomes and samples are informational only."""
        missing = []
        for region in self.regions:
            if not region.locator:
                missing.append(f"{region.region_id}: locator")
            if region.tolerance is Tolerance.VOLATILE and not region.format_rule:
                missing.append(f"{region.region_id}: format_rule")
        return missing
