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
4. **Compose.** A designated editor AI turns the confirmed findings into one
   clean, severity-sorted `REVIEW.md` (or a deterministic render if no editor
   is configured).

## Install

```bash
pip install -e .
```

Requires the reviewer CLIs on `PATH` (`claude`, `codex`) and `gh`/`git` for PR
fetching.

## Use

```bash
# Review a GitHub PR (uses `gh` in the repo)
reviewr review 1234 --repo /path/to/repo

# Review a local branch diff
reviewr review --base main --head HEAD --repo /path/to/repo

# Review a raw diff file
reviewr review --diff-file changes.diff --repo /path/to/repo
```

Artifacts (per-round outputs, raw CLI logs, `state-*.json`, and the final
`REVIEW.md`) land in `.reviewr/runs/<timestamp>/`.

## Configure

See `reviewr.toml`. Add/remove `[[reviewers]]`, tune `max_rounds`, and pick the
`composer`. Each reviewer declares how its CLI is invoked and where its
structured output is read from (`claude_json`, `last_message_file`, or
`stdout_json`), so new CLIs can be added without code changes.

## Layout

```
reviewr/
  config.py        load reviewr.toml
  pr.py            fetch PR/diff (gh, git, file)
  findings.py      Finding / Vote / ReviewerOutput schema
  backends.py      invoke a CLI reviewer, parse structured output
  state.py         shared anonymized findings, voting, convergence
  orchestrator.py  the round loop
  composer.py      final REVIEW.md
  cli.py           `reviewr` entry point
prompts/           review.j2 / critique.j2 / compose.j2
```
