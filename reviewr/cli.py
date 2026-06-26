"""Command-line interface: `reviewr ...`."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import click

from .config import load_config
from .orchestrator import Orchestrator
from . import pr as pr_mod

DEFAULT_CLONE_DIR = "~/.cache/reviewr/clones"


@click.group()
@click.version_option()
def main() -> None:
    """Multi-AI consensus code reviewer."""


@main.command()
@click.argument("ref", required=False)
@click.option("--config", "config_path", default="reviewr.toml",
              help="Path to reviewr.toml.")
@click.option("--repo", "repo_dir", default=None,
              help="Existing local checkout to review against (PR head is "
                   "inspected via a throwaway worktree; your branch is untouched).")
@click.option("--clone-dir", default=DEFAULT_CLONE_DIR,
              help="Where to cache auto-clones when no --repo is given.")
@click.option("--base", help="Local mode: base ref for `git diff base...head`.")
@click.option("--head", default="HEAD", help="Local mode: head ref.")
@click.option("--diff-file", help="Review a diff from a file instead of a PR.")
@click.option("--run-dir", help="Artifact dir (default ./.reviewr/runs/<ts>).")
def review(ref, config_path, repo_dir, clone_dir, base, head, diff_file, run_dir):
    """Review a PR or a local diff.

    REF may be a PR URL (https://github.com/owner/repo/pull/123), an
    owner/repo#123 spec, or a bare PR number (requires --repo).

    \b
    Examples:
      reviewr review https://github.com/powdr-labs/powdr/pull/123
      reviewr review 123 --repo ~/src/powdr
      reviewr review --base main --head HEAD --repo ~/src/powdr
    """
    config = load_config(config_path)

    # Resolve the review target into a prepared working tree.
    if diff_file:
        prepared = pr_mod.from_diff_file(diff_file, repo_dir or ".")
    elif base:
        prepared = pr_mod.from_git(base, head, repo_dir or ".")
    elif ref:
        pref = pr_mod.parse_pr_ref(ref)
        if repo_dir:
            prepared = pr_mod.prepare_from_local(repo_dir, pref.number, log=click.echo)
        elif pref.slug:
            prepared = pr_mod.prepare_from_clone(
                pref, clone_dir, pref.number, log=click.echo
            )
        else:
            raise click.UsageError(
                "a bare PR number needs --repo; pass a full PR URL to auto-clone"
            )
    else:
        raise click.UsageError("provide a PR REF, or --base, or --diff-file")

    pr = prepared.pr

    # Artifacts live in cwd (stable), never inside an ephemeral worktree/clone.
    if run_dir:
        rdir = Path(run_dir)
    else:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        rdir = Path.cwd() / ".reviewr" / "runs" / ts

    click.echo(f"Reviewing: {pr.short()}")
    click.echo(f"Inspecting code in: {pr.repo_dir}")
    click.echo(f"Reviewers: {', '.join(r.name for r in config.reviewers)}")
    click.echo(f"Artifacts: {rdir}\n")

    try:
        orch = Orchestrator(config, pr, rdir, log=click.echo)
        review_md = asyncio.run(orch.run())
    finally:
        prepared.cleanup()

    click.echo("\n" + "=" * 60)
    click.echo(review_md)


if __name__ == "__main__":
    main()
