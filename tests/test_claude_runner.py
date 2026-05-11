"""Tests for ``kernel.claude_runner`` — subprocess wrapper for ``claude -p``."""

from __future__ import annotations

import json
import subprocess

import pytest

from kernel.claude_runner import (
    ClaudeRunnerError,
    ClaudeResponse,
    invoke,
    load_default_system_prompt,
)


def _fake_completed_process(stdout: str, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    """Build a CompletedProcess-shaped object for monkeypatching ``subprocess.run``."""
    return subprocess.CompletedProcess(
        args=["claude", "-p", "--output-format", "json", "hi"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def test_invoke_parses_tokens_in_and_tokens_out_from_usage(monkeypatch) -> None:
    """The runner extracts ``tokens_in`` / ``tokens_out`` from claude -p's JSON usage."""
    response_json = json.dumps(
        {
            "result": "hello back",
            "usage": {"input_tokens": 17, "output_tokens": 41},
        }
    )

    def fake_run(*_args, **_kwargs):
        return _fake_completed_process(response_json)

    monkeypatch.setattr(subprocess, "run", fake_run)

    response = invoke("hi")

    assert isinstance(response, ClaudeResponse)
    assert response.text == "hello back"
    assert response.tokens_in == 17
    assert response.tokens_out == 41


def test_invoke_raises_on_non_zero_exit(monkeypatch) -> None:
    """A failing subprocess surfaces as ``ClaudeRunnerError`` so callers can react."""

    def fake_run(*_args, **_kwargs):
        return _fake_completed_process(
            stdout="",
            returncode=2,
            stderr="bad things happened",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(ClaudeRunnerError, match="bad things happened"):
        invoke("hi")


def test_invoke_parses_real_claude_p_array_envelope(monkeypatch) -> None:
    """``claude -p --output-format json`` returns an array of stream events.

    The terminal ``{type: "result"}`` event is the authoritative reply with
    aggregate ``usage``. The parser must find it and skip the earlier
    ``system`` / ``assistant`` / ``rate_limit_event`` items.
    """
    response_json = json.dumps(
        [
            {"type": "system", "subtype": "init", "session_id": "abc"},
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "Hi there, friend!"}],
                    "usage": {"input_tokens": 5, "output_tokens": 4},
                },
            },
            {"type": "rate_limit_event"},
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "Hi there, friend!",
                "usage": {"input_tokens": 5, "output_tokens": 12},
            },
        ]
    )

    def fake_run(*_args, **_kwargs):
        return _fake_completed_process(response_json)

    monkeypatch.setattr(subprocess, "run", fake_run)

    response = invoke("hi")

    assert response.text == "Hi there, friend!"
    assert response.tokens_in == 5
    assert response.tokens_out == 12  # result-level aggregate, not the per-message 4


def test_invoke_falls_back_to_assistant_event_when_no_result_event(monkeypatch) -> None:
    """If no result event is present, the parser uses the last assistant message."""
    response_json = json.dumps(
        [
            {"type": "system", "subtype": "init"},
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "partial reply"}],
                    "usage": {"input_tokens": 3, "output_tokens": 7},
                },
            },
        ]
    )

    def fake_run(*_args, **_kwargs):
        return _fake_completed_process(response_json)

    monkeypatch.setattr(subprocess, "run", fake_run)

    response = invoke("hi")
    assert response.text == "partial reply"
    assert response.tokens_in == 3
    assert response.tokens_out == 7


def test_invoke_default_system_prompt_loads_kernel_prompts_system_md(monkeypatch) -> None:
    """``invoke()`` with ``system_prompt=None`` (the default) must inject the
    on-disk ``kernel/prompts/system.md`` so the subprocess never inherits
    the developer's ambient Claude Code output style.

    Verifies by spying on ``subprocess.run``'s ``argv`` and confirming the
    ``--system-prompt`` value matches the file's content (non-empty and
    equal to ``load_default_system_prompt()``).
    """
    response_json = json.dumps(
        {"result": "ok", "usage": {"input_tokens": 1, "output_tokens": 1}}
    )
    captured: dict = {}

    def fake_run(argv, *_args, **_kwargs):
        captured["argv"] = list(argv)
        return _fake_completed_process(response_json)

    monkeypatch.setattr(subprocess, "run", fake_run)

    invoke("hi")  # default system_prompt=None

    argv = captured["argv"]
    assert "--system-prompt" in argv, f"system prompt flag missing from {argv}"
    idx = argv.index("--system-prompt")
    sent_prompt = argv[idx + 1]
    expected = load_default_system_prompt()
    assert sent_prompt == expected
    assert sent_prompt.strip(), "loaded system prompt must be non-empty"


def test_invoke_with_empty_system_prompt_suppresses_flag(monkeypatch) -> None:
    """Passing ``system_prompt=""`` (test-only escape hatch) must omit
    ``--system-prompt`` entirely so the subprocess inherits no system prompt.
    """
    response_json = json.dumps(
        {"result": "ok", "usage": {"input_tokens": 1, "output_tokens": 1}}
    )
    captured: dict = {}

    def fake_run(argv, *_args, **_kwargs):
        captured["argv"] = list(argv)
        return _fake_completed_process(response_json)

    monkeypatch.setattr(subprocess, "run", fake_run)

    invoke("hi", system_prompt="")

    assert "--system-prompt" not in captured["argv"]


def test_invoke_retries_on_transient_subprocess_error(monkeypatch) -> None:
    """Transient SubprocessError failures are retried before giving up."""
    response_json = json.dumps({"result": "eventually", "usage": {"input_tokens": 1, "output_tokens": 2}})

    calls = {"count": 0}

    def flaky_run(*_args, **_kwargs):
        calls["count"] += 1
        if calls["count"] < 2:
            raise subprocess.SubprocessError("transient")
        return _fake_completed_process(response_json)

    monkeypatch.setattr(subprocess, "run", flaky_run)

    response = invoke("hi")

    assert response.text == "eventually"
    assert calls["count"] == 2  # one failure, then success
