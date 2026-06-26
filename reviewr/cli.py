"""Command-line interface: `reviewr ...`."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import click

from .config import load_config
from .orchestrator import Orchestrator
from . import pr as pr_mod
from . import publish as publish_mod

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

    # Tee everything printed to screen into a buffer so the run is reproducible
    # in run.log / review.json (used by `reviewr publish`).
    log_lines: list[str] = []

    def logger(msg: str = "") -> None:
        log_lines.append(str(msg))
        click.echo(msg)

    # Resolve the review target into a prepared working tree.
    if diff_file:
        prepared = pr_mod.from_diff_file(diff_file, repo_dir or ".")
    elif base:
        prepared = pr_mod.from_git(base, head, repo_dir or ".")
    elif ref:
        pref = pr_mod.parse_pr_ref(ref)
        if repo_dir:
            prepared = pr_mod.prepare_from_local(repo_dir, pref.number, log=logger)
        elif pref.slug:
            prepared = pr_mod.prepare_from_clone(
                pref, clone_dir, pref.number, log=logger
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

    logger(f"Reviewing: {pr.short()}")
    logger(f"Inspecting code in: {pr.repo_dir}")
    logger(f"Reviewers: {', '.join(r.name for r in config.reviewers)}")
    logger(f"Artifacts: {rdir}\n")

    try:
        orch = Orchestrator(config, pr, rdir, log=logger, log_lines=log_lines)
        review_md = asyncio.run(orch.run())
    finally:
        prepared.cleanup()

    click.echo("\n" + "=" * 60)
    click.echo(review_md)


@main.command()
@click.argument("run", required=False)
@click.option("--pr", "pr_override",
              help="PR URL/owner-repo#n/number, if the run doesn't record it.")
@click.option("--event", type=click.Choice(["COMMENT", "APPROVE", "REQUEST_CHANGES"]),
              default="COMMENT", help="Review event to submit (default COMMENT).")
@click.option("--dry-run", is_flag=True,
              help="Print what would be posted instead of posting.")
def publish(run, pr_override, event, dry_run) -> None:
    """Post a finished review to its GitHub PR as one line-anchored review.

    RUN is a run directory, a review.json, or a REVIEW.md. Defaults to the
    latest run under ./.reviewr/runs.
    """
    run_dir = publish_mod.resolve_run_dir(run, Path.cwd())
    bundle = publish_mod.load_bundle(run_dir, pr_override)

    diff_path = run_dir / "pr.diff"
    if diff_path.exists():
        diff_text = diff_path.read_text()
    else:
        diff_text = pr_mod._run(
            ["gh", "pr", "diff", str(bundle.pr["number"]),
             "-R", f"{bundle.pr['owner']}/{bundle.pr['repo']}"]
        )

    payload = publish_mod.build_payload(bundle, diff_text, event)
    pr = bundle.pr
    target = f"{pr['owner']}/{pr['repo']}#{pr['number']}"
    click.echo(f"Run: {run_dir}")
    click.echo(f"Target PR: {target}  ({pr.get('url','')})")
    click.echo(
        f"Findings: {len(bundle.findings)}  "
        f"inline: {len(payload['comments'])}  "
        f"event: {event}"
    )

    if dry_run:
        click.echo("\n--- DRY RUN: review body ---\n")
        click.echo(payload["body"])
        click.echo("\n--- inline comments ---")
        for c in payload["comments"]:
            click.echo(f"  {c['path']}:{c['line']} — {c['body'].splitlines()[0]}")
        return

    result = publish_mod.post_review(bundle, payload)
    click.echo(f"\nPosted review: {result.get('html_url', '(submitted)')}")


if __name__ == "__main__":
    main()
