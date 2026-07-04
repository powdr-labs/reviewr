# AGENTS.md

Guidance for AI agents (and humans) working in this repository.

## What this is

`reviewr` is a multi-AI consensus code reviewer. Several AI CLIs (Claude,
Codex, …) review the same PR locally, debate to convergence over a shared,
**anonymized** findings list, then a single edited `REVIEW.md` is composed.
See `README.md` for the user-facing overview.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

Reviewer CLIs (`claude`, `codex`) and `gh`/`git` must be on `PATH` and
authenticated for live runs.

## Run

```bash
reviewr review https://github.com/owner/repo/pull/123   # URL -> auto-clone
reviewr review 123 --repo /path/to/repo                 # local checkout
reviewr review --base main --head HEAD --repo /path     # local diff
reviewr review --diff-file changes.diff --repo /path    # raw diff
reviewr review <url> --publish                          # post the review when done
reviewr publish --pr <url>                              # post a saved run later
reviewr fix                                             # turn the latest run into a fix PR
```

Artifacts land in `./.reviewr/runs/<owner>-<repo>-pr<N>-<timestamp>/`
(timestamp-only when the run has no PR identity): per-round outputs, raw CLI
logs, `state-*.json`, `REVIEW.md`, `run.log`, and `review.json` — the
machine-readable bundle that `publish` and `fix` consume; both select a run by
`--pr` via `publish.find_run_for_pr`.

Reviewers run against a working tree resolved by `reviewr/pr.py`:
- `--repo` + PR number → an ephemeral `git worktree` at the PR head (the user's
  branch is never mutated; cleaned up after the run). Cached build-artifact dirs
  from `[worktree].link` (`.lake`, `target`, …) are symlinked in so reviewers
  reuse compiled output instead of rebuilding (e.g. MathLib). Teardown via
  `git worktree remove --force` only unlinks the symlinks; the targets are safe.
- PR URL, no `--repo` → a cached clone under `~/.cache/reviewr/clones`, checked
  out at the PR head.

## Re-review

`review` records the reviewed commit (`pr.head_sha`) in `review.json`. On a
later `review` of the same PR, the CLI finds the prior run
(`publish.find_run_for_pr`), and if the head advanced, sets `pr.prior_findings`
+ `pr.delta_diff` (`pr.delta_diff(...)` = `prior_head..new_head`).
`prompts.render_review` then switches to `rereview.j2`. Same commit → no-op;
`--fresh` forces a full review. Everything downstream (debate, compose, publish,
fix) is unchanged — re-review just changes the round-0 framing and scope.

## Architecture (where things live)

| Concern | File |
| --- | --- |
| Load `reviewr.toml` | `reviewr/config.py` |
| Fetch PR / diff | `reviewr/pr.py` |
| Finding/Vote/Output + ComposedReview schema | `reviewr/findings.py` |
| Invoke a CLI reviewer, parse output | `reviewr/backends.py` |
| Shared anonymized state, voting, convergence | `reviewr/state.py` |
| The round loop; writes `run.log` + `review.json` | `reviewr/orchestrator.py` |
| Render prompt templates (review vs re-review, fix) | `reviewr/prompts.py` |
| Final `ComposedReview` (JSON) + `REVIEW.md` rendered from it | `reviewr/composer.py` |
| Post a run to the PR as one line-anchored review | `reviewr/publish.py` |
| Propose/reconcile patches, open a fix PR | `reviewr/fix.py` |
| CLI entry point (`review`, `publish`, `fix`) | `reviewr/cli.py` |
| Prompt templates | `prompts/*.j2` |

## Conventions / invariants — do not break these

- **Blind review is load-bearing.** The view returned by
  `ReviewState.anon_view()` must never leak which reviewer raised a finding or
  cast a vote. `origin` and voter names stay internal (`state-*.json` only).
  This is the whole point of the design — keep it anonymized.
- **Structured output.** Reviewers must emit JSON validating against
  `ReviewerOutput`. Output is constrained natively via the CLI schema flags
  (`claude --json-schema`, `codex exec --output-schema`); parsing is tolerant
  (`backends.extract_json`) as a fallback.
- **Backends are config-driven.** Adding a new reviewer CLI should be possible
  via `reviewr.toml` alone (command + `result_from` + schema/last-message
  flags), without code changes. Preserve that.
- **Reviewers run inside the target repo** so they can explore real code, not
  just the diff. Don't reduce them to diff-only. At the same time, **findings
  are scoped to the PR's changes** (enforced in `review.j2`/`critique.j2`):
  exploration is for judging the diff in context, not for reporting unrelated
  pre-existing issues. Keep both halves.
- **JSON is the source of truth for the final review.** The composer emits a
  structured `ComposedReview` (findings carry a `locations` list); `REVIEW.md`
  is rendered from it via `composer.render_markdown`, and `publish` anchors a
  comment per location from the same JSON. Don't make the Markdown
  authoritative or parse it back into data.
- A reviewer failing a round must not crash the run — `run_reviewer` returns a
  `RunResult` with `error` set; the loop continues.

## `fix` specifics

- Fix agents must edit files, so they run with `fix_command` (write
  permissions, e.g. codex `--sandbox workspace-write`), falling back to
  `command`. They produce changes by editing their own worktree; the candidate
  patch is captured via `git add -A && git diff --cached`, not structured output.
- Each proposer gets its own worktree (reused across rounds); the decider gets a
  fresh branch worktree (`-B`, so re-runs reset the branch). `run_fix` cleans up
  all worktrees if it fails partway — keep that invariant.
- The fix PR targets the reviewed PR's **head** branch (`pr.head`), pushed with
  `--force-with-lease`. Assumes a same-repo PR with write access.

## Adding a reviewer

Add a `[[reviewers]]` block to `reviewr.toml`. Pick `result_from`:
`claude_json` (Claude `--output-format json` envelope), `last_message_file`
(CLI writes final message to a file via `last_message_arg`), or `stdout_json`
(scan stdout). No code change should be needed.

## Style

Python 3.11+, type hints, `from __future__ import annotations`, dataclasses for
config/state, pydantic for the wire schema. Match the surrounding code.
