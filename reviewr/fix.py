"""`reviewr fix`: turn an agreed review into a fix PR.

Flow: each reviewer AI gets its own writable worktree at the PR head and edits
the files to fix the agreed findings (we capture each one's `git diff` as a
candidate patch). A debate round lets each AI revise its patch after seeing the
others' (anonymized). Then a single decider AI (claude by default) produces the
definitive fix in a fresh branch worktree, which is committed, pushed, and
opened as a PR targeting the reviewed PR's head branch.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config, ReviewerConfig
from .pr import _link_artifacts, _run
from .prompts import render_fix, render_fix_critique, render_fix_decide


@dataclass
class Worktree:
    name: str
    path: str


def _git(args: list[str], cwd: str) -> str:
    return _run(["git", *args], cwd=cwd)


def capture_patch(wt: str) -> str:
    """Stage everything and return the worktree's diff (incl. new files)."""
    _git(["add", "-A"], cwd=wt)
    return _git(["diff", "--cached"], cwd=wt)


def _hash(patch: str) -> str:
    return hashlib.sha256(patch.encode()).hexdigest()


async def _run_agent(
    rv: ReviewerConfig, prompt: str, cwd: str, log=print
) -> tuple[int, str, str]:
    """Run an agent (edit mode) in `cwd`. Returns (rc, stdout, stderr)."""
    argv = rv.fix_argv()
    stdin_data = prompt.encode() if rv.prompt_via == "stdin" else None
    if rv.prompt_via != "stdin":
        argv = [*argv, prompt]
    env = {**os.environ, **rv.env}
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin_data is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
        )
        out_b, err_b = await asyncio.wait_for(
            proc.communicate(stdin_data), timeout=rv.timeout
        )
    except asyncio.TimeoutError:
        log(f"  ! {rv.name}: timeout after {rv.timeout}s")
        return 124, "", "timeout"
    except FileNotFoundError as e:
        log(f"  ! {rv.name}: command not found: {e}")
        return 127, "", str(e)
    return proc.returncode, out_b.decode(errors="replace"), err_b.decode(errors="replace")


# --- preparing the base repo + worktrees ---------------------------------

@dataclass
class FixWorkspace:
    repo_dir: str
    head_sha: str
    worktrees: list[str] = field(default_factory=list)

    def cleanup(self) -> None:
        for wt in self.worktrees:
            subprocess.run(
                ["git", "worktree", "remove", "--force", wt],
                cwd=self.repo_dir, capture_output=True, text=True,
            )
            shutil.rmtree(wt, ignore_errors=True)


def prepare_workspace(
    pr: dict, repo_dir: str | None, clone_dir: str, log=print
) -> FixWorkspace:
    """Get a git repo to branch from and fetch the PR head."""
    if repo_dir:
        repo_dir = str(Path(repo_dir).resolve())
        if not (Path(repo_dir) / ".git").exists():
            raise ValueError(f"--repo is not a git checkout: {repo_dir}")
    else:
        slug = f"{pr['owner']}/{pr['repo']}"
        base = Path(clone_dir).expanduser()
        base.mkdir(parents=True, exist_ok=True)
        dest = base / f"{pr['owner']}__{pr['repo']}"
        if (dest / ".git").exists():
            log(f"Reusing cached clone {dest} (fetching…)")
            _git(["fetch", "--all", "--prune"], cwd=str(dest))
        else:
            log(f"Cloning {slug} into {dest}…")
            _run(["gh", "repo", "clone", slug, str(dest)])
        repo_dir = str(dest)

    log(f"Fetching PR #{pr['number']} head…")
    _git(["fetch", "origin", f"pull/{pr['number']}/head"], cwd=repo_dir)
    head_sha = _git(["rev-parse", "FETCH_HEAD"], cwd=repo_dir).strip()
    return FixWorkspace(repo_dir=repo_dir, head_sha=head_sha)


def add_worktree(
    ws: FixWorkspace, label: str, link: list[str], branch: str | None = None, log=print
) -> str:
    wt = tempfile.mkdtemp(prefix=f"reviewr-fix-{label}-")
    if branch:
        # -B resets the branch to the PR head if a previous run left it behind.
        _git(["worktree", "add", "-B", branch, wt, ws.head_sha], cwd=ws.repo_dir)
    else:
        _git(["worktree", "add", "--detach", wt, ws.head_sha], cwd=ws.repo_dir)
    ws.worktrees.append(wt)
    if link:
        _link_artifacts(ws.repo_dir, wt, link, log=log)
    return wt


# --- the fix consensus loop ----------------------------------------------

async def run_fix(
    config: Config,
    pr: dict,
    findings: list[dict],
    run_dir: Path,
    repo_dir: str | None,
    clone_dir: str,
    log=print,
) -> dict:
    """Run the proposal/debate/decide loop. Returns a result dict with the
    final patch and the decider's branch worktree (left in place for the
    caller to commit/push)."""
    fix_dir = run_dir / "fix"
    fix_dir.mkdir(parents=True, exist_ok=True)
    link = config.worktree.link

    ws = prepare_workspace(pr, repo_dir, clone_dir, log=log)
    try:
        return await _fix_loop(config, pr, findings, fix_dir, link, ws, log)
    except BaseException:
        ws.cleanup()  # don't leak worktrees if we fail partway
        raise


async def _fix_loop(config, pr, findings, fix_dir, link, ws, log) -> dict:
    reviewers = config.reviewers

    # One reused worktree per reviewer for the proposal/debate phase.
    wts: dict[str, str] = {}
    for rv in reviewers:
        wts[rv.name] = add_worktree(ws, rv.name, link, log=log)

    patches: dict[str, str] = {}

    async def propose(rv: ReviewerConfig, prompt: str) -> None:
        rc, out, err = await _run_agent(rv, prompt, wts[rv.name], log=log)
        patch = capture_patch(wts[rv.name])
        patches[rv.name] = patch
        n = len(patch.splitlines())
        log(f"  {rv.name}: patch {n} line(s)" + ("" if patch else " (empty!)"))

    # Round 0: independent fixes.
    log("Fix round 0: proposing patches")
    fp = render_fix(findings)
    await asyncio.gather(*(propose(rv, fp) for rv in reviewers))
    _save_patches(fix_dir, patches, 0)

    # Debate rounds.
    prev_hashes: dict[str, str] = {}
    for rnd in range(1, config.fix.max_rounds):
        prev_hashes = {k: _hash(v) for k, v in patches.items()}
        others = [p for p in patches.values() if p.strip()]
        log(f"Fix round {rnd}: revising ({len(others)} candidate patches)")
        cp = render_fix_critique(findings, others)
        await asyncio.gather(*(propose(rv, cp) for rv in reviewers))
        _save_patches(fix_dir, patches, rnd)
        if {k: _hash(v) for k, v in patches.items()} == prev_hashes:
            log(f"Patches stable after round {rnd}.")
            break

    # Decider: definitive fix in a fresh branch worktree.
    branch = f"{config.fix.branch_prefix}{pr['number']}"
    decider = config.reviewer(config.fix.decider)
    if decider is None:
        raise ValueError(f"fix.decider '{config.fix.decider}' not in [[reviewers]]")
    log(f"Decider ({decider.name}): composing final fix on branch {branch}")
    decide_wt = add_worktree(ws, "decider", link, branch=branch, log=log)
    dp = render_fix_decide(findings, [p for p in patches.values() if p.strip()])
    await _run_agent(decider, dp, decide_wt, log=log)
    final_patch = capture_patch(decide_wt)
    (fix_dir / "final.patch").write_text(final_patch)
    log(f"  final fix: {len(final_patch.splitlines())} line(s)")

    return {
        "workspace": ws,
        "branch": branch,
        "decide_wt": decide_wt,
        "final_patch": final_patch,
        "candidate_patches": patches,
    }


def _save_patches(fix_dir: Path, patches: dict[str, str], rnd: int) -> None:
    for name, patch in patches.items():
        (fix_dir / f"{name}-r{rnd}.patch").write_text(patch)


# --- committing, pushing, opening the PR ---------------------------------

def open_fix_pr(
    pr: dict, result: dict, findings: list[dict], reviewers: list[str],
    decider: str, log=print,
) -> str:
    """Commit the decider's edits, push the branch, open the PR. Returns URL."""
    wt = result["decide_wt"]
    branch = result["branch"]
    slug = f"{pr['owner']}/{pr['repo']}"

    _git(["add", "-A"], cwd=wt)
    title = f"reviewr: fix review findings for #{pr['number']}"
    _git(["commit", "-m", title], cwd=wt)
    log(f"Pushing {branch} to origin…")
    _git(["push", "--force-with-lease", "-u", "origin", branch], cwd=wt)

    body = _pr_body(pr, findings, reviewers, decider)
    out = _run(
        ["gh", "pr", "create", "-R", slug,
         "--base", pr["head"], "--head", branch,
         "--title", title, "--body", body],
        cwd=wt,
    )
    return out.strip()


def _pr_body(pr: dict, findings: list[dict], reviewers: list[str], decider: str) -> str:
    ais = ", ".join(reviewers) or "the panel"
    lines = [
        f"🤖 Automated fix from **reviewr** for the consensus review of "
        f"#{pr['number']}.",
        "",
        f"Patches proposed by **{ais}**, then reconciled by **{decider}** as the "
        f"final decider.",
        "",
        "Addresses:",
    ]
    for f in findings:
        loc = ", ".join(
            l.get("file", "") + (f":{l['line']}" if l.get("line") else "")
            for l in f.get("locations", [])
        )
        lines.append(f"- {f.get('title','')} ({loc})")
    return "\n".join(lines)
