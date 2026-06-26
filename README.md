# reviewr

Multi-AI consensus code reviewer. Several AI CLIs (Claude, Codex, …) review the
same PR locally, then **debate to convergence** over a shared, *anonymized*
findings list before a single composed `REVIEW.md` is produced.

## How it works

1. **Round 0 — independent review.** Every configured reviewer reviews the PR
   in parallel, running inside the repo so it can explore surrounding code, not
   just the diff. Each emits a structured list of findings (JSON Schema –
   enforced output).
2. **Rounds 1..N — blind debate.** All findings are merged into a shared list
   and shown back to every reviewer **with the source AI stripped** (and votes
   anonymized). Each reviewer votes `agree`/`dispute`/`unsure` on every finding
   and adds anything missed. Anonymity is deliberate — reviewers judge on
   merit, not on which model said it.
3. **Convergence.** A finding is `confirmed` (no disputes), `rejected`
   (disputes ≥ agrees), or `contested`. The loop stops when a round adds no new
   findings, nothing is contested, and the status set is stable — or at
   `max_rounds`.
4. **Compose.** A designated editor AI turns the confirmed findings into a
   structured **`ComposedReview` (JSON)** — the authoritative final artifact —
   merging duplicates so that one underlying issue spanning several files
   becomes a single finding with a `locations: [{file, line}, …]` list. The
   human-readable, severity-sorted `REVIEW.md` is then *rendered
   deterministically from that JSON*, so the two never disagree. (Falls back to
   a deterministic compose if no editor is configured.)

## Prerequisites

- Python 3.11+
- The reviewer CLIs on `PATH`, **authenticated**: `claude` (run it once to log
  in) and `codex` (`codex login`).
- `gh` (authenticated: `gh auth login`) and `git`, for fetching PRs.

## Install

Install it as a global command so `reviewr` works from any directory:

```bash
# with uv (recommended) — editable, so code edits apply without reinstalling
uv tool install -e .

# or with pipx
pipx install -e .
```

Both put `reviewr` on your `PATH` (e.g. `~/.local/bin`). Verify with
`reviewr --version`. To upgrade after pulling changes, re-run the install
command (not needed for editable installs unless dependencies changed). Remove
with `uv tool uninstall reviewr` / `pipx uninstall reviewr`.

For local development without a global install:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

## Use

Run `reviewr` from anywhere. You either point it at a local checkout, or hand
it a PR URL and it clones the repo itself for inspection.

```bash
# Just a PR URL — reviewr clones the repo (cached) and checks out the PR head
reviewr review https://github.com/powdr-labs/powdr/pull/123

# You already have a checkout — pass it + the PR number. Your branch is NOT
# touched: the PR head is inspected via a throwaway git worktree.
reviewr review 123 --repo ~/src/powdr
reviewr review powdr-labs/powdr#123 --repo ~/src/powdr

# Local branch diff (no PR)
reviewr review --base main --head HEAD --repo ~/src/powdr

# Raw diff file
reviewr review --diff-file changes.diff --repo ~/src/powdr

# Review and post it to the PR in one go
reviewr review https://github.com/powdr-labs/powdr/pull/123 --publish
```

Add `--publish` to post the review to the PR as soon as the run finishes (same
as running `reviewr publish` afterwards); `--event` sets the review event
(default `COMMENT`). It's skipped for non-PR reviews (local diff / diff file).

A `REF` may be a PR URL, an `owner/repo#123` spec, or a bare number (bare
number requires `--repo` to know which repo). Auto-clones are cached under
`~/.cache/reviewr/clones` (override with `--clone-dir`), so re-runs just fetch.

Reviewers may build the project to verify findings. The throwaway worktree
starts with no build artifacts, so in `--repo` mode the cached artifact dirs
listed in `[worktree].link` (`.lake`, `target`, …) are **symlinked** from your
checkout into the worktree — otherwise a `lake build` would recompile MathLib
from scratch every run. Builds in the worktree therefore write through to your
checkout's caches (fine for incremental build output). Auto-clone mode doesn't
need this: the clone is cached and keeps its own artifacts.

Artifacts land in `./.reviewr/runs/<owner>-<repo>-pr<N>-<timestamp>/` — named by
PR so parallel runs are easy to tell apart — in your current directory, never
inside the ephemeral worktree/clone. (`publish`/`fix` can find the right one by
`--pr`, so you rarely need the path.) Each run writes: per-round reviewer
outputs and raw CLI logs, `state-*.json` (the evolving findings + votes),
`pr.diff`, the final `REVIEW.md` (rendered from JSON), `run.log` (the on-screen
log), and `review.json` (the machine-readable bundle — PR identity, run
metadata, and the structured `review` whose findings carry multiple
`locations` — used by `reviewr publish`). Point `--config` at your
`reviewr.toml` if it isn't in the cwd.

## Publish

Post a finished review back to its GitHub PR as a single **review** (not a pile
of standalone comments): a summary body plus one inline comment per finding,
anchored to the line it concerns.

```bash
reviewr publish --pr <url>           # pick the run for this PR automatically
reviewr publish                      # else the most recent run under ./.reviewr/runs
reviewr publish .reviewr/runs/<dir>  # or a specific run dir/review.json/REVIEW.md
reviewr publish --dry-run            # print what would be posted, post nothing
reviewr publish --event REQUEST_CHANGES   # default is COMMENT
```

With many runs in flight, pass `--pr` (URL, `owner/repo#123`, or a bare number)
and reviewr selects the matching run by its recorded PR identity (newest if
several) — no need to remember which directory is which.

The summary body records that this was a multi-AI consensus review, which AIs
took part, how many rounds it ran, whether it converged, the composed review,
and the full run log (collapsed). Findings whose line isn't part of the PR diff
can't be anchored inline, so they're listed in the body instead of dropped.

If the run predates `review.json` or didn't record the PR, supply it with
`--pr` (a PR URL, `owner/repo#123`, or a bare number). Posting uses `gh`, so
`gh auth` must have write access to the repo.

## Fix

Turn an agreed review into a fix PR. The same AIs each get their **own writable
worktree** at the PR head and edit the files to fix the findings; we capture
each one's diff as a candidate patch. They debate (each revises after seeing
the others' anonymized patches) until the patches stabilise, then a **decider**
(claude by default) reconciles them into the definitive fix. That fix is
committed to a branch and opened as a PR **targeting the reviewed PR's head
branch**.

```bash
reviewr fix                 # fix the latest run (runs the AIs, then opens the PR)
reviewr fix --dry-run       # produce the fix and print the patch; don't push/open
reviewr fix .reviewr/runs/<ts> --repo ~/src/powdr   # branch from a local checkout
reviewr fix --from-saved    # skip the AIs; push the already-computed fix/final.patch
```

A normal `reviewr fix` runs the whole AI pipeline and then opens the PR. If you
already produced a fix (e.g. via `--dry-run`) and just want to ship it, add
`--from-saved`: it applies that run's `fix/final.patch` onto the branch and
opens the PR without re-running the AIs.

Like `review`, it uses your local checkout (`--repo`) or clones the repo
(cached under `--clone-dir`). The fix branch is `reviewr/fix-pr<N>` (configurable
via `[fix].branch_prefix`); it's pushed with `--force-with-lease`, so re-running
updates the same branch. Opening the PR uses `gh`, which needs **write access**
to the repo (this assumes a same-repo PR, not a fork).

Configure under `[fix]`: `decider` (who reconciles), `max_rounds` (propose/revise
rounds before the decider), `branch_prefix`. Each reviewer needs a `fix_command`
with write permissions (e.g. codex `--sandbox workspace-write`); it falls back
to `command` if unset.

> Note: fix agents run in parallel in separate worktrees that **share** the
> symlinked build caches (`[worktree].link`). If two agents kick off a heavy
> build at once they write the same cache concurrently — usually fine, but the
> main thing to watch.

## Configure

Everything lives in `reviewr.toml`. Add/remove `[[reviewers]]`, tune
`[consensus]`, and pick the `[composer]`. New CLIs can be added without code
changes — a reviewer just declares how its CLI is invoked and how its output is
read back.

Per-reviewer fields:

| Field | Meaning |
| --- | --- |
| `name` | Reviewer id (used by `[composer]` and in artifacts). |
| `command` | Argv to launch the CLI headlessly. |
| `prompt_via` | `"stdin"` (default) or `"arg"` — how the prompt is delivered. |
| `result_from` | Where to read structured output: `claude_json` (parse a Claude `--output-format json` envelope), `last_message_file` (read the file written via `last_message_arg`, e.g. codex `-o`), or `stdout_json` (scan stdout). |
| `schema_arg` | Flag to pass the JSON Schema, e.g. `--json-schema` (claude) or `--output-schema` (codex). Omit to rely on the prompt + tolerant parsing. |
| `schema_as` | `"inline"` passes the schema JSON as the flag value (claude); `"file"` (default) passes a path (codex). |
| `last_message_arg` | Flag the CLI uses to write its final message to a file (codex `-o`). Required for `result_from = "last_message_file"`. |
| `timeout` | Seconds per reviewer per round. |

`[consensus]`: `max_rounds` (cap on debate rounds) and `drop_rejected` (stop
carrying findings the panel votes down). `[composer]`: `reviewer` names who
edits the final `REVIEW.md`; omit it for a deterministic, no-AI render.
`[worktree].link`: artifact dirs (relative to the repo root) to symlink from
your checkout into the worktree so reviewers reuse compiled output instead of
rebuilding (see Use, above).

The schema handed to the CLIs is generated strict (every object gets
`additionalProperties: false` and all keys `required`) so it satisfies OpenAI /
Codex structured-output rules.

## Layout

```
reviewr/
  config.py        load reviewr.toml
  pr.py            fetch PR/diff (gh, git, file)
  findings.py      Finding / Vote / ReviewerOutput schema
  backends.py      invoke a CLI reviewer, parse structured output
  state.py         shared anonymized findings, voting, convergence
  orchestrator.py  the round loop; writes run.log + review.json
  composer.py      final REVIEW.md
  publish.py       post a run to the PR as one line-anchored review
  fix.py           AIs propose/reconcile patches -> open a fix PR
  cli.py           `reviewr` entry point (review, publish, fix)
prompts/           review.j2 / critique.j2 / compose.j2
```
