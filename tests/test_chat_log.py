"""Tests for ``kernel.chat_log`` — verbatim per-turn append-only log.

The chat log carries the last-N user/bot turns so the orchestrator can
resolve referential follow-ups (e.g., "yes, walk me through it"). These
tests exercise the deep-narrow API in isolation: append + load
round-trip, daily rotation, sha256 idempotency, atomic-write hygiene
(no .tmp debris), and per-chat_id isolation.

Storage layout under test:
    <vault_root>/_chat_log/<chat_id>/<YYYY-MM-DD>.jsonl
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from kernel.chat_log import ChatTurn, append, load_recent


def _fixed_clock(dt: datetime):
    """Build a clock callable that always returns ``dt``."""
    return lambda: dt


def test_append_then_load_recent_returns_chronological_turns(tmp_path: Path) -> None:
    """Three appends round-trip through load_recent in chronological order."""
    vault_root = tmp_path / "vault"
    chat_id = "chat-A"

    base = datetime(2026, 5, 10, 9, 0, 0, tzinfo=timezone.utc)
    for offset, msg in enumerate(("first", "second", "third")):
        append(
            chat_id,
            user_msg=f"user {msg}",
            bot_reply=f"bot {msg}",
            intent="",
            vault_root=vault_root,
            clock=_fixed_clock(base.replace(second=offset)),
        )

    turns = load_recent(chat_id, n=10, vault_root=vault_root)

    assert [t.user_msg for t in turns] == ["user first", "user second", "user third"]
    assert [t.bot_reply for t in turns] == ["bot first", "bot second", "bot third"]


def test_load_recent_walks_back_across_daily_files(tmp_path: Path, monkeypatch) -> None:
    """When today's file alone can't satisfy N, load_recent crosses into yesterday."""
    vault_root = tmp_path / "vault"
    chat_id = "chat-spanning"

    yesterday = datetime(2026, 5, 9, 23, 30, 0, tzinfo=timezone.utc)
    today = datetime(2026, 5, 10, 0, 15, 0, tzinfo=timezone.utc)

    append(
        chat_id,
        user_msg="from yesterday",
        bot_reply="reply from yesterday",
        intent="",
        vault_root=vault_root,
        clock=_fixed_clock(yesterday),
    )
    append(
        chat_id,
        user_msg="from today",
        bot_reply="reply from today",
        intent="",
        vault_root=vault_root,
        clock=_fixed_clock(today),
    )

    # Confirm two distinct daily files exist on disk.
    chat_dir = vault_root / "_chat_log" / chat_id
    daily_files = sorted(p.name for p in chat_dir.glob("*.jsonl"))
    assert daily_files == ["2026-05-09.jsonl", "2026-05-10.jsonl"]

    # Pin "today" to 2026-05-10 so the backward walk starts from the
    # known anchor; otherwise the test depends on wall-clock time.
    monkeypatch.setattr(
        "kernel.chat_log._now_utc",
        lambda clock=None: today,
    )

    turns = load_recent(chat_id, n=5, vault_root=vault_root)

    assert [t.user_msg for t in turns] == ["from yesterday", "from today"]


def test_append_is_idempotent_on_duplicate_content(tmp_path: Path) -> None:
    """Appending the same (chat_id, ts, user_msg, bot_reply) twice yields one entry."""
    vault_root = tmp_path / "vault"
    chat_id = "chat-dup"
    ts = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
    clock = _fixed_clock(ts)

    a = append(
        chat_id,
        user_msg="hello",
        bot_reply="world",
        intent="",
        vault_root=vault_root,
        clock=clock,
    )
    b = append(
        chat_id,
        user_msg="hello",
        bot_reply="world",
        intent="",
        vault_root=vault_root,
        clock=clock,
    )

    assert a.id == b.id  # sha256 over the same content collapses

    turns = load_recent(chat_id, n=10, vault_root=vault_root)
    assert len(turns) == 1
    assert turns[0].user_msg == "hello"

    # And the on-disk file has exactly one non-empty line.
    daily = vault_root / "_chat_log" / chat_id / "2026-05-10.jsonl"
    nonblank = [line for line in daily.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(nonblank) == 1


def test_append_leaves_no_tmp_debris_after_success(tmp_path: Path) -> None:
    """The atomic write path cleans up its tmp file."""
    vault_root = tmp_path / "vault"
    chat_id = "chat-clean"

    append(
        chat_id,
        user_msg="hi",
        bot_reply="ok",
        intent="",
        vault_root=vault_root,
        clock=_fixed_clock(datetime(2026, 5, 10, 8, 0, 0, tzinfo=timezone.utc)),
    )

    chat_dir = vault_root / "_chat_log" / chat_id
    leftover_tmp = list(chat_dir.glob("*.tmp"))
    assert leftover_tmp == []


def test_chat_id_isolation_across_load_recent(tmp_path: Path) -> None:
    """Turns appended under chat_id A do not appear in load_recent for chat_id B."""
    vault_root = tmp_path / "vault"
    ts = datetime(2026, 5, 10, 14, 0, 0, tzinfo=timezone.utc)
    clock = _fixed_clock(ts)

    append(
        "chat-A",
        user_msg="A's question",
        bot_reply="A's reply",
        intent="",
        vault_root=vault_root,
        clock=clock,
    )

    a_turns = load_recent("chat-A", n=5, vault_root=vault_root)
    b_turns = load_recent("chat-B", n=5, vault_root=vault_root)

    assert len(a_turns) == 1
    assert a_turns[0].user_msg == "A's question"
    assert b_turns == []


def test_appended_record_carries_all_fields(tmp_path: Path) -> None:
    """The on-disk record matches the ChatTurn schema field-for-field."""
    vault_root = tmp_path / "vault"
    chat_id = "chat-fields"
    ts = datetime(2026, 5, 10, 10, 30, 0, tzinfo=timezone.utc)

    turn = append(
        chat_id,
        user_msg="what can you do?",
        bot_reply="lots of things!",
        intent="capability.query",
        vault_root=vault_root,
        clock=_fixed_clock(ts),
    )

    daily = vault_root / "_chat_log" / chat_id / "2026-05-10.jsonl"
    record = json.loads(daily.read_text(encoding="utf-8").strip())

    assert record["id"] == turn.id
    assert record["ts"] == ts.isoformat()
    assert record["chat_id"] == chat_id
    assert record["user_msg"] == "what can you do?"
    assert record["bot_reply"] == "lots of things!"
    assert record["intent"] == "capability.query"


def test_load_recent_returns_empty_for_unknown_chat(tmp_path: Path) -> None:
    """A chat_id with no log directory yields an empty list, not an exception."""
    vault_root = tmp_path / "vault"
    assert load_recent("nobody", n=5, vault_root=vault_root) == []


def test_load_recent_clamps_to_n(tmp_path: Path) -> None:
    """Asking for fewer turns than exist returns the most-recent slice."""
    vault_root = tmp_path / "vault"
    chat_id = "chat-clamp"
    base = datetime(2026, 5, 10, 9, 0, 0, tzinfo=timezone.utc)

    for i in range(5):
        append(
            chat_id,
            user_msg=f"u{i}",
            bot_reply=f"b{i}",
            intent="",
            vault_root=vault_root,
            clock=_fixed_clock(base.replace(second=i)),
        )

    last_two = load_recent(chat_id, n=2, vault_root=vault_root)
    assert [t.user_msg for t in last_two] == ["u3", "u4"]


def test_chat_turn_is_frozen() -> None:
    """ChatTurn instances reject mutation (immutability per coding-style.md)."""
    turn = ChatTurn(
        id="abc",
        ts="2026-05-10T00:00:00+00:00",
        chat_id="c",
        user_msg="u",
        bot_reply="b",
        intent="",
    )
    import dataclasses

    try:
        turn.intent = "other"  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        return
    raise AssertionError("ChatTurn must be frozen")
