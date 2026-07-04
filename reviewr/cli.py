"""Command-line interface: `reviewr ...`."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from pathlib import Path

import click

from .config import load_config
from .orchestrator import Orchestrator
from . import pr as pr_mod
from . import publish as publish_mod
from . import fix as fix_mod

DEFAULT_CLONE_DIR = "~/.cache/reviewr/clones"


@click.group()
@click.version_option()
def main() -> None:
    """Multi-AI consensus code reviewer."""


def _pr_slug(pr) -> str | None:
    """A filesystem-safe run-dir prefix identifying the PR, e.g.
    'powdr-labs-evm-semantics-pr27'."""
    if pr.owner and pr.repo and pr.number:
        return re.sub(r"[^A-Za-z0-9._-]", "_",
                      f"{pr.owner}-{pr.repo}-pr{pr.number}")
    return None


def _run_dir_for(pr, run_dir: str | None) -> Path:
    if run_dir:
        return Path(run_dir)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = _pr_slug(pr)
    return Path.cwd() / ".reviewr" / "runs" / (f"{slug}-{ts}" if slug else ts)


def _publish_run(rdir: Path, pr, event: str) -> None:
    if not (pr.owner and pr.repo and pr.number):
        click.echo("\n--publish skipped: this run isn't a GitHub PR review.")
        return
    click.echo("\nPublishing review to the PR…")
    bundle = publish_mod.load_bundle(rdir, None)
    diff_text = (rdir / "pr.diff").read_text()
    payload = publish_mod.build_payload(bundle, diff_text, event)
    result = publish_mod.post_review(bundle, payload)
    click.echo(f"Posted review: {result.get('html_url', '(submitted)')}")


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
@click.option("--run-dir",
              help="Artifact dir (default ./.reviewr/runs/<owner>-<repo>-prN-<ts>).")
@click.option("--publish", "publish_after", is_flag=True,
              help="Post the review to the PR as soon as the run finishes.")
@click.option("--event", "publish_event",
              type=click.Choice(["COMMENT", "APPROVE", "REQUEST_CHANGES"]),
              default="COMMENT", help="Review event when --publish (default COMMENT).")
@click.option("--fresh", is_flag=True,
              help="Force a full review even if this PR was reviewed before "
                   "(skip the automatic delta re-review).")
def review(ref, config_path, repo_dir, clone_dir, base, head, diff_file, run_dir,
           publish_after, publish_event, fresh):
    """Review a PR or a local diff.

    REF may be a PR URL (https://github.com/owner/repo/pull/123), an
    owner/repo#123 spec, or a bare PR number (requires --repo).

    If this PR was already reviewed and the author pushed more commits, the
    same command automatically re-reviews only the new commits against the
    previous findings (use --fresh to force a full review instead).

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
            prepared = pr_mod.prepare_from_local(
                repo_dir, pref.number, link=config.worktree.link, log=logger
            )
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

    # Auto re-review: if this PR was reviewed before, review only the commits
    # added since, judged against the prior findings.
    if not fresh and pr.owner and pr.repo and pr.number:
        prior_dir = publish_mod.find_run_for_pr(
            Path.cwd(), f"{pr.owner}/{pr.repo}#{pr.number}"
        )
        if prior_dir:
            prior = publish_mod.load_bundle(prior_dir, None)
            prior_sha = prior.pr.get("head_sha")
            if prior_sha and pr.head_sha == prior_sha:
                logger(f"Already reviewed this commit ({prior_sha[:8]}); nothing "
                       "new. Use --fresh to review again anyway.")
                prepared.cleanup()
                return
            pr.prior_findings = prior.findings
            pr.delta_diff = (
                pr_mod.delta_diff(pr.repo_dir, prior_sha, pr.head_sha, log=logger)
                if prior_sha else None
            )
            span = (f"{prior_sha[:8]}..{(pr.head_sha or '')[:8]}"
                    if prior_sha else "the full PR (prior commit unknown)")
            logger(f"Prior review found ({prior_dir.name}); re-reviewing {span} "
                   f"against {len(prior.findings)} prior finding(s).")

    # Artifacts live in cwd (stable), never inside an ephemeral worktree/clone.
    rdir = _run_dir_for(pr, run_dir)

    mode = "Re-reviewing" if pr.prior_findings is not None else "Reviewing"
    logger(f"{mode}: {pr.short()}")
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

    if publish_after:
        _publish_run(rdir, pr, publish_event)


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
    run_dir = publish_mod.resolve_run_dir(run, Path.cwd(), pr_override)
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


@main.command()
@click.argument("run", required=False)
@click.option("--config", "config_path", default="reviewr.toml")
@click.option("--repo", "repo_dir", default=None,
              help="Local checkout to branch from (else the repo is cloned).")
@click.option("--clone-dir", default=DEFAULT_CLONE_DIR,
              help="Where to cache the clone when no --repo is given.")
@click.option("--pr", "pr_override",
              help="PR URL/owner-repo#n, if the run doesn't record it.")
@click.option("--dry-run", is_flag=True,
              help="Produce the fix but don't commit/push/open a PR.")
@click.option("--from-saved", is_flag=True,
              help="Skip the AIs; apply this run's saved fix/final.patch and "
                   "open the PR.")
def fix(run, config_path, repo_dir, clone_dir, pr_override, dry_run, from_saved):
    """Fix an agreed review: AIs propose patches, reach consensus, and a PR is
    opened targeting the reviewed PR.

    RUN is a run dir, review.json, or REVIEW.md (defaults to the latest run).
    """
    config = load_config(config_path)
    run_dir = publish_mod.resolve_run_dir(run, Path.cwd(), pr_override)
    bundle = publish_mod.load_bundle(run_dir, pr_override)
    pr = bundle.pr
    if not pr.get("head"):
        raise click.UsageError(
            "this run doesn't record the PR head branch; re-run `reviewr review` "
            "(fix opens a PR targeting that branch)"
        )
    if not bundle.findings:
        raise click.UsageError("no findings to fix in this run")

    click.echo(f"Run: {run_dir}")
    click.echo(f"Target PR: {pr['owner']}/{pr['repo']}#{pr['number']} "
               f"(base branch for the fix PR: {pr['head']})")
    click.echo(f"Findings to fix: {len(bundle.findings)}")
    if from_saved:
        click.echo("Mode: applying saved fix/final.patch (no AI run)\n")
        result = fix_mod.apply_saved(
            config, pr, run_dir, repo_dir, clone_dir, log=click.echo
        )
    else:
        click.echo(f"Proposers: {', '.join(r.name for r in config.reviewers)}  "
                   f"Decider: {config.fix.decider}\n")
        result = asyncio.run(fix_mod.run_fix(
            config, pr, bundle.findings, run_dir, repo_dir, clone_dir, log=click.echo
        ))
    ws = result["workspace"]
    try:
        patch = result["final_patch"]
        if not patch.strip():
            click.echo("\nThe decider produced no changes — nothing to open.")
            return
        if dry_run:
            click.echo("\n--- DRY RUN: final fix patch ---\n")
            click.echo(patch)
            click.echo(f"\nWould push branch {result['branch']} and open a PR "
                       f"into {pr['head']}.")
            return
        url = fix_mod.open_fix_pr(
            pr, result, bundle.findings,
            [r.name for r in config.reviewers], config.fix.decider,
            log=click.echo,
        )
        click.echo(f"\nOpened fix PR: {url}")
    finally:
        ws.cleanup()


if __name__ == "__main__":
    main()
