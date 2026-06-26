"""Invoke an AI reviewer as a headless CLI subprocess and parse its output."""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .config import ReviewerConfig
from .findings import ReviewerOutput


@dataclass
class RunResult:
    name: str
    output: ReviewerOutput | None  # None if the reviewer failed this round
    raw: str
    error: str | None = None
    returncode: int = 0


def extract_json(text: str) -> dict | None:
    """Best-effort: pull the first balanced JSON object out of CLI output.

    Handles raw JSON, ```json fenced blocks, and JSON embedded in chatter.
    """
    if not text:
        return None
    text = text.strip()

    # Strip a leading/trailing markdown fence if present.
    fence = re.match(r"^```[a-zA-Z0-9]*\n(.*)\n```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass

    # Scan for the first balanced {...}, respecting strings/escapes.
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = text[start : i + 1]
                        try:
                            obj = json.loads(candidate)
                            if isinstance(obj, dict):
                                return obj
                        except json.JSONDecodeError:
                            break  # try next "{"
        start = text.find("{", start + 1)
    return None


def _result_text(rv: ReviewerConfig, stdout: str, last_message_file: Path | None) -> str:
    if rv.result_from == "last_message_file":
        if last_message_file and last_message_file.exists():
            return last_message_file.read_text()
        return ""
    if rv.result_from == "claude_json":
        try:
            env = json.loads(stdout)
            # `--output-format json` wraps the agent's answer in `.result`.
            return env.get("result", "") if isinstance(env, dict) else stdout
        except json.JSONDecodeError:
            return stdout
    return stdout  # "stdout_json"


async def run_reviewer(
    rv: ReviewerConfig,
    prompt: str,
    *,
    schema_path: Path | None,
    repo_dir: str,
    artifact_dir: Path,
    tag: str,
) -> RunResult:
    """Run one reviewer once. Never raises: failures become RunResult(error=...)."""
    argv = list(rv.command)

    if schema_path and rv.schema_arg:
        argv += [rv.schema_arg, str(schema_path)]

    last_message_file: Path | None = None
    if rv.result_from == "last_message_file" and rv.last_message_arg:
        last_message_file = artifact_dir / f".{rv.name}-{tag}.lastmsg.json"
        argv += [rv.last_message_arg, str(last_message_file)]

    stdin_data: bytes | None = None
    if rv.prompt_via == "stdin":
        stdin_data = prompt.encode()
    else:
        argv.append(prompt)

    env = {**os.environ, **rv.env}

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin_data is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=repo_dir,
            env=env,
        )
        out_b, err_b = await asyncio.wait_for(
            proc.communicate(stdin_data), timeout=rv.timeout
        )
    except asyncio.TimeoutError:
        return RunResult(rv.name, None, "", error=f"timeout after {rv.timeout}s")
    except FileNotFoundError as e:
        return RunResult(rv.name, None, "", error=f"command not found: {e}")

    stdout = out_b.decode(errors="replace")
    stderr = err_b.decode(errors="replace")

    text = _result_text(rv, stdout, last_message_file)
    # Persist raw output for debugging / provenance.
    (artifact_dir / f"{rv.name}-{tag}.raw.txt").write_text(
        f"$ {' '.join(argv)}\n\n=== stdout ===\n{stdout}\n\n=== stderr ===\n{stderr}"
    )

    data = extract_json(text)
    if data is None:
        return RunResult(
            rv.name, None, text,
            error="no JSON found in output", returncode=proc.returncode,
        )
    try:
        output = ReviewerOutput.model_validate(data)
    except Exception as e:  # validation error
        return RunResult(
            rv.name, None, text,
            error=f"schema validation failed: {e}", returncode=proc.returncode,
        )

    return RunResult(rv.name, output, text, returncode=proc.returncode)
