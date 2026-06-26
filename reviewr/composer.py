"""Produce the final review.

The authoritative artifact is a structured `ComposedReview` (JSON): the editor
AI merges the panel's confirmed findings, grouping every file/line a single
issue touches into one finding with a `locations` list. The human-readable
`REVIEW.md` is then rendered deterministically from that JSON, so the two never
disagree.
"""

from __future__ import annotations

import json
from pathlib import Path

from .backends import run_reviewer
from .config import Config
from .findings import (
    ComposedFinding,
    ComposedReview,
    Location,
    SEVERITY_ORDER,
    Severity,
    composed_json_schema,
)
from .pr import PRContext
from .prompts import render_compose
from .state import ReviewState

_SEV_TITLE = {
    Severity.critical: "Critical",
    Severity.high: "High",
    Severity.medium: "Medium",
    Severity.low: "Low",
    Severity.nit: "Nits",
}


def _deterministic_review(state: ReviewState) -> ComposedReview:
    """Fallback when no editor AI is configured/available: 1 location each."""
    findings = [
        ComposedFinding(
            title=tf.finding.title,
            severity=tf.finding.severity,
            category=tf.finding.category,
            locations=[Location(file=tf.finding.file, line=tf.finding.line)],
            description=tf.finding.description,
            suggested_fix=tf.finding.suggested_fix,
        )
        for tf in state.confirmed_sorted()
    ]
    verdict = "Approve" if not findings else "Review the findings below."
    return ComposedReview(
        summary="Automated multi-AI consensus review.",
        verdict=verdict,
        findings=findings,
    )


def render_markdown(review: ComposedReview, pr: PRContext | None = None) -> str:
    title = pr.title if pr else None
    lines: list[str] = []
    if title:
        lines += [f"# Review: {title}", ""]
    if review.summary:
        lines += [review.summary, ""]

    if not review.findings:
        lines += ["No issues agreed on by the panel.", ""]
    else:
        by_sev: dict[Severity, list[ComposedFinding]] = {}
        for f in review.findings:
            by_sev.setdefault(f.severity, []).append(f)
        for sev in sorted(by_sev, key=lambda s: SEVERITY_ORDER[s]):
            lines += [f"## {_SEV_TITLE[sev]}", ""]
            for f in by_sev[sev]:
                lines.append(f"### {f.title}")
                locs = ", ".join(
                    f"`{loc.file}" + (f":{loc.line}" if loc.line else "") + "`"
                    for loc in f.locations
                )
                lines += [f"**Location(s):** {locs} · {f.category.value}", ""]
                lines.append(f.description)
                if f.suggested_fix:
                    lines += ["", f"**Suggested fix:** {f.suggested_fix}"]
                lines.append("")

    if review.verdict:
        lines += ["## Verdict", "", review.verdict, ""]
    return "\n".join(lines)


async def compose(
    pr: PRContext, state: ReviewState, config: Config, run_dir: Path
) -> tuple[ComposedReview, str]:
    """Return (structured review, rendered markdown)."""
    confirmed = [
        {**tf.finding.model_dump(), "id": tf.id} for tf in state.confirmed_sorted()
    ]

    rv = config.reviewer(config.composer.reviewer) if config.composer.reviewer else None
    if rv is None:
        review = _deterministic_review(state)
        return review, render_markdown(review, pr)

    schema_path = run_dir / "composed-schema.json"
    schema_path.write_text(json.dumps(composed_json_schema(), indent=2))
    prompt = render_compose(pr, confirmed)

    res = await run_reviewer(
        rv,
        prompt,
        schema_path=schema_path,
        repo_dir=pr.repo_dir,
        artifact_dir=run_dir,
        tag="compose",
        output_model=ComposedReview,
    )
    review = res.output if res.output else _deterministic_review(state)
    return review, render_markdown(review, pr)
