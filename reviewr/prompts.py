"""Jinja2 environment for rendering reviewer prompts."""

from __future__ import annotations

import json
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .pr import PRContext

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "prompts"

_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(enabled_extensions=()),
    trim_blocks=True,
    lstrip_blocks=True,
)
_env.filters["tojson_pretty"] = lambda v: json.dumps(v, indent=2)


def render_review(pr: PRContext) -> str:
    return _env.get_template("review.j2").render(pr=pr)


def render_critique(pr: PRContext, findings_view: list[dict], round_idx: int) -> str:
    return _env.get_template("critique.j2").render(
        pr=pr, findings=findings_view, round_idx=round_idx
    )


def render_compose(pr: PRContext, findings: list[dict]) -> str:
    return _env.get_template("compose.j2").render(pr=pr, findings=findings)
