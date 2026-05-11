"""Tests for ``Orchestrator._handle_echo`` chat_log wiring (issue #25).

Covers the two orchestrator-side contracts established by the ADR at
``docs/decisions/2026-05-10-chat-context-and-capabilities.md``:

  1. The echo handler loads prior turns via ``kernel.chat_log.load_recent``
     and prepends them to the LLM prompt as a "Recent conversation"
     preamble.
  2. The echo handler appends the new turn to the chat log after
     dispatch — including the error path — so the next turn has a
     complete antecedent record.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from kernel import chat_log
from kernel.claude_runner import ClaudeResponse, ClaudeRunnerError
from kernel.orchestrator import Orchestrator, SingleInstanceLock


class _RecordingInvoker:
    """Capture the prompt the orchestrator passes to the LLM."""

    def __init__(self, *, text: str = "ok", tokens_in: int = 1, tokens_out: int = 1) -> None:
        self.calls: list[dict] = []
        self._text = text
        self._tokens_in = tokens_in
        self._tokens_out = tokens_out

    def __call__(self, prompt: str, *, system_prompt: Optional[str] = None) -> ClaudeResponse:
        self.calls.append({"prompt": prompt, "system_prompt": system_prompt})
        return ClaudeResponse(
            text=self._text,
            tokens_in=self._tokens_in,
            tokens_out=self._tokens_out,
            raw={},
        )


def _fixed_clock(dt: datetime):
    return lambda: dt


def test_handle_echo_includes_prior_turns_in_prompt(
    tmp_path: Path, lock_path: Path
) -> None:
    """Prior turns rendered into a 'Recent conversation' preamble."""
    vault_root = tmp_path / "vault"
    chat_id = "chat-echo"

    # Pre-seed two prior turns so the echo handler picks them up.
    chat_log.append(
        chat_id,
        user_msg="generate me a workout plan",
        bot_reply="Saved plan to fitness/plans/2026-05-10.md.",
        intent="",
        vault_root=vault_root,
        clock=_fixed_clock(datetime(2026, 5, 10, 8, 0, 0, tzinfo=timezone.utc)),
    )
    chat_log.append(
        chat_id,
        user_msg="cool, want me to walk you through it?",
        bot_reply="Yes, walk me through it.",
        intent="",
        vault_root=vault_root,
        clock=_fixed_clock(datetime(2026, 5, 10, 8, 0, 30, tzinfo=timezone.utc)),
    )

    invoker = _RecordingInvoker(text="ok")
    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=tmp_path / "audit",
        vault_root=vault_root,
        invoker=invoker,
        chat_id=chat_id,
    )

    orchestrator.handle_message("yes please")

    assert len(invoker.calls) == 1
    prompt = invoker.calls[0]["prompt"]
    assert "## Recent conversation" in prompt
    assert "generate me a workout plan" in prompt
    assert "Saved plan to fitness/plans/2026-05-10.md." in prompt
    # The current turn's message stays at the bottom.
    assert prompt.rstrip().endswith("yes please")


def test_handle_echo_with_no_history_passes_message_unchanged(
    tmp_path: Path, lock_path: Path
) -> None:
    """An empty chat log yields the bare user message — issue-#1 shape preserved."""
    invoker = _RecordingInvoker()
    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=tmp_path / "audit",
        vault_root=tmp_path / "vault",
        invoker=invoker,
        chat_id="chat-fresh",
    )

    orchestrator.handle_message("hello")

    assert invoker.calls[0]["prompt"] == "hello"


def test_handle_echo_appends_turn_to_chat_log_after_success(
    tmp_path: Path, lock_path: Path
) -> None:
    """A successful dispatch records the turn so the next turn sees it."""
    vault_root = tmp_path / "vault"
    chat_id = "chat-append-ok"
    fixed_now = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)

    invoker = _RecordingInvoker(text="hello back")
    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=tmp_path / "audit",
        vault_root=vault_root,
        invoker=invoker,
        chat_id=chat_id,
        clock=_fixed_clock(fixed_now),
    )

    orchestrator.handle_message("hi there")

    turns = chat_log.load_recent(chat_id, n=10, vault_root=vault_root)
    assert len(turns) == 1
    assert turns[0].user_msg == "hi there"
    assert turns[0].bot_reply == "hello back"
    assert turns[0].chat_id == chat_id
    assert turns[0].intent == ""


def test_handle_echo_appends_friendly_error_on_runner_failure(
    tmp_path: Path, lock_path: Path
) -> None:
    """Even when the LLM call fails, the chat log records the user message + apology.

    Without this, the next turn would see only the user's message with
    no antecedent — losing the "you asked X, I apologized" context.
    """
    vault_root = tmp_path / "vault"
    chat_id = "chat-append-err"

    def boom(_prompt, *, system_prompt=None):
        raise ClaudeRunnerError("subprocess crashed")

    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=tmp_path / "audit",
        vault_root=vault_root,
        invoker=boom,
        chat_id=chat_id,
        clock=_fixed_clock(datetime(2026, 5, 10, 13, 0, 0, tzinfo=timezone.utc)),
    )

    reply = orchestrator.handle_message("anything")

    assert "wrong" in reply.text.lower()

    turns = chat_log.load_recent(chat_id, n=10, vault_root=vault_root)
    assert len(turns) == 1
    assert turns[0].user_msg == "anything"
    assert "wrong" in turns[0].bot_reply.lower()


def test_handle_echo_does_not_pollute_other_chat_id(
    tmp_path: Path, lock_path: Path
) -> None:
    """A turn under chat-A doesn't leak into chat-B's load_recent."""
    vault_root = tmp_path / "vault"

    invoker = _RecordingInvoker()
    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=tmp_path / "audit",
        vault_root=vault_root,
        invoker=invoker,
        chat_id="chat-A",
    )

    orchestrator.handle_message("just for A")

    a_turns = chat_log.load_recent("chat-A", n=10, vault_root=vault_root)
    b_turns = chat_log.load_recent("chat-B", n=10, vault_root=vault_root)

    assert len(a_turns) == 1
    assert b_turns == []
