"""Tests for ``kernel.orchestrator`` — flock + per-turn echo wiring."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from kernel.claude_runner import ClaudeResponse, ClaudeRunnerError
from kernel.orchestrator import (
    InstanceLockError,
    Orchestrator,
    SingleInstanceLock,
)


def _stub_invoker(text: str = "echo reply", tokens_in: int = 5, tokens_out: int = 7):
    """Build a lambda matching the ``claude_runner.invoke`` signature."""

    def _invoke(prompt, *, system_prompt=None):
        return ClaudeResponse(
            text=text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            raw={"echo_of": prompt, "system_prompt": system_prompt},
        )

    return _invoke


def test_single_instance_lock_refuses_second_acquisition(lock_path: Path) -> None:
    """A second SingleInstanceLock against the same path can't acquire while the first holds."""
    holder = SingleInstanceLock(lock_path)
    holder.acquire()
    try:
        intruder = SingleInstanceLock(lock_path)
        with pytest.raises(InstanceLockError):
            intruder.acquire()
    finally:
        holder.release()


def test_handle_message_returns_claude_reply_text(tmp_path: Path, lock_path: Path) -> None:
    """The orchestrator surfaces the LLM's response text to its caller."""
    audit_root = tmp_path / "audit"
    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=audit_root,
        invoker=_stub_invoker(text="hello!"),
    )

    reply = orchestrator.handle_message("hi")

    assert reply.text == "hello!"


def test_handle_message_writes_audit_entry_with_token_telemetry(
    tmp_path: Path, lock_path: Path
) -> None:
    """Each turn produces exactly one audit-log line with parsed token counts."""
    audit_root = tmp_path / "audit"
    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=audit_root,
        invoker=_stub_invoker(tokens_in=11, tokens_out=23),
    )

    orchestrator.handle_message("hi")

    daily_files = list(audit_root.glob("*.jsonl"))
    assert len(daily_files) == 1
    lines = [
        json.loads(raw)
        for raw in daily_files[0].read_text(encoding="utf-8").splitlines()
        if raw.strip()
    ]
    assert len(lines) == 1
    record = lines[0]
    assert record["op"] == "echo"
    assert record["outcome"] == "ok"
    assert record["actor"] == "kernel.orchestrator"
    assert record["tokens_in"] == 11
    assert record["tokens_out"] == 23
    assert "duration_ms" in record
    assert record["config"] == "default"


def test_handle_message_logs_error_outcome_when_runner_fails(
    tmp_path: Path, lock_path: Path
) -> None:
    """A claude_runner failure becomes an audit entry with outcome=error and a friendly reply."""
    audit_root = tmp_path / "audit"

    def boom(_prompt, *, system_prompt=None):
        raise ClaudeRunnerError("subprocess crashed")

    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=audit_root,
        invoker=boom,
    )

    reply = orchestrator.handle_message("hi")

    assert "wrong" in reply.text.lower()
    record = json.loads(
        next(audit_root.glob("*.jsonl")).read_text(encoding="utf-8").strip()
    )
    assert record["outcome"] == "error"
    assert "subprocess crashed" in record["error"]
