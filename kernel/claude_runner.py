"""Subprocess wrapper for ``claude -p`` (headless Claude Code).

Every per-turn LLM call goes through ``invoke``. The wrapper:

  1. Spawns ``claude -p --output-format json`` with the user's prompt
  2. Always sends an explicit system prompt (``kernel/prompts/system.md``
     unless the caller overrides) so the subprocess never inherits the
     developer's ambient Claude Code output style. Without this, a
     ``learning`` style on the developer's machine would leak insight
     blocks and code fences into vault writes.
  3. Parses the JSON envelope, extracting the response text and the
     ``usage.input_tokens`` / ``usage.output_tokens`` fields
  4. Surfaces a non-zero exit code as a ``ClaudeRunnerError``
  5. Retries transient subprocess failures via ``tenacity``

The token telemetry that this function returns is what the audit log
records per turn (``kernel/RUNTIME.md`` -> "Token telemetry"). No
``claude -p`` invocation in the kernel is allowed to skip this wrapper.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_fixed,
)

__all__ = ["ClaudeResponse", "ClaudeRunnerError", "invoke", "load_default_system_prompt"]


DEFAULT_BIN = "claude"
DEFAULT_TIMEOUT_SEC = 60
RETRY_ATTEMPTS = 3
RETRY_WAIT_SEC = 0.5

_DEFAULT_SYSTEM_PROMPT_PATH = Path(__file__).parent / "prompts" / "system.md"


def load_default_system_prompt() -> str:
    """Read ``kernel/prompts/system.md`` from disk.

    Kept as a function (not a module-level constant) so tests can monkeypatch
    the file and see the change without re-importing the module.
    """
    return _DEFAULT_SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")


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

    ``claude -p --output-format json`` emits a JSON array of stream events.
    The terminal event is ``{"type": "result", "result": "<text>",
    "usage": {"input_tokens", "output_tokens", ...}, ...}``. Earlier events
    (``system``, ``assistant``, ``rate_limit_event``) are not the
    authoritative reply — the ``result`` event aggregates them.

    For robustness we also accept a single-object envelope (legacy / fixture
    shape) so older tests and any future format change are not catastrophic.
    """
    try:
        payload = json.loads(raw_stdout)
    except json.JSONDecodeError as err:
        raise ClaudeRunnerError(
            f"could not parse claude -p JSON envelope: {err}"
        ) from err

    # Array shape (real claude -p output): find the terminal `result` event.
    if isinstance(payload, list):
        result_event = None
        for event in reversed(payload):
            if isinstance(event, dict) and event.get("type") == "result":
                result_event = event
                break
        if result_event is None:
            # Fall back to the last assistant message if no result event.
            for event in reversed(payload):
                if isinstance(event, dict) and event.get("type") == "assistant":
                    msg = event.get("message") or {}
                    content = msg.get("content") or []
                    text_parts = [
                        c.get("text", "")
                        for c in content
                        if isinstance(c, dict) and c.get("type") == "text"
                    ]
                    usage = msg.get("usage") or {}
                    return ClaudeResponse(
                        text="".join(text_parts),
                        tokens_in=int(usage.get("input_tokens", 0) or 0),
                        tokens_out=int(usage.get("output_tokens", 0) or 0),
                        raw={"events": payload},
                    )
            # No usable event at all.
            return ClaudeResponse(text="", tokens_in=0, tokens_out=0, raw={"events": payload})
        text = result_event.get("result") or ""
        usage = result_event.get("usage") or {}
        return ClaudeResponse(
            text=str(text),
            tokens_in=int(usage.get("input_tokens", 0) or 0),
            tokens_out=int(usage.get("output_tokens", 0) or 0),
            raw=result_event,
        )

    # Object shape (legacy / fixture).
    if isinstance(payload, dict):
        text = payload.get("result") or payload.get("text") or ""
        usage = payload.get("usage") or {}
        return ClaudeResponse(
            text=str(text),
            tokens_in=int(usage.get("input_tokens", 0) or 0),
            tokens_out=int(usage.get("output_tokens", 0) or 0),
            raw=payload,
        )

    raise ClaudeRunnerError(
        f"unexpected claude -p envelope type: {type(payload).__name__}"
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
        system_prompt: system prompt to inject. ``None`` (the default)
            loads ``kernel/prompts/system.md`` so the subprocess never
            inherits the developer's ambient Claude Code output style.
            Pass an explicit non-empty string to override; pass an empty
            string to deliberately suppress the kernel system prompt
            (test-only).
        binary: override path to the ``claude`` executable. Test-only.
        timeout_sec: how long to wait before killing the subprocess.

    Raises:
        ClaudeRunnerError: subprocess failed, timed out, or returned
            unparseable output.
    """
    if system_prompt is None:
        system_prompt = load_default_system_prompt()

    argv = _build_argv(
        prompt,
        binary=binary or os.environ.get("CLAUDE_BIN", DEFAULT_BIN),
        system_prompt=system_prompt or None,
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
