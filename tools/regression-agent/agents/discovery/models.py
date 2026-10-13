from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class Confidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNCERTAIN = "UNCERTAIN"


class Importance(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class InteractionClass(str, Enum):
    READ_ONLY = "READ_ONLY"
    STATE_CHANGE = "STATE_CHANGE"
    DESTRUCTIVE = "DESTRUCTIVE"
    EXTERNAL = "EXTERNAL"


class Evidence(BaseModel):
    """Where a finding came from. Required on every non-structural fact (plan/discovery.md section 19)."""

    type: str  # "source" | "network" | "runtime" | "dom"
    file: str | None = None
    line: int | None = None
    method: str | None = None
    endpoint: str | None = None
    observation: str | None = None


class RouteModel(BaseModel):
    route: str
    component: str | None = None
    route_type: str = "static"  # static | dynamic
    protected: bool = False
    source_file: str | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    confidence: Confidence = Confidence.MEDIUM


class ComponentModel(BaseModel):
    name: str
    file: str
    parent: str | None = None
    children: list[str] = Field(default_factory=list)
    page: str | None = None
    evidence: list[Evidence] = Field(default_factory=list)


class ApiModel(BaseModel):
    method: str
    endpoint: str
    used_by: list[str] = Field(default_factory=list)
    purpose: str | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    confidence: Confidence = Confidence.MEDIUM


class ElementModel(BaseModel):
    element_type: str
    text: str | None = None
    selector: str
    component: str | None = None
    action: str | None = None
    importance: Importance = Importance.MEDIUM
    interaction_class: InteractionClass = InteractionClass.READ_ONLY
    evidence: list[Evidence] = Field(default_factory=list)


class DynamicElementModel(BaseModel):
    element: str
    selector: str | None = None
    dynamic: bool = True
    reason: str
    evidence: list[Evidence] = Field(default_factory=list)


class FlowStep(BaseModel):
    page: str
    action: str


class FlowModel(BaseModel):
    flow_id: str
    name: str
    importance: Importance = Importance.MEDIUM
    steps: list[FlowStep]
    evidence: list[Evidence] = Field(default_factory=list)
    confidence: Confidence = Confidence.MEDIUM


class DependencyModel(BaseModel):
    source: str
    target: str
    relationship: str  # e.g. "renders", "calls", "navigates_to"
    evidence: list[Evidence] = Field(default_factory=list)


class PageModel(BaseModel):
    page_id: str
    route: str
    component: str | None = None
    importance: Importance = Importance.MEDIUM
    components: list[str] = Field(default_factory=list)
    apis: list[str] = Field(default_factory=list)
    elements: list[str] = Field(default_factory=list)
    dynamic_elements: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)


class BaselineModel(BaseModel):
    screenshot: str | None = None
    dom_snapshot: str | None = None
    url: str | None = None
    console_errors: list[str] = Field(default_factory=list)


class ApplicationModel(BaseModel):
    application: dict = Field(default_factory=dict)
    routes: list[RouteModel] = Field(default_factory=list)
    pages: list[PageModel] = Field(default_factory=list)
    components: list[ComponentModel] = Field(default_factory=list)
    apis: list[ApiModel] = Field(default_factory=list)
    elements: list[ElementModel] = Field(default_factory=list)
    flows: list[FlowModel] = Field(default_factory=list)
    dynamic_elements: list[DynamicElementModel] = Field(default_factory=list)
    dependencies: list[DependencyModel] = Field(default_factory=list)
    baseline: BaselineModel = Field(default_factory=BaselineModel)
