"""The consensus loop: review -> share -> debate -> converge -> compose."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from .backends import RunResult, run_reviewer
from .composer import compose
from .config import Config
from .findings import output_json_schema
from .pr import PRContext
from .prompts import render_critique, render_review
from .state import ReviewState


class Orchestrator:
    def __init__(self, config: Config, pr: PRContext, run_dir: Path, log=print):
        self.config = config
        self.pr = pr
        self.run_dir = run_dir
        self.state = ReviewState()
        self.log = log
        self.schema_path = run_dir / "schema.json"

    def _setup(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.schema_path.write_text(json.dumps(output_json_schema(), indent=2))
        (self.run_dir / "pr.diff").write_text(self.pr.diff)
        (self.run_dir / "pr.json").write_text(
            json.dumps(
                {
                    "title": self.pr.title,
                    "base": self.pr.base,
                    "head": self.pr.head,
                    "description": self.pr.description,
                },
                indent=2,
            )
        )

    async def _run_round(self, prompt_for, tag: str) -> list[RunResult]:
        async def one(rv):
            return await run_reviewer(
                rv,
                prompt_for(rv),
                schema_path=self.schema_path,
                repo_dir=self.pr.repo_dir,
                artifact_dir=self.run_dir,
                tag=tag,
            )

        results = await asyncio.gather(*(one(rv) for rv in self.config.reviewers))
        for r in results:
            if r.error:
                self.log(f"  ! {r.name}: {r.error}")
        return results

    def _save_state(self, label: str) -> None:
        (self.run_dir / f"state-{label}.json").write_text(
            json.dumps(self.state.to_dict(), indent=2)
        )

    async def run(self) -> str:
        self._setup()
        cfg = self.config.consensus
        drop = cfg.drop_rejected

        # --- Round 0: independent review --------------------------------
        self.log("Round 0: independent review")
        review_prompt = render_review(self.pr)
        results = await self._run_round(lambda rv: review_prompt, "r0")
        for r in results:
            if r.output:
                ids = self.state.add_findings(r.name, r.output.new_findings, 0)
                self.log(f"  {r.name}: {len(ids)} findings")
        self._save_state("r0")

        # --- Rounds 1..N: blind debate ----------------------------------
        prev_snapshot = None
        for rnd in range(1, cfg.max_rounds + 1):
            n_before = len(self.state.findings)
            view = self.state.anon_view(drop)
            self.log(f"Round {rnd}: debate ({len(view)} shared findings)")

            def prompt_for(rv, view=view, rnd=rnd):
                return render_critique(self.pr, view, rnd)

            results = await self._run_round(prompt_for, f"r{rnd}")
            for r in results:
                if r.output:
                    self.state.apply_votes(r.name, r.output.votes)
                    self.state.add_findings(r.name, r.output.new_findings, rnd)
            self._save_state(f"r{rnd}")

            new_count = len(self.state.findings) - n_before
            contested = self.state.has_contested(drop)
            snapshot = self.state.status_snapshot(drop)
            self.log(
                f"  +{new_count} new · contested={contested} · "
                f"stable={snapshot == prev_snapshot}"
            )

            if new_count == 0 and not contested and snapshot == prev_snapshot:
                self.log(f"Converged after round {rnd}.")
                break
            prev_snapshot = snapshot
        else:
            self.log(f"Reached max_rounds ({cfg.max_rounds}) without full convergence.")

        # --- Compose final review ---------------------------------------
        self.log("Composing final review")
        review_md = await compose(self.pr, self.state, self.config, self.run_dir)
        out_path = self.run_dir / "REVIEW.md"
        out_path.write_text(review_md)
        self.log(f"Wrote {out_path}")
        return review_md
