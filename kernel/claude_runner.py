"""Subprocess wrapper for ``claude -p`` (headless Claude Code).

Every per-turn LLM call goes through ``invoke``. The wrapper:

  1. Spawns ``claude -p --output-format json`` with the user's prompt
  2. Parses the JSON envelope, extracting the response text and the
     ``usage.input_tokens`` / ``usage.output_tokens`` fields
  3. Surfaces a non-zero exit code as a ``ClaudeRunnerError``
  4. Retries transient subprocess failures via ``tenacity``

The token telemetry that this function returns is what the audit log
records per turn (``kernel/RUNTIME.md`` -> "Token telemetry"). No
``claude -p`` invocation in the kernel is allowed to skip this wrapper.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from typing import Sequence

from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_fixed,
)

__all__ = ["ClaudeResponse", "ClaudeRunnerError", "invoke"]


DEFAULT_BIN = "claude"
DEFAULT_TIMEOUT_SEC = 60
RETRY_ATTEMPTS = 3
RETRY_WAIT_SEC = 0.5


class ClaudeRunnerError(RuntimeError):
    """Raised when the subprocess fails or its output cannot be parsed."""


@dataclass(frozen=True)
class ClaudeResponse:
    """Structured wrapper for a single ``claude -p`` invocation."""

    text: str
    tokens_in: int
    tokens_out: int
    raw: dict


def _build_argv(
    prompt: str,
    *,
    binary: str,
    system_prompt: str | None,
) -> list[str]:
    """Compose the ``claude -p`` command line with JSON output enabled."""
    argv: list[str] = [binary, "-p", "--output-format", "json"]
    if system_prompt:
        argv.extend(["--system-prompt", system_prompt])
    argv.append(prompt)
    return argv


def _parse_response(raw_stdout: str) -> ClaudeResponse:
    """Decode the JSON envelope and pull out text + token usage.

    ``claude -p --output-format json`` is documented to emit one JSON
    object on stdout per invocation. Token usage lives under ``usage`` with
    Anthropic's standard ``input_tokens`` / ``output_tokens`` keys.
    """
    try:
        payload = json.loads(raw_stdout)
    except json.JSONDecodeError as err:
        raise ClaudeRunnerError(
            f"could not parse claude -p JSON envelope: {err}"
        ) from err

    text = payload.get("result") or payload.get("text") or ""
    usage = payload.get("usage") or {}
    tokens_in = int(usage.get("input_tokens", 0) or 0)
    tokens_out = int(usage.get("output_tokens", 0) or 0)

    return ClaudeResponse(
        text=str(text),
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        raw=payload,
    )


@retry(
    stop=stop_after_attempt(RETRY_ATTEMPTS),
    wait=wait_fixed(RETRY_WAIT_SEC),
    retry=retry_if_exception_type(subprocess.SubprocessError),
    reraise=True,
)
def _run_subprocess(argv: Sequence[str], *, timeout: int) -> subprocess.CompletedProcess:
    """Run the subprocess, retrying transient ``SubprocessError`` failures."""
    return subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def invoke(
    prompt: str,
    *,
    system_prompt: str | None = None,
    binary: str | None = None,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
) -> ClaudeResponse:
    """Invoke ``claude -p`` once and return the structured response.

    Args:
        prompt: the user-side message to send.
        system_prompt: optional system prompt to inject. The kernel's
            generic system prompt lives in ``kernel/prompts/system.md``;
            issue #1 just passes a small placeholder.
        binary: override path to the ``claude`` executable. Test-only.
        timeout_sec: how long to wait before killing the subprocess.

    Raises:
        ClaudeRunnerError: subprocess failed, timed out, or returned
            unparseable output.
    """
    argv = _build_argv(
        prompt,
        binary=binary or os.environ.get("CLAUDE_BIN", DEFAULT_BIN),
        system_prompt=system_prompt,
    )

    try:
        proc = _run_subprocess(argv, timeout=timeout_sec)
    except subprocess.TimeoutExpired as err:
        raise ClaudeRunnerError(f"claude -p timed out after {timeout_sec}s") from err
    except RetryError as err:
        raise ClaudeRunnerError(
            f"claude -p failed after {RETRY_ATTEMPTS} attempts: {err}"
        ) from err

    if proc.returncode != 0:
        raise ClaudeRunnerError(
            f"claude -p exited with code {proc.returncode}: {proc.stderr.strip()}"
        )

    return _parse_response(proc.stdout)
