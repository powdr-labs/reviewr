"""Produce the final REVIEW.md from the converged state."""

from __future__ import annotations

import asyncio
from pathlib import Path

from .backends import run_reviewer
from .config import Config
from .findings import SEVERITY_ORDER, Severity
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


def _deterministic_review(pr: PRContext, state: ReviewState) -> str:
    """Fallback composer: render markdown directly, no AI editor."""
    lines = [f"# Review: {pr.title}", ""]
    confirmed = state.confirmed_sorted()
    if not confirmed:
        lines.append("No issues agreed on by the panel.")
        return "\n".join(lines) + "\n"

    by_sev: dict[Severity, list] = {}
    for tf in confirmed:
        by_sev.setdefault(tf.finding.severity, []).append(tf)

    for sev in sorted(by_sev, key=lambda s: SEVERITY_ORDER[s]):
        lines.append(f"## {_SEV_TITLE[sev]}")
        lines.append("")
        for tf in by_sev[sev]:
            f = tf.finding
            loc = f.file + (f":{f.line}" if f.line else "")
            lines.append(f"### {f.title}")
            lines.append(f"`{loc}` · {f.category.value}")
            lines.append("")
            lines.append(f.description)
            if f.suggested_fix:
                lines.append("")
                lines.append(f"**Suggested fix:** {f.suggested_fix}")
            lines.append("")
    return "\n".join(lines) + "\n"


async def compose(
    pr: PRContext, state: ReviewState, config: Config, run_dir: Path
) -> str:
    confirmed = [
        {**tf.finding.model_dump(), "id": tf.id} for tf in state.confirmed_sorted()
    ]

    if not config.composer.reviewer:
        return _deterministic_review(pr, state)

    rv = config.reviewer(config.composer.reviewer)
    if rv is None:
        return _deterministic_review(pr, state)

    prompt = render_compose(pr, confirmed)
    # Compose returns prose markdown, not structured JSON -> read raw output.
    raw_rv = type(rv)(
        name=rv.name,
        command=rv.command,
        prompt_via=rv.prompt_via,
        result_from=rv.result_from,
        schema_arg=None,  # no schema: we want free-form markdown
        last_message_arg=rv.last_message_arg,
        timeout=rv.timeout,
        env=rv.env,
    )
    res = await run_reviewer(
        raw_rv,
        prompt,
        schema_path=None,
        repo_dir=pr.repo_dir,
        artifact_dir=run_dir,
        tag="compose",
    )
    # res.output will be None (markdown isn't valid ReviewerOutput JSON); use raw.
    text = res.raw.strip()
    if not text:
        return _deterministic_review(pr, state)
    return text + "\n"
