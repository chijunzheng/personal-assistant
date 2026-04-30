"""Tests for ``kernel.session`` — active session manager.

The session lives at ``vault/_index/active_session.md``: a markdown file with
a YAML frontmatter (chat id, started_at, last_updated, turn count) and a
running summary body. The kernel loads/creates it on every turn and updates
it after the dispatch completes.

Tests verify external behavior:
  - ``load_or_create`` is idempotent: same chat_id yields the same session
  - ``update`` appends to the running summary, doesn't truncate
  - the on-disk file round-trips to the same Session object
"""

from __future__ import annotations

from pathlib import Path

from kernel.session import Session, load_or_create, update


def test_load_or_create_creates_session_file_for_new_chat(tmp_path: Path) -> None:
    """First call for a chat_id materializes the session file with frontmatter."""
    vault_root = tmp_path / "vault"

    session = load_or_create("chat-123", vault_root=vault_root)

    assert isinstance(session, Session)
    assert session.chat_id == "chat-123"
    assert session.turns == 0
    expected = vault_root / "_index" / "active_session.md"
    assert expected.exists()


def test_load_or_create_returns_existing_session_unchanged(tmp_path: Path) -> None:
    """Second call for the same chat_id returns the existing session, not a new one."""
    vault_root = tmp_path / "vault"

    first = load_or_create("chat-abc", vault_root=vault_root)
    second = load_or_create("chat-abc", vault_root=vault_root)

    assert first.chat_id == second.chat_id
    assert first.session_id == second.session_id
    assert first.started_at == second.started_at


def test_load_or_create_starts_a_fresh_session_for_new_chat_id(tmp_path: Path) -> None:
    """A different chat_id supersedes the old session (single-chat invariant)."""
    vault_root = tmp_path / "vault"

    first = load_or_create("chat-old", vault_root=vault_root)
    second = load_or_create("chat-new", vault_root=vault_root)

    assert second.chat_id == "chat-new"
    assert second.session_id != first.session_id


def test_update_appends_to_running_summary_and_increments_turn_count(
    tmp_path: Path,
) -> None:
    """Each ``update`` adds a bullet to the summary and bumps ``turns``."""
    vault_root = tmp_path / "vault"
    session = load_or_create("chat-9", vault_root=vault_root)

    updated = update(session, "captured a thought about consciousness", vault_root=vault_root)

    assert updated.turns == 1
    assert "consciousness" in updated.summary
    # And the on-disk file reflects the update too.
    contents = (vault_root / "_index" / "active_session.md").read_text(encoding="utf-8")
    assert "consciousness" in contents


def test_update_preserves_prior_summary_lines(tmp_path: Path) -> None:
    """A second update appends without erasing earlier turns."""
    vault_root = tmp_path / "vault"
    session = load_or_create("chat-9", vault_root=vault_root)

    after_first = update(session, "first thought", vault_root=vault_root)
    after_second = update(after_first, "second thought", vault_root=vault_root)

    assert "first thought" in after_second.summary
    assert "second thought" in after_second.summary
    assert after_second.turns == 2
