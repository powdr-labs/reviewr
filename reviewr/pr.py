"""Resolve the thing under review into a working tree the reviewers can explore.

Two PR entry points:
  - local checkout + PR number -> we add a throwaway git *worktree* at the PR
    head, so your checked-out branch is never touched.
  - PR URL with no local checkout -> we clone the repo (cached) and check out
    the PR head for inspection.

Plus the non-PR modes: a local `git diff` range, or a raw diff file.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass
class PRContext:
    title: str
    description: str
    diff: str
    base: str
    head: str
    repo_dir: str  # reviewers run here so they can explore surrounding code
    number: int | None = None
    owner: str | None = None
    repo: str | None = None
    url: str | None = None
    head_sha: str | None = None  # the commit actually checked out / reviewed

    # Re-review context (transient; not persisted as PR identity):
    prior_findings: list[dict] | None = None  # findings from the previous review
    delta_diff: str | None = None             # diff of the author's new commits

    def short(self) -> str:
        return f"{self.title} ({self.base}...{self.head})"

    @property
    def slug(self) -> str | None:
        return f"{self.owner}/{self.repo}" if self.owner and self.repo else None


@dataclass
class PreparedPR:
    """A ready-to-review context plus a cleanup hook (worktree/temp removal)."""

    pr: PRContext
    cleanup: Callable[[], None]


def _run(cmd: list[str], cwd: str | None = None) -> str:
    res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=True)
    return res.stdout


def _noop() -> None:
    pass


# --- PR reference parsing ------------------------------------------------

@dataclass
class PRRef:
    owner: str | None
    repo: str | None
    number: int

    @property
    def slug(self) -> str | None:
        return f"{self.owner}/{self.repo}" if self.owner and self.repo else None


def parse_pr_ref(ref: str) -> PRRef:
    """Accept a full URL, `owner/repo#123`, or a bare `123`."""
    ref = ref.strip()
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)", ref)
    if m:
        return PRRef(m.group(1), m.group(2), int(m.group(3)))
    m = re.match(r"([^/\s]+)/([^/#\s]+)#(\d+)$", ref)
    if m:
        return PRRef(m.group(1), m.group(2), int(m.group(3)))
    if re.match(r"\d+$", ref):
        return PRRef(None, None, int(ref))
    raise ValueError(
        f"unrecognized PR reference: {ref!r} "
        "(use a PR URL, owner/repo#123, or a bare number with --repo)"
    )


# --- building a PRContext from GitHub ------------------------------------

def _github_context(number: int, repo_dir: str, gh_repo: str | None) -> PRContext:
    base = ["gh", "pr"]
    repo_args = ["-R", gh_repo] if gh_repo else []
    diff = _run(base + ["diff", str(number), *repo_args], cwd=repo_dir)
    meta = json.loads(
        _run(
            base
            + ["view", str(number), *repo_args,
               "--json", "title,body,baseRefName,headRefName,url,number"],
            cwd=repo_dir,
        )
    )
    url = meta.get("url", "") or ""
    owner = repo = None
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+)/pull/\d+", url)
    if m:
        owner, repo = m.group(1), m.group(2)
    return PRContext(
        title=meta.get("title", f"PR #{number}"),
        description=meta.get("body") or "",
        diff=diff,
        base=meta.get("baseRefName", ""),
        head=meta.get("headRefName", ""),
        repo_dir=repo_dir,
        number=meta.get("number", number),
        owner=owner,
        repo=repo,
        url=url,
    )


# --- the two PR preparation strategies -----------------------------------

def _link_artifacts(repo_dir: str, wt: str, link: list[str], log=print) -> None:
    """Symlink cached build-artifact dirs from the checkout into the worktree.

    Lets reviewers reuse compiled output (e.g. Lean/MathLib `.lake`, Rust
    `target`) instead of recompiling from scratch in the fresh worktree. Writes
    go to the shared target dir — acceptable for incremental build caches.
    """
    for rel in link:
        rel = os.path.normpath(rel)
        if rel.startswith("..") or os.path.isabs(rel):
            log(f"  skip link {rel!r}: must be a path inside the repo")
            continue
        src = Path(repo_dir) / rel
        dst = Path(wt) / rel
        if not src.exists():
            continue
        if dst.exists() or dst.is_symlink():
            continue  # tracked in the PR or already linked — leave it
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.symlink_to(src, target_is_directory=src.is_dir())
        log(f"  linked {rel} -> {src}")


def prepare_from_local(
    repo_dir: str, number: int, link: list[str] | None = None, log=print
) -> PreparedPR:
    """Use an existing checkout; inspect the PR via an ephemeral worktree."""
    repo_dir = str(Path(repo_dir).resolve())
    if not (Path(repo_dir) / ".git").exists():
        raise ValueError(f"--repo is not a git checkout: {repo_dir}")

    pr = _github_context(number, repo_dir, gh_repo=None)

    log(f"Fetching PR #{number} head into a throwaway worktree…")
    _run(["git", "fetch", "origin", f"pull/{number}/head"], cwd=repo_dir)
    pr.head_sha = _run(["git", "rev-parse", "FETCH_HEAD"], cwd=repo_dir).strip()
    wt = tempfile.mkdtemp(prefix=f"reviewr-pr{number}-")
    _run(["git", "worktree", "add", "--detach", wt, "FETCH_HEAD"], cwd=repo_dir)
    pr.repo_dir = wt

    if link:
        _link_artifacts(repo_dir, wt, link, log=log)

    def cleanup() -> None:
        subprocess.run(
            ["git", "worktree", "remove", "--force", wt],
            cwd=repo_dir, capture_output=True, text=True,
        )
        shutil.rmtree(wt, ignore_errors=True)

    return PreparedPR(pr=pr, cleanup=cleanup)


def prepare_from_clone(
    pref: PRRef, clone_base: str, number: int, log=print
) -> PreparedPR:
    """No local checkout: clone the repo (cached) and check out the PR head."""
    if not pref.slug:
        raise ValueError("a bare PR number needs --repo; pass a full PR URL to clone")

    base = Path(clone_base).expanduser()
    base.mkdir(parents=True, exist_ok=True)
    dest = base / f"{pref.owner}__{pref.repo}"

    if (dest / ".git").exists():
        log(f"Reusing cached clone {dest} (fetching…)")
        _run(["git", "fetch", "--all", "--prune"], cwd=str(dest))
    else:
        log(f"Cloning {pref.slug} into {dest}…")
        _run(["gh", "repo", "clone", pref.slug, str(dest)])

    _run(["git", "fetch", "origin", f"pull/{number}/head"], cwd=str(dest))
    _run(["git", "checkout", "--force", "--detach", "FETCH_HEAD"], cwd=str(dest))

    pr = _github_context(number, str(dest), gh_repo=pref.slug)
    pr.repo_dir = str(dest)
    pr.head_sha = _run(["git", "rev-parse", "HEAD"], cwd=str(dest)).strip()
    # Keep the clone cached for next time.
    return PreparedPR(pr=pr, cleanup=_noop)


def delta_diff(repo_dir: str, prior_sha: str, new_sha: str, log=print) -> str | None:
    """Diff of the commits added between two PR-head commits (prior..new).

    Returns None if the prior commit isn't available locally (e.g. the branch
    was force-pushed), in which case the caller should re-review the full PR.
    """
    def have(sha: str) -> bool:
        return subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}"],
            cwd=repo_dir, capture_output=True, text=True,
        ).returncode == 0

    if not have(prior_sha):
        subprocess.run(
            ["git", "fetch", "origin", prior_sha],
            cwd=repo_dir, capture_output=True, text=True,
        )
    if not have(prior_sha):
        log(f"  prior commit {prior_sha[:8]} unavailable (force-push?); "
            "re-reviewing the full PR")
        return None
    return _run(["git", "diff", prior_sha, new_sha], cwd=repo_dir)


# --- non-PR modes --------------------------------------------------------

def from_git(base: str, head: str, repo_dir: str) -> PreparedPR:
    repo_dir = str(Path(repo_dir).resolve())
    diff = _run(["git", "diff", f"{base}...{head}"], cwd=repo_dir)
    pr = PRContext(
        title=f"local diff {base}...{head}",
        description="", diff=diff, base=base, head=head, repo_dir=repo_dir,
    )
    return PreparedPR(pr=pr, cleanup=_noop)


def from_diff_file(diff_path: str, repo_dir: str) -> PreparedPR:
    repo_dir = str(Path(repo_dir).resolve())
    diff = Path(diff_path).read_text()
    pr = PRContext(
        title=f"diff file {Path(diff_path).name}",
        description="", diff=diff, base="", head="", repo_dir=repo_dir,
    )
    return PreparedPR(pr=pr, cleanup=_noop)
