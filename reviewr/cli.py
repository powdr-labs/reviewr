"""Command-line interface: `reviewr ...`."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import click

from .config import load_config
from .orchestrator import Orchestrator
from . import pr as pr_mod


@click.group()
@click.version_option()
def main() -> None:
    """Multi-AI consensus code reviewer."""


@main.command()
@click.argument("ref", required=False)
@click.option("--config", "config_path", default="reviewr.toml",
              help="Path to reviewr.toml.")
@click.option("--repo", "repo_dir", default=".",
              help="Repository directory the reviewers run in.")
@click.option("--base", help="Local mode: base ref for `git diff base...head`.")
@click.option("--head", default="HEAD", help="Local mode: head ref.")
@click.option("--diff-file", help="Review a diff from a file instead of a PR.")
@click.option("--run-dir", help="Where to write artifacts (default .reviewr/runs/<ts>).")
def review(ref, config_path, repo_dir, base, head, diff_file, run_dir) -> None:
    """Review a PR (REF = number/URL), a local diff (--base), or a --diff-file."""
    config = load_config(config_path)
    repo_dir = str(Path(repo_dir).resolve())

    if diff_file:
        pr = pr_mod.from_diff_file(diff_file, repo_dir)
    elif base:
        pr = pr_mod.from_git(base, head, repo_dir)
    elif ref:
        pr = pr_mod.from_github(ref, repo_dir)
    else:
        raise click.UsageError("provide a PR REF, or --base, or --diff-file")

    if run_dir:
        rdir = Path(run_dir)
    else:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        rdir = Path(repo_dir) / ".reviewr" / "runs" / ts

    click.echo(f"Reviewing: {pr.short()}")
    click.echo(f"Reviewers: {', '.join(r.name for r in config.reviewers)}")
    click.echo(f"Artifacts: {rdir}\n")

    orch = Orchestrator(config, pr, rdir, log=click.echo)
    review_md = asyncio.run(orch.run())

    click.echo("\n" + "=" * 60)
    click.echo(review_md)


if __name__ == "__main__":
    main()
