"""Tests for ``domains.journal.handler.write`` — the first plugin write path.

The handler:

  - Picks a slug from the message and a date from the session/clock
  - Writes ``vault/journal/{date}-{slug}.md`` via ``kernel.vault.atomic_write``
  - Frontmatter contains ``date``, ``tags`` (LLM-extracted), ``links: []``,
    ``source``, ``session_id``
  - Idempotent on content sha256: same input yields the same path & file

The LLM-driven tag extraction is mocked so tests don't shell out to
``claude -p``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import yaml

from domains.journal.handler import write
from kernel.session import Session


def _make_session(chat_id: str = "chat-1", session_id: str = "sess-abc") -> Session:
    """Build a minimal Session for handler tests."""
    return Session(
        chat_id=chat_id,
        session_id=session_id,
        started_at="2026-04-29T10:00:00+00:00",
        last_updated="2026-04-29T10:00:00+00:00",
        turns=0,
        summary="",
    )


def _fixed_clock() -> datetime:
    return datetime(2026, 4, 29, 12, 30, 0, tzinfo=timezone.utc)


def _no_tags(_text: str) -> list[str]:
    return []


def test_write_creates_file_at_dated_slug_path(tmp_path: Path) -> None:
    """A successful write puts the note at ``vault/journal/{date}-{slug}.md``."""
    vault_root = tmp_path / "vault"
    session = _make_session()

    result = write(
        intent="journal.capture",
        message="interesting idea about tiered memory in agents",
        session=session,
        vault_root=vault_root,
        clock=_fixed_clock,
        tag_extractor=_no_tags,
    )

    expected_dir = vault_root / "journal"
    assert result.path.parent == expected_dir
    assert result.path.name.startswith("2026-04-29-")
    assert result.path.name.endswith(".md")
    assert result.path.exists()


def test_write_emits_frontmatter_with_required_fields(tmp_path: Path) -> None:
    """All five required frontmatter fields appear with sensible values."""
    vault_root = tmp_path / "vault"
    session = _make_session(session_id="sess-zzz")

    result = write(
        intent="journal.capture",
        message="memgpt is interesting",
        session=session,
        vault_root=vault_root,
        clock=_fixed_clock,
        tag_extractor=lambda _t: ["memory", "agents"],
    )

    contents = result.path.read_text(encoding="utf-8")
    assert contents.startswith("---\n")

    head, _, body = contents.partition("\n---\n")
    front = yaml.safe_load(head[len("---\n"):])

    assert front["date"] == "2026-04-29T12:30:00+00:00"
    assert front["tags"] == ["memory", "agents"]
    assert front["links"] == []
    assert front["source"] == "telegram"
    assert front["session_id"] == "sess-zzz"
    assert "memgpt is interesting" in body


def test_write_is_idempotent_on_identical_input(tmp_path: Path) -> None:
    """Re-writing the same message in the same session returns the same path."""
    vault_root = tmp_path / "vault"
    session = _make_session()

    first = write(
        intent="journal.capture",
        message="reread MemGPT today",
        session=session,
        vault_root=vault_root,
        clock=_fixed_clock,
        tag_extractor=_no_tags,
    )
    second = write(
        intent="journal.capture",
        message="reread MemGPT today",
        session=session,
        vault_root=vault_root,
        clock=_fixed_clock,
        tag_extractor=_no_tags,
    )

    assert first.path == second.path
    assert first.content_sha256 == second.content_sha256
    # And only one file exists in the journal dir.
    journal_files = list((vault_root / "journal").iterdir())
    assert len(journal_files) == 1


def test_write_different_messages_produce_different_paths(tmp_path: Path) -> None:
    """Different inputs do not collide onto the same filename."""
    vault_root = tmp_path / "vault"
    session = _make_session()

    first = write(
        intent="journal.capture",
        message="thought one",
        session=session,
        vault_root=vault_root,
        clock=_fixed_clock,
        tag_extractor=_no_tags,
    )
    second = write(
        intent="journal.capture",
        message="thought two",
        session=session,
        vault_root=vault_root,
        clock=_fixed_clock,
        tag_extractor=_no_tags,
    )

    assert first.path != second.path
