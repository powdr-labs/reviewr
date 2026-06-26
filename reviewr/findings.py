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


def output_json_schema() -> dict:
    """JSON Schema handed to the CLIs for structured output."""
    return ReviewerOutput.model_json_schema()
