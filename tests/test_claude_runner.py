"""Tests for ``kernel.claude_runner`` — subprocess wrapper for ``claude -p``."""

from __future__ import annotations

import json
import subprocess

import pytest

from kernel.claude_runner import ClaudeRunnerError, ClaudeResponse, invoke


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
