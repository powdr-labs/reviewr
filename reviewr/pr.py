"""Fetch the thing under review: a GitHub PR, or a local git diff."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class PRContext:
    title: str
    description: str
    diff: str
    base: str
    head: str
    repo_dir: str  # reviewers run here so they can explore surrounding code

    def short(self) -> str:
        return f"{self.title} ({self.base}...{self.head})"


def _run(cmd: list[str], cwd: str) -> str:
    res = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, check=True
    )
    return res.stdout


def from_github(ref: str, repo_dir: str) -> PRContext:
    """ref is a PR number, URL, or branch understood by `gh pr`."""
    diff = _run(["gh", "pr", "diff", ref], repo_dir)
    meta = json.loads(
        _run(
            [
                "gh", "pr", "view", ref,
                "--json", "title,body,baseRefName,headRefName",
            ],
            repo_dir,
        )
    )
    return PRContext(
        title=meta.get("title", ref),
        description=meta.get("body") or "",
        diff=diff,
        base=meta.get("baseRefName", ""),
        head=meta.get("headRefName", ""),
        repo_dir=repo_dir,
    )


def from_git(base: str, head: str, repo_dir: str) -> PRContext:
    diff = _run(["git", "diff", f"{base}...{head}"], repo_dir)
    return PRContext(
        title=f"local diff {base}...{head}",
        description="",
        diff=diff,
        base=base,
        head=head,
        repo_dir=repo_dir,
    )


def from_diff_file(diff_path: str, repo_dir: str) -> PRContext:
    diff = Path(diff_path).read_text()
    return PRContext(
        title=f"diff file {Path(diff_path).name}",
        description="",
        diff=diff,
        base="",
        head="",
        repo_dir=repo_dir,
    )
