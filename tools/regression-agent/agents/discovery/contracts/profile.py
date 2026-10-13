"""A site profile: everything site-specific the agents need, so the code stays generic."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class SetupStep(BaseModel):
    """One UI step that applies a sample value, e.g. type a postcode into a location picker."""

    action: Literal["goto", "click", "fill"]
    target: str  # visible text or label to act on; a path for "goto"
    value: str | None = None  # may use {key} sample values and {env:NAME} secrets


class Login(BaseModel):
    """How to sign in as the test account. Put secrets in .env and refer to them as {env:NAME}."""

    steps: list[SetupStep]
    success_text: str | None = None  # text visible only after signing in, to confirm it worked


class SiteProfile(BaseModel):
    name: str
    # Inputs to visit pages with; keys are free-form. A key named like a route
    # parameter (e.g. "id" for /items/:id) fills that parameter.
    samples: list[dict[str, str]] = Field(default_factory=list)  # rotated between runs
    pinned: list[dict[str, str]] = Field(default_factory=list)  # tested on every run
    # Situations the agent must find on its own, e.g. "an item that is unavailable".
    find_samples: list[str] = Field(default_factory=list)
    rules: list[str] = Field(default_factory=list)
    # Sample key -> steps that apply it in the browser before the page is opened.
    setup: dict[str, list[SetupStep]] = Field(default_factory=dict)
    login: Login | None = None

    def sample_labels(self) -> list[str]:
        return [sample_label(sample) for sample in self.pinned + self.samples]


def sample_label(sample: dict[str, str]) -> str:
    return " ".join(f"{key}={value}" for key, value in sample.items()) or "(no inputs: default state)"


def load_profile(path: Path) -> SiteProfile:
    return SiteProfile.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
