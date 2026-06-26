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
```

Artifacts land in `./.reviewr/runs/<timestamp>/` (per-round outputs, raw CLI
logs, `state-*.json`, `REVIEW.md`).

Reviewers run against a working tree resolved by `reviewr/pr.py`:
- `--repo` + PR number → an ephemeral `git worktree` at the PR head (the user's
  branch is never mutated; cleaned up after the run).
- PR URL, no `--repo` → a cached clone under `~/.cache/reviewr/clones`, checked
  out at the PR head.

## Architecture (where things live)

| Concern | File |
| --- | --- |
| Load `reviewr.toml` | `reviewr/config.py` |
| Fetch PR / diff | `reviewr/pr.py` |
| Finding/Vote/Output schema | `reviewr/findings.py` |
| Invoke a CLI reviewer, parse output | `reviewr/backends.py` |
| Shared anonymized state, voting, convergence | `reviewr/state.py` |
| The round loop | `reviewr/orchestrator.py` |
| Final `REVIEW.md` | `reviewr/composer.py` |
| CLI entry point | `reviewr/cli.py` |
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
  just the diff. Don't reduce them to diff-only.
- A reviewer failing a round must not crash the run — `run_reviewer` returns a
  `RunResult` with `error` set; the loop continues.

## Adding a reviewer

Add a `[[reviewers]]` block to `reviewr.toml`. Pick `result_from`:
`claude_json` (Claude `--output-format json` envelope), `last_message_file`
(CLI writes final message to a file via `last_message_arg`), or `stdout_json`
(scan stdout). No code change should be needed.

## Style

Python 3.11+, type hints, `from __future__ import annotations`, dataclasses for
config/state, pydantic for the wire schema. Match the surrounding code.
