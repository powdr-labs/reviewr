"""Data model for findings, votes, and the per-reviewer structured output.

The same `ReviewerOutput` schema is used in every round:
  - round 0: reviewers fill `new_findings` (and `summary`); `votes` is empty.
  - later rounds: reviewers fill `votes` (on the shared, anonymized findings)
    and may add anything missed to `new_findings`.

This schema is what we hand to the AI CLIs as a JSON Schema for structured
output (`claude --json-schema`, `codex exec --output-schema`).
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Severity(str, Enum):
    critical = "critical"
    high = "high"
    medium = "medium"
    low = "low"
    nit = "nit"


# Lower is more severe; used for sorting in the composed output.
SEVERITY_ORDER = {
    Severity.critical: 0,
    Severity.high: 1,
    Severity.medium: 2,
    Severity.low: 3,
    Severity.nit: 4,
}


class Category(str, Enum):
    bug = "bug"
    security = "security"
    performance = "performance"
    correctness = "correctness"
    maintainability = "maintainability"
    style = "style"
    other = "other"


class Stance(str, Enum):
    agree = "agree"      # this finding is real and worth reporting
    dispute = "dispute"  # this finding is wrong / not an issue
    unsure = "unsure"    # cannot tell yet


class Finding(BaseModel):
    title: str = Field(description="One-line summary of the issue.")
    severity: Severity
    category: Category
    file: str = Field(description="Path of the file the issue is in.")
    line: Optional[int] = Field(
        default=None, description="Line number if applicable."
    )
    description: str = Field(
        description="What is wrong and why it matters. Be specific and concrete."
    )
    suggested_fix: Optional[str] = Field(
        default=None, description="How to fix it, if you have a concrete suggestion."
    )


class Vote(BaseModel):
    finding_id: str = Field(description="The id of the finding being voted on.")
    stance: Stance
    reason: str = Field(
        default="",
        description="Brief justification. Required when disputing.",
    )


class ReviewerOutput(BaseModel):
    """The structured artifact each reviewer must emit every round."""

    summary: str = Field(
        default="",
        description="A short, anonymous summary of your overall assessment.",
    )
    new_findings: list[Finding] = Field(
        default_factory=list,
        description="Issues not already present in the shared findings list.",
    )
    votes: list[Vote] = Field(
        default_factory=list,
        description="Your vote on each finding in the shared findings list.",
    )


class Location(BaseModel):
    file: str = Field(description="Path of the file.")
    line: Optional[int] = Field(default=None, description="Line number if applicable.")


class ComposedFinding(BaseModel):
    """A finding in the final, edited review. May span several locations."""

    title: str = Field(description="One-line summary of the issue.")
    severity: Severity
    category: Category
    locations: list[Location] = Field(
        description="Every file/line this finding applies to (>=1)."
    )
    description: str = Field(description="What is wrong and why it matters.")
    suggested_fix: Optional[str] = Field(default=None)


class ComposedReview(BaseModel):
    """The final review: the authoritative JSON the Markdown is rendered from."""

    summary: str = Field(description="2-4 sentence overall assessment.")
    verdict: str = Field(
        description="e.g. 'Approve', 'Request changes', with brief reasoning."
    )
    findings: list[ComposedFinding] = Field(default_factory=list)


def _strictify(node):
    """Make a JSON Schema satisfy OpenAI/Codex strict structured-output rules.

    Every object must set `additionalProperties: false` and list *all* of its
    properties in `required`; `default` keys are not allowed. Optional fields
    stay expressible because pydantic already emits them as nullable
    (`anyOf: [..., {"type": "null"}]`), so a model can answer null.
    """
    if isinstance(node, dict):
        node.pop("default", None)
        if node.get("type") == "object" and "properties" in node:
            node["additionalProperties"] = False
            node["required"] = list(node["properties"].keys())
        for v in node.values():
            _strictify(v)
    elif isinstance(node, list):
        for v in node:
            _strictify(v)
    return node


def output_json_schema() -> dict:
    """Strict JSON Schema handed to the CLIs for structured output."""
    return _strictify(ReviewerOutput.model_json_schema())


def composed_json_schema() -> dict:
    """Strict JSON Schema for the final composed review."""
    return _strictify(ComposedReview.model_json_schema())
