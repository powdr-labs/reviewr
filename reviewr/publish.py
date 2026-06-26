"""Publish a finished review to a GitHub PR as a single, line-anchored review.

Reads a run's `review.json` (written by the orchestrator), then posts ONE PR
review via the GitHub API (`POST .../pulls/{n}/reviews`) containing:
  - a summary body: that this is a multi-AI consensus review, which AIs took
    part, how many rounds, whether it converged, the composed review, and the
    full run log;
  - one inline comment per finding, anchored to its file/line.

Findings whose line isn't part of the PR diff can't be inline-anchored, so they
are listed in the summary body instead of being dropped.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .pr import parse_pr_ref


# --- locating the run ----------------------------------------------------

def find_latest_run(base: Path) -> Path | None:
    runs = base / ".reviewr" / "runs"
    if not runs.is_dir():
        return None
    candidates = sorted(
        (d for d in runs.iterdir() if d.is_dir()),
        key=lambda d: d.name,
        reverse=True,
    )
    for d in candidates:
        if (d / "review.json").exists() or (d / "REVIEW.md").exists():
            return d
    return None


def resolve_run_dir(arg: str | None, cwd: Path) -> Path:
    if arg is None:
        d = find_latest_run(cwd)
        if d is None:
            raise FileNotFoundError(
                "no runs found under ./.reviewr/runs — run `reviewr review` first"
            )
        return d
    p = Path(arg)
    if p.is_dir():
        return p
    if p.is_file():  # review.json or REVIEW.md
        return p.parent
    raise FileNotFoundError(f"no such run: {arg}")


# --- loading the review bundle -------------------------------------------

@dataclass
class Bundle:
    pr: dict
    reviewers: list[str]
    rounds_run: int
    converged: bool
    findings: list[dict]
    summary: str
    verdict: str
    review_markdown: str
    log: str
    run_dir: Path


def _reconstruct_bundle(run_dir: Path) -> dict:
    """Best-effort bundle for runs made before review.json existed."""
    states = sorted(run_dir.glob("state-r*.json"), key=lambda p: p.name)
    findings: list[dict] = []
    reviewers: set[str] = set()
    rounds = 0
    if states:
        rounds = max(
            int(m.group(1))
            for p in states
            if (m := re.search(r"state-r(\d+)\.json", p.name))
        )
        last = json.loads(states[-1].read_text())
        for f in last.get("findings", []):
            reviewers.add(f.get("origin", ""))
            reviewers.update(f.get("votes", {}).keys())
            if f.get("status") == "confirmed":
                fin = f["finding"]
                findings.append({
                    "title": fin.get("title", ""),
                    "severity": fin.get("severity", ""),
                    "category": fin.get("category", ""),
                    "description": fin.get("description", ""),
                    "suggested_fix": fin.get("suggested_fix"),
                    "locations": [{"file": fin.get("file"), "line": fin.get("line")}],
                })
    review_md = ""
    if (run_dir / "REVIEW.md").exists():
        review_md = (run_dir / "REVIEW.md").read_text()
    log = ""
    if (run_dir / "run.log").exists():
        log = (run_dir / "run.log").read_text()
    pr = {}
    if (run_dir / "pr.json").exists():
        pr = json.loads((run_dir / "pr.json").read_text())
    return {
        "pr": pr,
        "reviewers": sorted(r for r in reviewers if r),
        "rounds_run": rounds,
        "converged": False,
        "findings": findings,
        "review_markdown": review_md,
        "log": log,
    }


def load_bundle(run_dir: Path, pr_override: str | None) -> Bundle:
    rj = run_dir / "review.json"
    data = json.loads(rj.read_text()) if rj.exists() else _reconstruct_bundle(run_dir)

    # Findings live under the structured `review` for fresh runs; the
    # reconstruct path puts them at top level.
    review = data.get("review") or {}
    findings = review.get("findings", data.get("findings", []))
    summary = review.get("summary", "")
    verdict = review.get("verdict", "")

    pr = dict(data.get("pr") or {})
    if pr_override:
        ref = parse_pr_ref(pr_override)
        pr["number"] = ref.number
        if ref.owner and ref.repo:
            pr["owner"], pr["repo"] = ref.owner, ref.repo

    if not (pr.get("owner") and pr.get("repo") and pr.get("number")):
        raise ValueError(
            "PR identity unknown for this run; pass --pr <url> "
            "(e.g. https://github.com/owner/repo/pull/123)"
        )

    return Bundle(
        pr=pr,
        reviewers=data.get("reviewers", []),
        rounds_run=data.get("rounds_run", 0),
        converged=data.get("converged", False),
        findings=findings,
        summary=summary,
        verdict=verdict,
        review_markdown=data.get("review_markdown", ""),
        log=data.get("log", ""),
        run_dir=run_dir,
    )


# --- diff parsing: which (path, new-line) pairs are commentable ----------

def commentable_lines(diff_text: str) -> dict[str, set[int]]:
    """Map file path -> set of new-side line numbers present in the diff."""
    result: dict[str, set[int]] = {}
    path: str | None = None
    new_line = 0
    in_hunk = False
    for line in diff_text.splitlines():
        if line.startswith("diff --git"):
            in_hunk, path = False, None
            continue
        if not in_hunk:
            if line.startswith("+++ "):
                p = line[4:].strip()
                path = None if p == "/dev/null" else re.sub(r"^b/", "", p)
            elif line.startswith("@@"):
                m = re.search(r"\+(\d+)", line)
                new_line = int(m.group(1)) if m else 0
                in_hunk = True
            continue
        # inside a hunk body
        if line.startswith("@@"):  # next hunk, same file
            m = re.search(r"\+(\d+)", line)
            new_line = int(m.group(1)) if m else 0
        elif line.startswith("+"):
            result.setdefault(path, set()).add(new_line)
            new_line += 1
        elif line.startswith("-"):
            pass  # old side; doesn't advance the new-line counter
        elif line.startswith(" "):
            result.setdefault(path, set()).add(new_line)
            new_line += 1
        elif line.startswith("\\"):
            pass  # "\ No newline at end of file"
        else:  # left the hunk body (blank/unknown) — wait for next @@ or file
            in_hunk = False
    return result


# --- building and posting the review -------------------------------------

_SEV_EMOJI = {
    "critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵", "nit": "⚪",
}


def _finding_comment_body(f: dict) -> str:
    sev = f.get("severity", "")
    head = f"{_SEV_EMOJI.get(sev, '')} **{sev.upper()}: {f.get('title','')}**"
    cat = f.get("category")
    if cat:
        head += f"  _({cat})_"
    parts = [head, "", f.get("description", "")]
    if f.get("suggested_fix"):
        parts += ["", f"**Suggested fix:** {f['suggested_fix']}"]
    parts += ["", "— _reviewr multi-AI consensus_"]
    return "\n".join(parts)


def build_payload(b: Bundle, diff_text: str, event: str) -> dict:
    commentable = commentable_lines(diff_text)
    inline: list[dict] = []
    orphans: list[tuple[dict, dict]] = []  # (finding, location) not in the diff
    for f in b.findings:
        body = _finding_comment_body(f)
        for loc in f.get("locations", []):
            path, line = loc.get("file"), loc.get("line")
            if line and path in commentable and line in commentable[path]:
                inline.append(
                    {"path": path, "line": int(line), "side": "RIGHT", "body": body}
                )
            else:
                orphans.append((f, loc))

    body = _summary_body(b, n_inline=len(inline), orphans=orphans)
    return {"body": body, "event": event, "comments": inline}


def _summary_body(b: Bundle, n_inline: int, orphans: list[tuple[dict, dict]]) -> str:
    """The main review body. Kept succinct: the per-finding detail lives in the
    inline comments, so we only carry the overall summary, verdict, the handful
    of findings that couldn't be anchored, and the run log."""
    ais = ", ".join(b.reviewers) or "multiple AIs"
    conv = "reached consensus" if b.converged else "stopped at the round cap"
    rounds = b.rounds_run
    lines = [
        "## 🤖 reviewr — multi-AI consensus review",
        "",
        f"Independent reviews by **{ais}**, then blind cross-review over "
        f"**{rounds} round{'s' if rounds != 1 else ''}** ({conv}). "
        f"{len(b.findings)} agreed finding(s); {n_inline} posted inline below.",
        "",
    ]
    # Overall assessment — prefer the structured summary; for old runs without
    # one, fall back to the composed markdown so the body isn't empty.
    if b.summary.strip():
        lines += [b.summary.strip(), ""]
    elif not (b.summary or b.verdict) and b.review_markdown.strip():
        lines += [b.review_markdown.strip(), ""]
    if b.verdict.strip():
        lines += [f"**Verdict:** {b.verdict.strip()}", ""]

    if orphans:
        lines += ["### Findings not on changed lines",
                  "_(couldn't be anchored to the diff)_", ""]
        for f, loc in orphans:
            where = loc.get("file", "")
            if loc.get("line"):
                where += f":{loc['line']}"
            lines.append(
                f"- **{f.get('severity','').upper()}** `{where}` — "
                f"{f.get('title','')}: {f.get('description','')}"
            )
        lines.append("")
    if b.log:
        lines += ["<details><summary>Run log</summary>", "",
                  "```", b.log.strip(), "```", "", "</details>"]
    return "\n".join(lines)


def post_review(b: Bundle, payload: dict) -> dict:
    endpoint = f"repos/{b.pr['owner']}/{b.pr['repo']}/pulls/{b.pr['number']}/reviews"
    proc = subprocess.run(
        ["gh", "api", "--method", "POST", endpoint, "--input", "-"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gh api failed:\n{proc.stderr.strip()}")
    return json.loads(proc.stdout)
