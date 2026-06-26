"""Load `reviewr.toml` into typed config objects."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ReviewerConfig:
    """How to invoke one AI reviewer as a headless CLI subprocess."""

    name: str
    command: list[str]

    # How the prompt reaches the process: piped to stdin, or appended as argv.
    prompt_via: str = "stdin"  # "stdin" | "arg"

    # Where to read the reviewer's structured result from:
    #   "claude_json"        - parse stdout as a Claude `--output-format json`
    #                          envelope and take `.result`
    #   "last_message_file"  - read the file passed via `last_message_arg`
    #   "stdout_json"        - find a JSON object anywhere in stdout
    result_from: str = "stdout_json"

    # Flag used to pass the JSON Schema file (e.g. "--json-schema",
    # "--output-schema"). Omitted -> rely on the prompt + tolerant parsing.
    schema_arg: str | None = None

    # Flag used to ask the CLI to write its final message to a file
    # (e.g. codex `-o`). Required when result_from == "last_message_file".
    last_message_arg: str | None = None

    timeout: int = 600  # seconds per reviewer per round
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class ConsensusConfig:
    max_rounds: int = 4
    # Drop findings the panel votes down instead of carrying them forever.
    drop_rejected: bool = True


@dataclass
class ComposerConfig:
    # Name of a reviewer (from [[reviewers]]) used to compose the final review.
    # If None, the final review is rendered deterministically from the state.
    reviewer: str | None = None


@dataclass
class Config:
    reviewers: list[ReviewerConfig]
    consensus: ConsensusConfig
    composer: ComposerConfig

    def reviewer(self, name: str) -> ReviewerConfig | None:
        return next((r for r in self.reviewers if r.name == name), None)


def _expand_env(value: dict[str, str]) -> dict[str, str]:
    return {k: os.path.expandvars(v) for k, v in value.items()}


def load_config(path: str | Path) -> Config:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"config not found: {path}")
    data = tomllib.loads(path.read_text())

    reviewers = []
    for r in data.get("reviewers", []):
        reviewers.append(
            ReviewerConfig(
                name=r["name"],
                command=list(r["command"]),
                prompt_via=r.get("prompt_via", "stdin"),
                result_from=r.get("result_from", "stdout_json"),
                schema_arg=r.get("schema_arg"),
                last_message_arg=r.get("last_message_arg"),
                timeout=int(r.get("timeout", 600)),
                env=_expand_env(r.get("env", {})),
            )
        )
    if not reviewers:
        raise ValueError("config has no [[reviewers]]")

    c = data.get("consensus", {})
    consensus = ConsensusConfig(
        max_rounds=int(c.get("max_rounds", 4)),
        drop_rejected=bool(c.get("drop_rejected", True)),
    )

    comp = data.get("composer", {})
    composer = ComposerConfig(reviewer=comp.get("reviewer"))

    return Config(reviewers=reviewers, consensus=consensus, composer=composer)
