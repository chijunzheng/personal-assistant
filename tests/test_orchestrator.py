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


def test_capabilities_placeholder_substituted_before_invoker(
    tmp_path: Path, lock_path: Path
) -> None:
    """The ``{capabilities}`` slot is filled before the LLM call, not at run time.

    This pins the contract that ``claude_runner.invoke`` always receives a
    ready-to-go system prompt -- substitution belongs at the orchestrator
    level (it knows about ``domains_root``), not inside the runner. Failing
    this test means the bot would surface a literal ``{capabilities}`` to
    Claude, defeating the whole capabilities preamble.
    """
    import yaml

    domains_root = tmp_path / "domains"
    domains_root.mkdir()
    fitness_dir = domains_root / "fitness"
    fitness_dir.mkdir()
    (fitness_dir / "domain.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "fitness",
                "description_user_facing": "Track workouts and adapt plans.",
                "intents": ["fitness.workout_log"],
                "intent_examples": {"fitness.workout_log": ["did 5x5 squats"]},
            }
        ),
        encoding="utf-8",
    )

    captured: dict = {}

    def _invoker(prompt, *, system_prompt=None):
        # Stash the system prompt the runner would have seen; the
        # orchestrator must have substituted the placeholder by now.
        captured["system_prompt"] = system_prompt
        return ClaudeResponse(
            text="ok",
            tokens_in=1,
            tokens_out=1,
            raw={"prompt": prompt, "system_prompt": system_prompt},
        )

    audit_root = tmp_path / "audit"
    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=audit_root,
        invoker=_invoker,
        domains_root=domains_root,
        # Pass a system prompt that contains the placeholder so we exercise
        # the substitution path rather than the legacy tracer default.
        system_prompt="Coach voice prompt.\n\n{capabilities}\n\nEnd.",
    )

    orchestrator.handle_message("what can you do?")

    assert "system_prompt" in captured
    sent_prompt = captured["system_prompt"]
    assert sent_prompt is not None
    # Substitution happened: the literal token is gone, the description
    # the user would actually recognize is present.
    assert "{capabilities}" not in sent_prompt
    assert "Track workouts and adapt plans." in sent_prompt
